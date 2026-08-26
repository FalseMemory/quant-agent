"""
Engine: fetches data, runs both strategies, produces JSON-ready result dicts.
All heavy lifting cached in-process; recompute() is called by the API on demand
or when params change.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import data_feed as dfd
from .strategies import vol_target_tqqq, cycle_rotation_a, ASSETS_B, RISKY_B, SAFE_B
from .backtest import run_weight_backtest, run_multi_asset, metrics, slice_window, renorm

WINDOWS = ["full", "3y", "1y", "since_jun"]
WINDOW_LABELS = {"full": "全历史", "3y": "近3年", "1y": "近1年", "since_jun": "2026-06以来"}


def _curve_payload(bt: pd.DataFrame, window: str) -> dict:
    seg = renorm(slice_window(bt, window))
    m_strat = metrics(seg["equity"], seg["ret"], seg["weight"])
    m_bench = metrics(seg["bench_equity"], seg["bench_equity"].pct_change().fillna(0))
    return {
        "window": window,
        "label": WINDOW_LABELS[window],
        "start": str(seg.index[0].date()),
        "end": str(seg.index[-1].date()),
        "strategy": m_strat,
        "benchmark": m_bench,
        "beats_benchmark": (m_strat.get("total_return") or -999) > (m_bench.get("total_return") or 999),
        # downsample curves for the chart (max ~400 points)
        "dates": [str(d.date() if hasattr(d, "date") else d) for d in seg.index[:: max(1, len(seg) // 400)]],
        "equity": [round(float(v), 4) for v in seg["equity"][:: max(1, len(seg) // 400)]],
        "bench_equity": [round(float(v), 4) for v in seg["bench_equity"][:: max(1, len(seg) // 400)]],
        "drawdown": [round(float(v) * 100, 2) for v in seg["dd"][:: max(1, len(seg) // 400)]],
    }


def build_strategy_a(params: dict | None = None) -> dict:
    p = {
        "vol_window_daily": 40,
        "target_vol": 0.35,
        "trend_window": 200,
        "min_change": 0.10,
        "stop_dd": 0.25,
    }
    if params:
        p.update(params)

    qqq_d = dfd.get_us("QQQ", "1d", "5y")
    tqqq_d = dfd.get_us("TQQQ", "1d", "5y")
    w_d = vol_target_tqqq(
        qqq_d, tqqq_d["close"], freq="D",
        vol_window=p["vol_window_daily"], target_vol=p["target_vol"],
        trend_window=p["trend_window"], min_change=p["min_change"],
        stop_dd=p["stop_dd"],
    )
    bt_d = run_weight_backtest(w_d, tqqq_d["close"])

    out = {"id": "A", "name": "波动红利 · TQQQ 波动率目标", "market": "美股",
           "freq_label": "日级信号 · 次日执行", "windows": {}}
    for win in WINDOWS:
        out["windows"][win] = _curve_payload(bt_d, win)

    rv_now = float((qqq_d["close"].pct_change().rolling(p["vol_window_daily"]).std()
                    * np.sqrt(252)).iloc[-1])
    gate_now = bool(qqq_d["close"].iloc[-1] > qqq_d["close"].rolling(p["trend_window"]).mean().iloc[-1])
    out["current"] = {
        "exposure": round(float(w_d.iloc[-1]) * 100),
        "realized_vol": round(rv_now * 100, 1),
        "target_vol": p["target_vol"] * 100,
        "trend_gate_on": gate_now,
        "as_of": str(tqqq_d.index[-1].date()),
    }
    return out


def build_strategy_c(params: dict | None = None) -> dict:
    """港股：恒指两倍杠杆 ETF(7200.HK) 波动率目标 + SMA200 趋势闸门。
    信号源与基准均为 2800.HK(盈富基金)。日级，次日执行。"""
    p = {
        "vol_window": 20,
        "target_vol": 0.40,     # 恒指长期波动高于纳指，阈值放宽
        "trend_window": 200,
        "min_change": 0.10,
        "stop_dd": None,
    }
    if params:
        p.update(params)

    sig = dfd.get_us("2800.HK", "1d", "5y")   # signal + benchmark
    veh = dfd.get_us("7200.HK", "1d", "5y")   # tradeable 2x HSI

    px_sig = sig["close"].reindex(veh.index).ffill()   # align calendars
    sig_aligned = sig.copy()
    sig_aligned["close"] = px_sig

    w = vol_target_tqqq(
        sig_aligned, veh["close"], freq="D",
        vol_window=p["vol_window"], target_vol=p["target_vol"],
        trend_window=p["trend_window"], min_change=p["min_change"],
        stop_dd=p["stop_dd"],
    )
    bt = run_weight_backtest(w, veh["close"])

    out = {"id": "C", "name": "波动红利 · 恒指2x (7200.HK)", "market": "港股",
           "freq_label": "日级信号 · 次日执行", "windows": {}}
    for win in WINDOWS:
        out["windows"][win] = _curve_payload(bt, win)

    rv_now = float((px_sig.pct_change().rolling(p["vol_window"]).std() * np.sqrt(252)).iloc[-1])
    gate_now = bool(px_sig.iloc[-1] > px_sig.rolling(p["trend_window"]).mean().iloc[-1])
    out["current"] = {
        "exposure": round(float(w.iloc[-1]) * 100),
        "realized_vol": round(rv_now * 100, 1),
        "target_vol": p["target_vol"] * 100,
        "trend_gate_on": gate_now,
        "as_of": str(veh.index[-1].date()),
    }
    return out


def build_strategy_b(params: dict | None = None) -> dict:
    p = {"mom_window": 120, "vol_window": 60, "ma_window": 60, "weekly": True,
         "buffer_pct": 0.10, "cost_rate": 0.0015}
    if params:
        p.update(params)

    frames = {c: dfd.get_a(c, "20220601") for c in list(ASSETS_B.keys())}
    decisions = cycle_rotation_a(
        frames, mom_window=p["mom_window"], vol_window=p["vol_window"],
        ma_window=p["ma_window"], weekly=p["weekly"], buffer_pct=p["buffer_pct"],
    )

    closes = pd.DataFrame({c: f["close"] for c, f in frames.items()}).sort_index()
    weights = pd.DataFrame(0.0, index=closes.index, columns=closes.columns)
    for t, row in decisions.iterrows():
        pos = closes.index.searchsorted(t)
        exec_idx = closes.index[min(pos + 1, len(closes) - 1)]
        weights.loc[exec_idx:, row["pick"]] = 1.0
    bt = run_multi_asset(weights, closes, cost_rate=p["cost_rate"])

    out = {"id": "B", "name": "周期红利 · A股周度轮动", "market": "A股",
           "freq_label": "周日级（每周五收盘决策）", "windows": {},
           "assets": {c: v["name"] for c, v in ASSETS_B.items()}}
    for win in WINDOWS:
        payload = _curve_payload(bt, win)
        # benchmark for B should be 沪深300 not first-column bond
        seg = slice_window(bt, win)
        hs300 = closes["sh000300"].reindex(seg.index).dropna()
        bench_eq = hs300 / hs300.iloc[0]
        payload["benchmark"] = metrics(bench_eq, bench_eq.pct_change().fillna(0))
        payload["beats_benchmark"] = (payload["strategy"].get("total_return") or -999) > (
            payload["benchmark"].get("total_return") or 999)
        payload["bench_equity"] = [round(float(v), 4) for v in bench_eq[:: max(1, len(bench_eq) // 400)]]
        payload["dates"] = [str(d.date()) for d in bench_eq.index[:: max(1, len(bench_eq) // 400)]]
        out["windows"][win] = payload

    last_dec = decisions.iloc[-1]
    out["current"] = {
        "pick": str(last_dec["pick"]),
        "pick_name": ASSETS_B[str(last_dec["pick"])]["name"],
        "etf": ASSETS_B[str(last_dec["pick"])]["etf"],
        "reason": str(last_dec["reason"]),
        "decision_date": str(decisions.index[-1].date()),
        "as_of": str(closes.index[-1].date()),
    }
    recent = decisions.tail(12).iloc[::-1]
    out["recent_picks"] = [
        {"date": str(t.date()), "pick": ASSETS_B[r["pick"]]["name"], "reason": r["reason"]}
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
