"""B 策略重新寻优（2026-08-29）。

背景：8/26 那轮参数寻优跑在带缺陷的构建器上（持仓叠加成 3~4 倍杠杆），
结论不可信。本脚本在修复后的构建器上重新扫描：

- 标的池：指数池 / ETF池 / ETF+黄金池
- 结构：Top1 轮动、Top2 轮动、波动缩放、单资产均线择时
- 执行延迟：1 根K线（与 A 一致的标准口径） vs 2 根K线（现有实现）
- 对照组：各资产买入持有 + 当前线上配置

所有回测均含 15bp 换手成本、次日执行、无未来函数。
"""
from __future__ import annotations

import json
import itertools
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

from backend import data_feed as dfd
from backend.backtest import run_multi_asset, metrics

START = "20220601"
BENCH = "sh000300"          # 沪深300
SAFE = "sh511010"           # 国债ETF（防御资产）

POOLS = {
    "指数池(创业板/300/红利)": ["sz399006", "sh000300", "sh000015"],
    "ETF池(创业板/300/红利)": ["sz159915", "sh510300", "sh510880"],
    "ETF+黄金池": ["sz159915", "sh510300", "sh510880", "sh518880"],
}
NAMES = {
    "sz399006": "创业板指", "sh000300": "沪深300", "sh000015": "上证红利",
    "sz159915": "创业板ETF", "sh510300": "沪深300ETF", "sh510880": "红利ETF",
    "sh518880": "黄金ETF", "sh511010": "国债ETF",
}

FRAMES: dict[str, pd.DataFrame] = {}
_BENCH_EQ: pd.Series | None = None


def load(code: str) -> pd.DataFrame:
    if code not in FRAMES:
        FRAMES[code] = dfd.get_a(code, START)
    return FRAMES[code]


def closes_of(codes: list[str]) -> pd.DataFrame:
    codes = list(dict.fromkeys(list(codes) + [SAFE]))
    return pd.DataFrame({c: load(c)["close"] for c in codes}).sort_index()


def rotation_weights(closes, candidates, *, mom_window, vol_window, ma_window,
                     weekly, top_k, buffer_pct, risk_fraction=1.0, lag=1):
    """风险调整动量轮动。返回 (weights, decisions)。

    lag=1：决策日 t 的权重从 t 开始，经回测 shift 后赚 t+1 的收益（与 A 一致的口径）
    lag=2：权重从 t+1 开始，多延迟一天（现有线上实现）
    """
    rets = closes.pct_change()
    score = closes.pct_change(mom_window) / rets.rolling(vol_window).std() * np.sqrt(252)
    above_ma = closes > closes.rolling(ma_window).mean()

    if weekly:
        key = closes.index.to_period("W")
        is_last = pd.Series(key, index=closes.index) != pd.Series(key, index=closes.index).shift(-1)
        dec_idx = closes.index[is_last.fillna(False)]
    else:
        dec_idx = closes.index

    w = pd.DataFrame(0.0, index=closes.index, columns=closes.columns)
    rows, prev_pick = [], []
    for t in dec_idx:
        s, ok = score.loc[t], above_ma.loc[t]
        cand = [c for c in candidates if bool(ok.get(c, False)) and np.isfinite(s.get(c, np.nan))]
        cand.sort(key=lambda c: s[c], reverse=True)
        if not cand:
            chosen, reason = [], "候选全部走弱，转入防御"
        elif top_k == 1:
            # 与生产实现严格一致：只有"最强挑战者"与在位者比较，
            # 挑战者分数需超出在位者 buffer_pct 才顶替。
            # 注意：不能用"逐个挑战者比较最弱在位者"——分数为负时乘法缓冲会失效。
            leader = cand[0]
            inc = prev_pick[0] if (prev_pick and prev_pick[0] in cand) else None
            if inc is None or leader == inc or s[leader] > s[inc] * (1 + buffer_pct):
                chosen = [leader]
            else:
                chosen = [inc]
            reason = "动量排序(含在位缓冲)"
        else:
            # Top-K：在位者优先占座，空位由分数最高的挑战者补
            merged: list[str] = [c for c in cand if c in prev_pick][:top_k]
            for c in cand:
                if len(merged) >= top_k:
                    break
                if c not in merged:
                    merged.append(c)
            chosen = sorted(merged, key=lambda c: s[c], reverse=True)[:top_k]
            reason = "动量排序(TopK 等权)"
        prev_pick = chosen
        pos = closes.index.searchsorted(t)
        start = closes.index[min(pos + lag - 1, len(closes) - 1)]
        # 关键：每次切换先清空所有列，杜绝持仓叠加
        w.loc[start:, :] = 0.0
        if chosen:
            per = risk_fraction / len(chosen)
            for c in chosen:
                w.loc[start:, c] = per
            if risk_fraction < 1.0:
                w.loc[start:, SAFE] = w.loc[start:, SAFE] + (1.0 - risk_fraction)
        else:
            w.loc[start:, SAFE] = 1.0
        rows.append({"date": t, "pick": "+".join(chosen) or SAFE, "reason": reason})
    return w, pd.DataFrame(rows).set_index("date")


def ma_gate_weights(closes, asset, ma_window):
    """单资产均线择时：站上均线持有，否则持国债。"""
    w = pd.DataFrame(0.0, index=closes.index, columns=closes.columns)
    on = (closes[asset] > closes[asset].rolling(ma_window).mean()).fillna(False)
    w[asset] = on.astype(float)
    w[SAFE] = 1.0 - w[asset]
    return w


def evaluate(w: pd.DataFrame, closes: pd.DataFrame, label: str, cfg: dict) -> dict:
    global _BENCH_EQ
    bt = run_multi_asset(w, closes, cost_rate=0.0015)
    m = metrics(bt["equity"], bt["ret"], bt["weight"])
    if _BENCH_EQ is None:
        bench = load(BENCH)["close"].dropna()
        _BENCH_EQ = bench / bench.iloc[0]
    bench_eq = _BENCH_EQ
    bm = metrics(bench_eq, bench_eq.pct_change().fillna(0))

    # 分段
    def win_stats(start):
        sub = bt.loc[bt.index >= start]
        if len(sub) < 2:
            return {}
        bsub = bench_eq.reindex(sub.index).dropna()
        return metrics(sub["equity"], sub["ret"], sub["weight"])

    def monthly_win():
        df = pd.concat([bt["ret"], bench_eq.pct_change().fillna(0)], axis=1,
                       keys=["s", "b"]).dropna()
        per = df.index.to_period("M")
        gs = (1 + df["s"]).groupby(per).prod() - 1
        gb = (1 + df["b"]).groupby(per).prod() - 1
        ex = gs - gb
        return round(float((ex > 0).mean()) * 100, 1), len(ex)

    mw, n_months = monthly_win()
    y1 = win_stats(bt.index[-1] - pd.DateOffset(years=1))
    since_jun = win_stats(pd.Timestamp("2026-06-01"))
    turnover = float(w.diff().abs().sum(axis=1).mean()) * 10000

    return {
        "label": label, "cfg": cfg,
        "total_return": m["total_return"], "cagr": m["cagr"], "max_dd": m["max_dd"],
        "sharpe": m["sharpe"], "ann_vol": m["ann_vol"], "exposure": m["avg_exposure"],
        "bench_total": bm["total_return"], "bench_dd": bm["max_dd"],
        "excess": round((m["total_return"] or 0) - (bm["total_return"] or 0), 1),
        "y1": y1.get("total_return"), "since_jun": since_jun.get("total_return"),
        "monthly_win": mw, "n_months": n_months, "turnover_bp": round(turnover, 1),
    }


def buy_hold(code: str) -> dict:
    closes = closes_of([code])
    w = pd.DataFrame(0.0, index=closes.index, columns=closes.columns)
    w[code] = 1.0
    return evaluate(w, closes, f"买入持有 · {NAMES.get(code, code)}", {"mode": "buy_hold", "code": code})


def main() -> None:
    results: list[dict] = []

    # ---------- 对照组：各资产买入持有 ----------
    for code in ["sz399006", "sh000300", "sh000015", "sh518880", "sh511010"]:
        results.append(buy_hold(code))

    # ---------- 当前线上配置（指数池 + 2根K线延迟） ----------
    closes = closes_of(POOLS["指数池(创业板/300/红利)"])
    w, _ = rotation_weights(closes, POOLS["指数池(创业板/300/红利)"],
                            mom_window=120, vol_window=60, ma_window=60,
                            weekly=True, top_k=1, buffer_pct=0.10, lag=2)
    results.append(evaluate(w, closes, "★当前线上配置(mom120/ma60/缓冲10%/延迟2根)",
                            {"mode": "rot", "pool": "指数池", "lag": 2}))

    # ---------- 轮动参数网格 ----------
    for pool_name, pool in POOLS.items():
        closes = closes_of(pool)
        grid = itertools.product([60, 120, 180], [40, 60, 120], [0.0, 0.10], [True, False], [1, 2])
        for mom, ma, buf, weekly, lag in grid:
            w, _ = rotation_weights(closes, pool, mom_window=mom, vol_window=60,
                                    ma_window=ma, weekly=weekly, top_k=1,
                                    buffer_pct=buf, lag=lag)
            results.append(evaluate(w, closes, f"轮动Top1 {pool_name} mom{mom} ma{ma} buf{buf} "
                                               f"{'周频' if weekly else '日频'} 延迟{lag}",
                                    {"mode": "rot", "pool": pool_name, "mom": mom, "ma": ma,
                                     "buf": buf, "weekly": weekly, "top_k": 1, "lag": lag}))
        # Top2 等权
        for mom, ma in itertools.product([120, 180], [60, 120]):
            w, _ = rotation_weights(closes, pool, mom_window=mom, vol_window=60,
                                    ma_window=ma, weekly=True, top_k=2,
                                    buffer_pct=0.10, lag=1)
            results.append(evaluate(w, closes, f"轮动Top2 {pool_name} mom{mom} ma{ma}",
                                    {"mode": "rot", "pool": pool_name, "mom": mom, "ma": ma,
                                     "top_k": 2, "weekly": True, "lag": 1}))
        # 波动缩放（风险敞口 50% / 70%）
        for rf in (0.5, 0.7):
            w, _ = rotation_weights(closes, pool, mom_window=120, vol_window=60,
                                    ma_window=60, weekly=True, top_k=1,
                                    buffer_pct=0.10, risk_fraction=rf, lag=1)
            results.append(evaluate(w, closes, f"轮动+敞口{int(rf*100)}% {pool_name}",
                                    {"mode": "rot", "pool": pool_name, "risk_fraction": rf,
                                     "top_k": 1, "lag": 1}))

    # ---------- 单资产均线择时 ----------
    for asset in ["sz399006", "sh000300", "sh000015", "sh518880", "sz159915", "sh510880"]:
        closes = closes_of([asset])
        for ma in (60, 120, 200):
            w = ma_gate_weights(closes, asset, ma)
            results.append(evaluate(w, closes, f"均线择时 {NAMES.get(asset, asset)} MA{ma}",
                                    {"mode": "ma_gate", "asset": asset, "ma": ma}))

    out = Path("reports/b_sweep_results.json")
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")

    # 排序展示
    def show(title, rows, n=12):
        print(f"\n===== {title} =====")
        print(f"{'策略':<52}{'收益':>8}{'年化':>7}{'回撤':>8}{'Sharpe':>8}{'月胜率':>8}{'近1年':>8}{'6月来':>8}{'暴露':>7}")
        for r in rows[:n]:
            print(f"{r['label'][:50]:<52}{r['total_return']:>8}{str(r['cagr']):>7}{r['max_dd']:>8}"
                  f"{str(r['sharpe']):>8}{str(r['monthly_win']):>8}{str(r['y1']):>8}"
                  f"{str(r['since_jun']):>8}{str(r['exposure']):>7}")

    strats = [r for r in results if r["cfg"].get("mode") != "buy_hold"]
    show("按 Sharpe 排序", sorted(strats, key=lambda r: (r["sharpe"] or -9), reverse=True))
    show("按总收益排序", sorted(strats, key=lambda r: (r["total_return"] or -999), reverse=True))
    show("基准：各资产买入持有", [r for r in results if r["cfg"].get("mode") == "buy_hold"], 6)
    print(f"\n共 {len(results)} 组实验，已写入 {out}")


if __name__ == "__main__":
    main()
