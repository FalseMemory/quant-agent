"""
Engine: fetches data, runs both strategies, produces JSON-ready result dicts.
All heavy lifting cached in-process; recompute() is called by the API on demand
or when params change.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import data_feed as dfd
from .strategies import cycle_rotation_a
from .settings_store import (
    DEFAULT_A_ASSETS, DEFAULT_B_ASSETS, DEFAULT_B_SAFE_ASSET, DEFAULT_C_ASSETS,
)
from .backtest import run_multi_asset, metrics, slice_window, renorm

WINDOWS = ["full", "3y", "1y", "since_jun"]
WINDOW_LABELS = {"full": "全历史", "3y": "近3年", "1y": "近1年", "since_jun": "2026-06以来"}

# 每次构建后保留的原始净值曲线（未降采样），供 /api/custom 自定义区间计算。
# 结构: {sid: {"index": DatetimeIndex, "equity": Series, "bench": Series(已归一化),
#              "weights": Series|None, "picks": Series|None, "bench_name": str}}
RAW: dict[str, dict] = {}

BENCH_NAMES = {"A": "TQQQ 买入持有", "B": "沪深300 买入持有", "C": "7200.HK 买入持有"}


def _store_raw(sid: str, bt: pd.DataFrame, bench: pd.Series | None = None,
               weights: pd.Series | None = None, picks: pd.Series | None = None,
               asset_label: str | None = None,
               benchmark_curves: dict[str, pd.Series] | None = None) -> None:
    """保留策略的完整净值曲线、基准与逐日持仓，供自定义起点计算与悬浮提示。"""
    RAW[sid] = {
        "index": bt.index,
        "equity": bt["equity"],
        "bench": bench if bench is not None else bt["bench_equity"],
        "weights": weights,
        "picks": picks,
        "asset_label": asset_label,
        "benchmark_curves": benchmark_curves or {},
        "bench_name": BENCH_NAMES.get(sid, "基准"),
    }


def _positions_series(weights: pd.Series | None, picks: pd.Series | None,
                      index: pd.DatetimeIndex, asset_label: str | None = None) -> list[dict]:
    """把逐日目标持仓对齐到曲线日期；百分比同时给出风险资产与现金仓位。"""
    out = []
    w = weights.reindex(index).ffill().fillna(0.0) if weights is not None else None
    p = picks.reindex(index).ffill() if picks is not None else None
    for ts in index:
        raw_weight = float(w.loc[ts]) if w is not None and pd.notna(w.loc[ts]) else 0.0
        pct = int(round(min(max(raw_weight, 0.0), 1.0) * 100))
        cash_pct = 100 - pct
        name = p.loc[ts] if p is not None and pd.notna(p.loc[ts]) else None
        asset = name if isinstance(name, str) and name else (asset_label or "现金")
        items = []
        if pct > 0 and asset != "现金":
            items.append({"asset": asset, "weight": pct})
        if cash_pct > 0:
            items.append({"asset": "现金", "weight": cash_pct})
        out.append({
            "asset": asset if pct > 0 and asset != "现金" else "现金",
            "weight": pct,
            "cash_weight": cash_pct,
            "items": items or [{"asset": "现金", "weight": 100}],
        })
    return out


def _positions_at(raw: dict, index: pd.DatetimeIndex) -> list[dict]:
    return _positions_series(raw.get("weights"), raw.get("picks"), index,
                             raw.get("asset_label"))


def _benchmark_curve_payload(curves: dict[str, pd.Series] | None,
                             index: pd.DatetimeIndex, step: int) -> list[dict]:
    """将多条买入持有曲线对齐到窗口起点并按图表步长降采样。"""
    out = []
    for name, series in (curves or {}).items():
        aligned = series.reindex(index).ffill().dropna()
        if len(aligned) != len(index) or len(aligned) < 2:
            continue
        normalized = aligned / aligned.iloc[0]
        out.append({"name": name, "values": [round(float(v), 4) for v in normalized.iloc[::step]]})
    return out


def custom_window(sid: str, start: str) -> dict:
    """从指定日期起算的策略 vs 基准对比（起点归一为 1）。

    返回与 _curve_payload 同构的 payload，前端可作为第五个窗口直接渲染。
    """
    raw = RAW.get(sid)
    if raw is None:
        raise ValueError(f"策略 {sid} 尚未构建，请先刷新数据")
    try:
        ts = pd.Timestamp(start)
    except (TypeError, ValueError) as exc:
        raise ValueError("起始日期格式无效") from exc
    if pd.isna(ts):
        raise ValueError("起始日期格式无效")

    idx, equity, bench = raw["index"], raw["equity"], raw["bench"]
    mask = idx >= ts.normalize()
    if int(mask.sum()) < 2:
        raise ValueError(f"起始日期 {start} 晚于或过于接近数据末尾（数据截至 {idx[-1].date()}）")

    seg_idx = idx[mask]
    seg_eq = equity[mask]
    seg_eq = seg_eq / seg_eq.iloc[0]
    bench_eq = bench.reindex(seg_idx).dropna()
    if len(bench_eq) < 2:
        raise ValueError("该区间基准数据不足")
    bench_eq = bench_eq / bench_eq.iloc[0]
    seg_eq = seg_eq.reindex(bench_eq.index)
    seg_dd = seg_eq / seg_eq.cummax() - 1

    m_strat = metrics(seg_eq, seg_eq.pct_change().fillna(0))
    m_bench = metrics(bench_eq, bench_eq.pct_change().fillna(0))
    step = max(1, len(seg_eq) // 400)
    return {
        "window": "custom", "label": f"自定义({seg_idx[0].date()}起)",
        "start": str(seg_idx[0].date()), "end": str(seg_idx[-1].date()),
        "strategy": m_strat, "benchmark": m_bench,
        "beats_benchmark": (m_strat.get("total_return") or -999) > (m_bench.get("total_return") or 999),
        "bench_name": raw.get("bench_name", "基准"),
        "dates": [str(d.date()) for d in bench_eq.index[::step]],
        "equity": [round(float(v), 4) for v in seg_eq[::step]],
        "bench_equity": [round(float(v), 4) for v in bench_eq[::step]],
        "benchmark_curves": _benchmark_curve_payload(
            raw.get("benchmark_curves"), bench_eq.index, step),
        "drawdown": [round(float(v) * 100, 2) for v in seg_dd[::step]],
        "positions": _positions_at(raw, bench_eq.index)[::step],
    }


def _curve_payload(bt: pd.DataFrame, window: str, *, weights: pd.Series | None = None,
                   picks: pd.Series | None = None, asset_label: str | None = None,
                   bench_name: str | None = None,
                   benchmark_curves: dict[str, pd.Series] | None = None) -> dict:
    seg = renorm(slice_window(bt, window))
    m_strat = metrics(seg["equity"], seg["ret"], seg["weight"])
    m_bench = metrics(seg["bench_equity"], seg["bench_equity"].pct_change().fillna(0))
    step = max(1, len(seg) // 400)
    return {
        "window": window,
        "label": WINDOW_LABELS[window],
        "start": str(seg.index[0].date()),
        "end": str(seg.index[-1].date()),
        "strategy": m_strat,
        "benchmark": m_bench,
        "beats_benchmark": (m_strat.get("total_return") or -999) > (m_bench.get("total_return") or 999),
        "bench_name": bench_name or "基准",
        # downsample curves for the chart (max ~400 points)
        "dates": [str(d.date() if hasattr(d, "date") else d) for d in seg.index[::step]],
        "equity": [round(float(v), 4) for v in seg["equity"][::step]],
        "bench_equity": [round(float(v), 4) for v in seg["bench_equity"][::step]],
        "benchmark_curves": _benchmark_curve_payload(benchmark_curves, seg.index, step),
        "drawdown": [round(float(v) * 100, 2) for v in seg["dd"][::step]],
        "positions": _positions_series(weights, picks, seg.index, asset_label)[::step],
    }


def _apply_exposure_hysteresis(pick: str, desired: float, previous_pick: str | None,
                               previous_exposure: float, min_change: float) -> float:
    change = abs(desired - previous_exposure)
    if pick == previous_pick and change < min_change and not np.isclose(change, min_change):
        return previous_exposure
    return desired


def _build_dynamic_vol_pool(sid: str, params: dict | None, *, market: str,
                            strategy_name: str, default_assets: list[dict],
                            core_code: str) -> dict:
    p = {"target_vol": 0.35, "trend_window": 200, "assets": default_assets,
         "vol_window": 40, "momentum_window": 60, "min_change": 0.10,
         "cost_rate": 0.0015}
    if params:
        p.update(params)

    meta_by_code = {item["code"]: dict(item) for item in p["assets"]}
    frames, failed_assets = {}, []
    min_history = max(p["trend_window"], p["momentum_window"], p["vol_window"]) + 5
    for code, meta in meta_by_code.items():
        try:
            frame = dfd.get_us(code, "1d", "5y")
            if "close" not in frame or len(frame) < min_history:
                raise ValueError("历史行情长度不足")
            frames[code] = frame
        except Exception as exc:  # noqa: BLE001 — isolate one user asset failure
            failed_assets.append({"code": code, "name": meta["name"], "error": str(exc)[:120]})
    available = [code for code in meta_by_code if code in frames]
    if not available:
        raise ValueError(f"用户ETF池没有可用行情，无法生成策略{sid}推荐")

    closes = pd.concat({code: frames[code]["close"] for code in available}, axis=1).sort_index()
    closes = closes.ffill().dropna()
    if len(closes) < min_history:
        raise ValueError(f"策略{sid}有效候选的共同历史行情长度不足")
    returns = closes.pct_change()
    vol20 = returns.rolling(20).std() * np.sqrt(252)
    vol40 = returns.rolling(40).std() * np.sqrt(252)
    momentum = closes.pct_change(p["momentum_window"]) / returns.rolling(
        p["momentum_window"]).std() * np.sqrt(252)
    above_ma = closes > closes.rolling(p["trend_window"]).mean()
    eligible_scores = momentum.where(above_ma & momentum.notna())

    weights = pd.DataFrame(0.0, index=closes.index, columns=closes.columns)
    picked_codes = pd.Series(index=closes.index, dtype=object)
    previous_pick, previous_exposure = None, 0.0
    for date in closes.index:
        scores = eligible_scores.loc[date].dropna()
        if scores.empty:
            previous_pick, previous_exposure = None, 0.0
            continue
        pick = str(scores.idxmax())
        realized = vol40.at[date, pick]
        desired = float(np.clip(p["target_vol"] / realized, 0.0, 1.0)) if np.isfinite(realized) and realized > 0 else 0.0
        exposure = _apply_exposure_hysteresis(
            pick, desired, previous_pick, previous_exposure, p["min_change"])
        weights.at[date, pick] = exposure
        picked_codes.at[date] = pick
        previous_pick, previous_exposure = pick, exposure

    bt = run_multi_asset(weights, closes, cost_rate=p["cost_rate"])
    benchmark_code = core_code if core_code in available else available[0]
    benchmark = closes[benchmark_code].reindex(bt.index).ffill()
    bt["bench_equity"] = benchmark / benchmark.iloc[0]
    bt["dd_bench"] = bt["bench_equity"] / bt["bench_equity"].cummax() - 1
    BENCH_NAMES[sid] = f"{benchmark_code} 买入持有"

    label_of = lambda code: f"{code} {meta_by_code[code]['name']}"
    total_weight = weights.sum(axis=1).reindex(bt.index).fillna(0.0)
    picked_labels = picked_codes.reindex(bt.index).map(
        lambda code: label_of(code) if code in meta_by_code else None)
    benchmark_curves = {
        label_of(code): closes[code].reindex(bt.index).ffill() for code in available
    }
    _store_raw(sid, bt, weights=total_weight, picks=picked_labels,
               benchmark_curves=benchmark_curves)

    last_date = closes.index[-1]
    pool = [
        {**meta_by_code[code], "etf": label_of(code),
         "above_ma": bool(above_ma.at[last_date, code]),
         "momentum_score": round(float(momentum.at[last_date, code]), 4)
         if np.isfinite(momentum.at[last_date, code]) else None,
         "as_of": str(last_date.date())}
        for code in available
    ]
    current_pick = picked_codes.iloc[-1] if pd.notna(picked_codes.iloc[-1]) else None
    current_exposure = float(total_weight.iloc[-1])
    current_vol = vol40.at[last_date, current_pick] if current_pick else np.nan
    out = {"id": sid, "name": strategy_name, "market": market,
           "freq_label": "日级信号 · 次日执行", "windows": {}, "pool": pool,
           "failed_assets": failed_assets, "pool_limit": 10,
           "assets": {code: meta_by_code[code]["name"] for code in available}}
    for win in WINDOWS:
        out["windows"][win] = _curve_payload(
            bt, win, weights=total_weight, picks=picked_labels,
            bench_name=BENCH_NAMES[sid], benchmark_curves=benchmark_curves)
    out["current"] = {
        "pick": current_pick or "现金",
        "pick_name": meta_by_code[current_pick]["name"] if current_pick else "现金",
        "etf": label_of(current_pick) if current_pick else "现金",
        "exposure": round(current_exposure * 100),
        "realized_vol": round(float(current_vol) * 100, 1) if np.isfinite(current_vol) else None,
        "realized_vol_20": round(float(vol20.at[last_date, current_pick]) * 100, 1)
        if current_pick and np.isfinite(vol20.at[last_date, current_pick]) else None,
        "target_vol": round(float(p["target_vol"]) * 100, 1),
        "trend_gate_on": bool(current_pick), "as_of": str(last_date.date()),
        "trend_window": p["trend_window"],
    }
    return out


def build_strategy_a(params: dict | None = None) -> dict:
    return _build_dynamic_vol_pool(
        "A", params, market="美股", strategy_name="波动红利 · 美股动态 ETF 池",
        default_assets=DEFAULT_A_ASSETS, core_code="TQQQ")


def build_strategy_c(params: dict | None = None) -> dict:
    return _build_dynamic_vol_pool(
        "C", params, market="港股", strategy_name="波动红利 · 港股动态 ETF 池",
        default_assets=DEFAULT_C_ASSETS, core_code="7200.HK")


def build_strategy_b(params: dict | None = None) -> dict:
    p = {"mom_window": 180, "vol_window": 60, "ma_window": 60, "weekly": True,
         "buffer_pct": 0.10, "cost_rate": 0.0015,
         "assets": DEFAULT_B_ASSETS, "safe_asset": DEFAULT_B_SAFE_ASSET}
    if params:
        p.update(params)

    risky_meta = {item["code"]: dict(item) for item in p["assets"]}
    safe_meta = dict(p["safe_asset"])
    all_meta = {**risky_meta, safe_meta["code"]: safe_meta}
    frames, failed_assets = {}, []
    for code, meta in all_meta.items():
        try:
            frame = dfd.get_a(code, "20220601")
            if len(frame) < max(p["mom_window"], p["ma_window"], p["vol_window"]) + 5:
                raise ValueError("历史行情长度不足")
            frames[code] = frame
        except Exception as exc:  # noqa: BLE001 — isolate one user asset failure
            if code == safe_meta["code"]:
                raise ValueError(f"防御ETF {code[-6:]} 行情不可用：{exc}") from exc
            failed_assets.append({"code": code, "name": meta["name"], "error": str(exc)[:120]})
    available_risky = [code for code in risky_meta if code in frames]
    if not available_risky:
        raise ValueError("用户ETF池没有可用行情，无法生成策略B推荐")

    bench_close = dfd.get_a("sh000300", "20220601")["close"]
    decisions = cycle_rotation_a(
        frames, risky_assets=available_risky, safe_asset=safe_meta["code"],
        mom_window=p["mom_window"], vol_window=p["vol_window"],
        ma_window=p["ma_window"], weekly=p["weekly"], buffer_pct=p["buffer_pct"],
    )

    closes = pd.DataFrame({c: f["close"] for c, f in frames.items()}).sort_index()
    weights = pd.DataFrame(0.0, index=closes.index, columns=closes.columns)
    for t, row in decisions.iterrows():
        pos = closes.index.searchsorted(t)
        exec_idx = closes.index[min(pos + 1, len(closes) - 1)]
        weights.loc[exec_idx:, :] = 0.0
        weights.loc[exec_idx:, row["pick"]] = 1.0
    bt = run_multi_asset(weights, closes, cost_rate=p["cost_rate"])
    hs300_full = bench_close.reindex(bt.index).ffill().dropna()
    bt = bt.loc[hs300_full.index].copy()
    bt["bench_equity"] = hs300_full / hs300_full.iloc[0]
    bt["dd_bench"] = bt["bench_equity"] / bt["bench_equity"].cummax() - 1

    label_of = lambda code: f"{code[-6:]} {all_meta[code]['name']}"
    total_weight = weights.sum(axis=1).reindex(bt.index).ffill().fillna(0.0)
    picked_code = weights.idxmax(axis=1).reindex(bt.index).ffill().where(total_weight > 0)
    picked_label = picked_code.map(lambda code: label_of(code) if code in all_meta else None)
    benchmark_curves = {
        label_of(code): closes[code].reindex(bt.index).ffill()
        for code in available_risky
    }
    _store_raw("B", bt, weights=total_weight, picks=picked_label,
               benchmark_curves=benchmark_curves)

    last_date = closes.index[-1]
    score_now = closes.pct_change(p["mom_window"]).iloc[-1] / closes.pct_change().rolling(
        p["vol_window"]).std().iloc[-1] * np.sqrt(252)
    ma_now = closes.rolling(p["ma_window"]).mean().iloc[-1]
    pool = []
    for code in available_risky:
        pool.append({**risky_meta[code], "etf": label_of(code),
                     "above_ma": bool(closes[code].iloc[-1] > ma_now[code]),
                     "momentum_score": round(float(score_now[code]), 4) if np.isfinite(score_now[code]) else None,
                     "as_of": str(last_date.date())})

    out = {"id": "B", "name": "周期红利 · A股周度轮动", "market": "A股",
           "freq_label": "周日级（每周五收盘决策）", "windows": {},
           "assets": {c: all_meta[c]["name"] for c in frames}, "pool": pool,
           "safe_asset": {**safe_meta, "etf": label_of(safe_meta["code"])},
           "failed_assets": failed_assets, "pool_limit": 10}
    for win in WINDOWS:
        out["windows"][win] = _curve_payload(
            bt, win, weights=total_weight, picks=picked_label,
            bench_name=BENCH_NAMES["B"], benchmark_curves=benchmark_curves)

    last_dec = decisions.iloc[-1]
    pick = str(last_dec["pick"])
    out["current"] = {
        "pick": pick, "pick_name": all_meta[pick]["name"], "etf": label_of(pick),
        "reason": str(last_dec["reason"]), "decision_date": str(decisions.index[-1].date()),
        "as_of": str(last_date.date()), "ma_window": p["ma_window"],
        "mom_window": p["mom_window"],
    }
    recent = decisions.tail(12).iloc[::-1]
    out["recent_picks"] = [
        {"date": str(t.date()), "pick": all_meta[r["pick"]]["name"], "reason": r["reason"]}
        for t, r in recent.iterrows()
    ]
    return out


def build_all(params_a: dict | None = None, params_b: dict | None = None,
              params_c: dict | None = None) -> dict:
    return {
        "A": build_strategy_a(params_a),
        "B": build_strategy_b(params_b),
        "C": build_strategy_c(params_c),
    }
