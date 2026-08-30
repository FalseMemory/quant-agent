"""Offline research lab: reproduce production baselines from cached CSVs, then
run one-at-a-time parameter/feature experiments and quantify each change.

Usage:
    python lab_optimize.py            # run all experiments, dump JSON + print tables
Data: data_cache/*.csv (no network). Baselines must match /api/summary numbers.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from backend.strategies import vol_target_tqqq, cycle_rotation_a, ASSETS_B, RISKY_B, SAFE_B  # noqa: E402
from backend.backtest import run_weight_backtest, run_multi_asset, metrics, slice_window  # noqa: E402

DATA = ROOT / "data_cache"
REPORT_DIR = ROOT / "reports"
WINDOWS = ["full", "3y", "1y", "since_jun"]


# ---------------------------------------------------------------------------
# Data loading (direct from cache; same files the engine serves)
# ---------------------------------------------------------------------------
def load_csv(name: str) -> pd.DataFrame:
    df = pd.read_csv(DATA / name, index_col=0)
    df.index = pd.to_datetime(df.index).normalize()
    return df.sort_index()


QQQ = load_csv("us_QQQ_1d_5y.csv")
TQQQ = load_csv("us_TQQQ_1d_5y.csv")


def load_a() -> dict[str, pd.DataFrame]:
    out = {}
    for code in ASSETS_B:
        df = load_csv(f"a_{code}_20220601_now.csv")
        # engine calls get_a(code,'20220601') whose source starts 2022-06; keep as-is
        out[code] = df
    return out


FRAMES_A_CN = load_a()


# ---------------------------------------------------------------------------
# Generic signal builders (superset of strategies.py; defaults == baseline)
# ---------------------------------------------------------------------------
def wt_signal_a(
    qqq_close: pd.Series,
    *,
    vol_window: int = 20,
    target_vol: float = 0.35,
    trend_window: int = 200,
    min_change: float = 0.10,
    max_leverage: float = 1.0,
    gate_mode: str = "sma",        # sma | dual | none
    conf_window: int = 50,
    vol_cut_mult: float | None = None,   # extra de-lever when rv > thr
    vol_cut_thr: float = 0.45,
    stop_dd: float | None = None,        # trailing stop on QQQ close
    rec_window: int = 50,
    dd_soft: tuple | None = None,        # (thr, mult) scale w by mult when drawdown-from-high < -thr
    dd_hard: float | None = None,        # flat when drawdown-from-high < -thr
    mom_exit: tuple | None = None,       # (n_days, thr) flat when ret_n < -thr; revive on close>SMA(rec_window)
    volpct_cap: tuple | None = None,     # (pct_lookback_days, pctile, cap) rv percentile cap
) -> pd.Series:
    rets = qqq_close.pct_change()
    rv = rets.rolling(vol_window).std() * np.sqrt(252)

    def _sma(w):
        return qqq_close.rolling(w, min_periods=max(20, w // 4)).mean()

    if gate_mode == "none":
        gate_s = pd.Series(1.0, index=qqq_close.index)
    elif gate_mode == "dual":
        gate_s = (qqq_close > _sma(trend_window)) & (_sma(conf_window) > _sma(trend_window))
    else:
        gate_s = qqq_close > _sma(trend_window)

    raw = (target_vol / rv.replace(0, np.nan)).clip(upper=max_leverage).fillna(0.0)
    if vol_cut_mult is not None:
        hot = rv > vol_cut_thr
        raw = raw.where(~hot, raw * vol_cut_mult)

    if dd_soft is not None or dd_hard is not None:
        high = qqq_close.rolling(252, min_periods=60).max()
        ddh = qqq_close / high - 1
        if dd_soft is not None:
            thr, mult = dd_soft
            raw = raw.where(ddh > -thr, raw * mult)
        if dd_hard is not None:
            raw = raw.where(ddh > -dd_hard, 0.0)

    if volpct_cap is not None:
        lb, pct, cap = volpct_cap
        prc = rv.rolling(lb, min_periods=60).rank(pct=True)
        raw = raw.where(prc <= pct, raw.clip(upper=cap))

    w_pre = (raw * gate_s.astype(float)).fillna(0.0)

    if mom_exit is not None:
        n, thr = mom_exit
        sma_rec2 = _sma(rec_window)
        mom_n = qqq_close.pct_change(n)
        exit_sig = (mom_n < -thr).values
        w_vals = w_pre.to_numpy(copy=True)
        px_v2 = qqq_close.values
        cur_off = False
        for i in range(len(w_vals)):
            if not cur_off and exit_sig[i]:
                cur_off = True
            elif cur_off and px_v2[i] > sma_rec2.iloc[i]:
                cur_off = False
            if cur_off:
                w_vals[i] = 0.0
        w_pre = pd.Series(w_vals, index=w_pre.index)

    if stop_dd is not None:
        sma_rec = _sma(rec_window)
        rolling_high = qqq_close.rolling(252, min_periods=60).max()
        dd = qqq_close / rolling_high - 1
        stopped = np.zeros(len(qqq_close), dtype=bool)
        cur = False
        px_v = qqq_close.values
        for i in range(len(px_v)):
            if not cur and dd.iloc[i] < -stop_dd:
                cur = True
            elif cur and px_v[i] > sma_rec.iloc[i]:
                cur = False
            stopped[i] = cur
        w_pre = w_pre * (~pd.Series(stopped, index=qqq_close.index)).astype(float)

    out = np.zeros(len(w_pre))
    cur_w = 0.0
    for i, v in enumerate(w_pre.values):
        if abs(v - cur_w) >= min_change:
            cur_w = float(v)
        out[i] = cur_w
    return pd.Series(out, index=w_pre.index, name="weight")


def rot_signal_b(
    frames: dict[str, pd.DataFrame],
    *,
    mom_window: int = 60,
    vol_window: int = 60,
    ma_window: int = 40,
    weekly: bool = True,
    top_k: int = 1,
    buffer_pct: float = 0.0,     # keep prev pick unless challenger beats by this margin
    risk_fraction: float = 1.0,  # total risky allocation; rest -> bond ETF
):
    """Return (weights_df, decisions_df). weights decided at close t, executed t+1."""
    closes = pd.DataFrame({c: f["close"] for c, f in frames.items()}).sort_index()
    rets = closes.pct_change()
    score = closes.pct_change(mom_window) / rets.rolling(vol_window).std() * np.sqrt(252)
    above_ma = closes > closes.rolling(ma_window).mean()

    if weekly:
        key = closes.index.to_period("W")
        is_last = pd.Series(key, index=closes.index) != pd.Series(key, index=closes.index).shift(-1)
        dec_idx = closes.index[is_last.fillna(False)]
    else:
        dec_idx = closes.index

    rows = []
    picks_by_date = {}
    prev_pick: list[str] = []
    for t in dec_idx:
        s = score.loc[t]
        ok = above_ma.loc[t]
        cand = [c for c in RISKY_B if bool(ok.get(c, False)) and np.isfinite(s.get(c, np.nan))]
        # rank by score desc
        cand.sort(key=lambda c: s[c], reverse=True)
        chosen: list[str] = []
        if buffer_pct <= 0:
            chosen = cand[:top_k]
        else:
            # buffer rule: incumbent keeps seat unless a challenger outscores it by margin
            inc = [c for c in prev_pick if c in cand]
            chal = [c for c in cand if c not in prev_pick]
            merged: list[str] = []
            for c in inc:
                if len(merged) < top_k:
                    merged.append(c)
            for c in chal:
                if len(merged) >= top_k:
                    break
                weakest = merged[-1] if merged else None
                if weakest is None or s[c] > s[weakest] * (1 + buffer_pct):
                    merged.append(c)
                    merged.sort(key=lambda c: s[c], reverse=True)
                    merged = merged[:top_k]
            chosen = merged
        if not chosen:
            pick_label, w_row = SAFE_B, {SAFE_B: 1.0}
            reason = "风险资产趋势走弱，转入防御"
        else:
            k = len(chosen)
            w_row = {c: risk_fraction / k for c in chosen}
            if risk_fraction < 1.0:
                w_row[SAFE_B] = w_row.get(SAFE_B, 0.0) + (1.0 - risk_fraction)
            pick_label = "+".join(chosen)
            reason = "动量最优且在均线上方" if buffer_pct <= 0 else "动量排序(含在位缓冲)"
        rows.append({"date": t, "pick": pick_label, "weight": risk_fraction, "reason": reason})
        picks_by_date[t] = w_row
        prev_pick = chosen

    decisions = pd.DataFrame(rows).set_index("date")

    weights = pd.DataFrame(0.0, index=closes.index, columns=list(closes.columns))
    for t, wrow in picks_by_date.items():
        pos = closes.index.searchsorted(t)
        exec_idx = closes.index[min(pos + 1, len(closes) - 1)]
        # BUGFIX: 每次切换必须先清空全部列，否则历史持仓会叠加成数倍杠杆
        # （与 engine.build_strategy_b 同一处缺陷，2026-08-29 修复）
        weights.loc[exec_idx:, :] = 0.0
        for c, v in wrow.items():
            weights.loc[exec_idx:, c] = v
    return weights, decisions


# ---------------------------------------------------------------------------
# Evaluation helpers
# ---------------------------------------------------------------------------
def period_stats(strat_ret: pd.Series, bench_ret: pd.Series, code: str) -> dict:
    df = pd.concat([strat_ret, bench_ret], axis=1, keys=["s", "b"]).dropna()
    per = df.index.to_period(code)
    gs = (1 + df["s"]).groupby(per).prod() - 1
    gb = (1 + df["b"]).groupby(per).prod() - 1
    ex = gs - gb
    up = gb > 0
    res = {
        "n": int(len(ex)),
        "win_rate": round(float((ex > 0).mean()) * 100, 1),
        "avg_excess": round(float(ex.mean()) * 100, 2),
        "med_excess": round(float(ex.median()) * 100, 2),
        "bench_up_win": round(float((ex[up] > 0).mean()) * 100, 1) if up.sum() else None,
        "bench_down_win": round(float((ex[~up] > 0).mean()) * 100, 1) if (~up).sum() else None,
        "down_capture": round(float(gs[~up].sum() / gb[~up].sum()) * 100, 1) if (~up).sum() and gb[~up].sum() != 0 else None,
        "up_capture": round(float(gs[up].sum() / gb[up].sum()) * 100, 1) if (up).sum() and gb[up].sum() != 0 else None,
        "worst_excess": round(float(ex.min()) * 100, 2),
        "best_excess": round(float(ex.max()) * 100, 2),
    }
    return res


def yearly_table(strat_ret: pd.Series, bench_ret: pd.Series) -> list[dict]:
    df = pd.concat([strat_ret, bench_ret], axis=1, keys=["s", "b"]).dropna()
    per = df.index.year
    gs = (1 + df["s"]).groupby(per).prod() - 1
    gb = (1 + df["b"]).groupby(per).prod() - 1
    return [{"year": int(y), "strat": round(float(s) * 100, 1), "tqqq": round(float(b) * 100, 1),
             "win": bool(s > b)} for y, s, b in zip(gs.index, gs.values, gb.values)]


def window_pack(bt: pd.DataFrame, bench_series: pd.Series | None = None) -> dict:
    """bt from run_weight_backtest; bench_series overrides benchmark returns."""
    pack = {}
    for win in WINDOWS:
        seg = slice_window(bt, win)
        m = metrics(seg["equity"], seg["ret"], seg["weight"])
        if bench_series is None:
            b_rets = seg["bench_equity"].pct_change().fillna(0)
            b_eq = seg["bench_equity"]
        else:
            b_eq = bench_series.reindex(seg.index).dropna()
            b_rets = b_eq.pct_change().fillna(0)
        mb = metrics(b_eq, b_rets)
        pack[win] = {"strategy": m, "benchmark": mb}
    return pack


def eval_weight_bt(w: pd.Series, px: pd.Series, *, cost_rate: float = 0.0015,
                   bench_px: pd.Series | None = None) -> dict:
    bt = run_weight_backtest(w, px, cost_rate=cost_rate)
    bench_rets = None
    if bench_px is not None:
        bp = bench_px.reindex(px.index).ffill()
        bench_rets = bp.pct_change().fillna(0)
    b_rets = bench_rets if bench_rets is not None else bt["bench_equity"].pct_change().fillna(0)
    out = {
        "windows": window_pack(bt),
        "monthly": period_stats(bt["ret"], b_rets, "M"),
        "quarterly": period_stats(bt["ret"], b_rets, "Q"),
        "yearly": yearly_table(bt["ret"], b_rets),
        "turnover_bp_day": round(float(w.diff().abs().fillna(0).mean()) * 10000, 1),
    }
    return out, bt


def eval_multi_bt(weights: pd.DataFrame, closes: pd.DataFrame, *, cost_rate: float = 0.0015,
                  bench_code: str = "sh000300") -> dict:
    bt = run_multi_asset(weights, closes, cost_rate=cost_rate)
    bench = closes[bench_code].reindex(closes.index).dropna()
    bench_rets = bench.pct_change().fillna(0)
    strat_rets = bt["ret"]
    common = strat_rets.index.intersection(bench_rets.index)
    out = {
        "windows": window_pack(bt, bench),
        "monthly": period_stats(strat_rets.reindex(common), bench_rets.reindex(common), "M"),
        "quarterly": period_stats(strat_rets.reindex(common), bench_rets.reindex(common), "Q"),
        "yearly": yearly_table(strat_rets.reindex(common), bench_rets.reindex(common)),
        "turnover_bp_day": round(float(weights.diff().abs().sum(axis=1).fillna(0).mean()) * 10000, 1),
    }
    return out, bt


# ---------------------------------------------------------------------------
# Experiment runner
# ---------------------------------------------------------------------------
RESULTS: list[dict] = []


def record(group: str, exp_id: str, desc: str, params: dict, ev: dict):
    row = {
        "group": group, "exp": exp_id, "desc": desc, "params": params,
        **{f"{w}_strategy": v for w in WINDOWS for v in (ev["windows"][w]["strategy"],)},
        **{f"{w}_{k}": v for w in WINDOWS for k, v in ev["windows"][w]["strategy"].items()},
        "full_bench_total": ev["windows"]["full"]["benchmark"]["total_return"],
        "3y_bench_total": ev["windows"]["3y"]["benchmark"]["total_return"],
        "1y_bench_total": ev["windows"]["1y"]["benchmark"]["total_return"],
        "monthly": ev["monthly"], "quarterly": ev["quarterly"],
        "yearly": ev["yearly"], "turnover_bp_day": ev["turnover_bp_day"],
    }
    RESULTS.append(row)
    return row


def print_table(rows: list[dict], title: str, bench_col: str = "full_bench_total"):
    print(f"\n=== {title} ===")
    hdr = (f"{'exp':22s} {'full%':>8s} {'3y%':>8s} {'1y%':>8s} {'ddFull%':>8s} "
           f"{'shrpF':>6s} {'calF':>6s} {'Mwin%':>6s} {'Qwin%':>6s} {'dnCap%':>7s} {'toBP/d':>7s}")
    print(hdr)
    for r in rows:
        f = r["full_strategy"]; y3 = r["3y_strategy"]; y1 = r["1y_strategy"]
        print(f"{r['exp']:22s} {f['total_return']:>8} {y3['total_return']:>8} {y1['total_return']:>8} "
              f"{f['max_dd']:>8} {f['sharpe']:>6} {f['calmar']:>6} "
              f"{r['monthly']['win_rate']:>6} {r['quarterly']['win_rate']:>6} "
              f"{r['monthly']['down_capture']:>7} {r['turnover_bp_day']:>7}")


# ---------------------------------------------------------------------------
# Strategy A experiments (US: trade TQQQ on QQQ signals; benchmark TQQQ B&H)
# ---------------------------------------------------------------------------
def run_group_a():
    px = TQQQ["close"]
    sig = QQQ["close"]
    base_params = dict(vol_window=20, target_vol=0.35, trend_window=200, min_change=0.10)

    def ev_of(params, tag, desc, cost_rate=0.0015):
        w = wt_signal_a(sig, **params)
        ev, bt = eval_weight_bt(w, px, cost_rate=cost_rate, bench_px=px)
        record("A", tag, desc, params, ev)
        return w, bt

    # E0 baseline (must equal live panel)
    w0, bt0 = ev_of(base_params, "A0_baseline", "现行基线(与线上一致)")
    print_table([RESULTS[-1]], "A baseline check vs live panel:")
    ref = json.load(open(ROOT / "_summary_ref.json", encoding="utf-8"))
    live = ref["A"]["windows"]
    ok = all(abs(RESULTS[-1][f"{w}_strategy"]["total_return"] - live[w]["strategy"]["total_return"]) < 0.15
             for w in WINDOWS)
    print("baseline match live:", "PASS" if ok else "FAIL",
          {w: (RESULTS[-1][f"{w}_strategy"]["total_return"], live[w]["strategy"]["total_return"]) for w in WINDOWS})

    exps = []
    for tv in (0.30, 0.40, 0.45, 0.50):
        exps.append((dict(base_params, target_vol=tv), f"A_tv{int(tv*100)}", f"目标波动→{tv:.0%}"))
    for vw in (10, 30, 40, 60):
        exps.append((dict(base_params, vol_window=vw), f"A_vw{vw}", f"波动窗口→{vw}日"))
    for tw in (100, 150, 250):
        exps.append((dict(base_params, trend_window=tw), f"A_tw{tw}", f"SMA闸门→{tw}"))
    exps.append((dict(base_params, gate_mode="dual", conf_window=50), "A_dual200_50", "双确认:SMA50>SMA200"))
    exps.append((dict(base_params, gate_mode="none"), "A_nogate", "取消趋势闸门"))
    for mc in (0.05, 0.20):
        exps.append((dict(base_params, min_change=mc), f"A_mc{int(mc*100)}", f"滞回带→±{mc:.0%}"))
    for mult, thr in ((0.5, 0.45), (0.5, 0.40), (0.65, 0.45)):
        exps.append((dict(base_params, vol_cut_mult=mult, vol_cut_thr=thr),
                     f"A_volcut{int(mult*100)}_{int(thr*100)}", f"高波降仓x{mult}(rv>{thr:.0%})"))
    for sd in (0.15, 0.20, 0.25):
        exps.append((dict(base_params, stop_dd=sd), f"A_stop{int(sd*100)}", f"移动止损−{sd:.0%}(SMA50复活)"))

    for params, tag, desc in exps:
        ev_of(params, tag, desc)

    # ---- round 2: differentiation overlays & upside enhancement ----
    exps2 = [
        (dict(base_params, max_leverage=1.15), "A_lv115", "满仓上限放宽至1.15x"),
        (dict(base_params, max_leverage=1.25), "A_lv125", "满仓上限放宽至1.25x"),
        (dict(base_params, dd_soft=(0.10, 0.5)), "A_dd10h", "距一年高点的回撤>10%→仓位减半"),
        (dict(base_params, dd_soft=(0.15, 0.5)), "A_dd15h", "回撤>15%→仓位减半"),
        (dict(base_params, dd_hard=0.20), "A_dd20f", "回撤>20%→清仓"),
        (dict(base_params, dd_soft=(0.10, 0.5), dd_hard=0.20), "A_ddtier", "分层风控:>10%减半,>20%清仓"),
        (dict(base_params, mom_exit=(20, 0.08)), "A_momexit8", "20日动量<-8%清仓,SMA50复活"),
        (dict(base_params, mom_exit=(20, 0.12)), "A_momexit12", "20日动量<-12%清仓,SMA50复活"),
        (dict(base_params, volpct_cap=(504, 0.8, 0.5)), "A_volp80", "rv两年80分位以上封顶0.5x"),
        (dict(base_params, vol_window=40, stop_dd=0.25), "A_vw40_stop25", "组合:vw40+25%移动止损"),
        (dict(base_params, vol_window=40, dd_soft=(0.10, 0.5), dd_hard=0.20), "A_vw40_ddtier", "组合:vw40+分层风控"),
        (dict(base_params, vol_window=40, mom_exit=(20, 0.12)), "A_vw40_mex12", "组合:vw40+动量急退12%"),
        (dict(base_params, vol_window=40, stop_dd=0.25, max_leverage=1.15), "A_vw40_st25_lv115", "组合:vw40+止损25%+上限1.15x"),
        (dict(base_params, vol_window=40, dd_soft=(0.10, 0.5), dd_hard=0.20, max_leverage=1.15), "A_vw40_dd_lv115", "组合:vw40+分层风控+上限1.15x"),
        (dict(base_params, target_vol=0.30, vol_window=40), "A_tv30_vw40", "组合:目标波动30%+vw40"),
    ]
    for params, tag, desc in exps2:
        ev_of(params, tag, desc)

    print_table([r for r in RESULTS if r["group"] == "A"], "Strategy A sweep (one-at-a-time)")
    return w0, bt0


# ---------------------------------------------------------------------------
# Strategy B experiments (A-share rotation; benchmark 沪深300)
# ---------------------------------------------------------------------------
def run_group_b():
    frames = FRAMES_A_CN
    closes = pd.DataFrame({c: f["close"] for c, f in frames.items()}).sort_index()
    base = dict(mom_window=60, vol_window=60, ma_window=40, weekly=True)

    def ev_of(params, tag, desc):
        weights, decisions = rot_signal_b(frames, **params)
        ev, bt = eval_multi_bt(weights, closes)
        record("B", tag, desc, params, ev)

    ev_of(base, "B0_baseline", "现行基线(周度·单选·全仓)")

    exps = []
    for mw in (20, 40, 90, 120):
        exps.append((dict(base, mom_window=mw), f"B_mom{mw}", f"动量窗口→{mw}日"))
    for ma in (20, 60, 100, 120):
        exps.append((dict(base, ma_window=ma), f"B_ma{ma}", f"均线门槛→MA{ma}"))
    exps.append((dict(base, weekly=False), "B_daily", "改为日度决策"))
    exps.append((dict(base, top_k=2), "B_top2", "前二等权(50/50)"))
    exps.append((dict(base, buffer_pct=0.10), "B_buffer10", "换仓缓冲10%(挑战者须优10%)"))
    exps.append((dict(base, buffer_pct=0.25), "B_buffer25", "换仓缓冲25%"))
    for rf in (0.70, 0.85):
        exps.append((dict(base, risk_fraction=rf), f"B_rf{int(rf*100)}", f"风险仓位上限→{rf:.0%}(余国债)"))

    for params, tag, desc in exps:
        ev_of(params, tag, desc)

    # ---- stage 2: mom×ma grid ----
    grid = []
    for mw in (20, 40, 60, 90, 120):
        for ma in (20, 40, 60):
            if (mw, ma) == (60, 40):
                continue  # baseline already done
            tag = f"B_g_m{mw}_a{ma}"
            ev_of(dict(base, mom_window=mw, ma_window=ma), tag, f"网格:动量{mw}/均线{ma}")
            grid.append(tag)

    # ---- stage 3: feature overlays on the grid winner (auto-selected) ----
    def score_row(r):
        return (r["monthly"]["win_rate"], r["full_strategy"]["sharpe"] or -9)

    cands = [r for r in RESULTS if r["group"] == "B" and r["exp"] in ("B0_baseline", *grid)]
    best = max(cands, key=score_row)
    best_p = dict(best["params"])
    print(f"\n[B] grid winner = {best['exp']} (Mwin={best['monthly']['win_rate']}%, "
          f"sharpe={best['full_strategy']['sharpe']}) -> applying overlays")
    ev_of(dict(best_p, top_k=2), "B_best_top2", "最优格+前二等权")
    ev_of(dict(best_p, buffer_pct=0.10), "B_best_buf10", "最优格+换仓缓冲10%")
    ev_of(dict(best_p, buffer_pct=0.25), "B_best_buf25", "最优格+换仓缓冲25%")
    ev_of(dict(best_p, risk_fraction=0.85), "B_best_rf85", "最优格+风险仓位85%")
    ev_of(dict(best_p, top_k=2, buffer_pct=0.10), "B_best_top2_buf", "最优格+前二+缓冲")
    ev_of(dict(best_p, top_k=2, risk_fraction=0.85), "B_best_top2_rf85", "最优格+前二+风险85%")

    print_table([r for r in RESULTS if r["group"] == "B"], "Strategy B sweep")


# ---------------------------------------------------------------------------
def main():
    REPORT_DIR.mkdir(exist_ok=True)
    run_group_a()
    run_group_b()
    with open(REPORT_DIR / "lab_results.json", "w", encoding="utf-8") as fh:
        json.dump(RESULTS, fh, ensure_ascii=False, indent=1, default=str)
    print(f"\nsaved -> reports/lab_results.json ({len(RESULTS)} experiments)")


if __name__ == "__main__":
    main()
