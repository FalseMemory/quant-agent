"""
Strategy library. Each strategy consumes OHLCV frames and emits a target-weight
series (fraction of equity in the risky asset, rest is cash/bond).

Design rules:
- Signals use only information available at bar t close.
- Weights are shifted one bar before applying returns (next-bar execution).
- No lookahead anywhere.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# Strategy A - US hourly/daily: volatility premium on TQQQ
# ---------------------------------------------------------------------------
def vol_target_tqqq(
    qqq: pd.DataFrame,
    tqqq_close: pd.Series,
    *,
    freq: str = "D",          # "H" hourly bars or "D" daily bars
    vol_window: int = 20,      # bars of realized-vol estimate
    target_vol: float = 0.35,  # annualized vol target
    trend_window: int = 200,   # SMA days for regime gate (daily basis)
    min_change: float = 0.10,  # ignore exposure moves smaller than this
    max_leverage: float = 1.0, # cap on TQQQ units held (1.0 = fully in TQQQ)
    stop_dd: float | None = None, # optional trailing drawdown stop on QQQ
) -> pd.Series:
    """Return target weight in TQQQ indexed like tqqq_close."""
    px = qqq["close"]
    rets = px.pct_change()
    ann = 252 * (6.5 if freq == "H" else 1)
    rv = rets.rolling(vol_window).std() * np.sqrt(ann)

    # trend gate on DAILY closes regardless of bar freq
    daily = px if freq == "D" else px.resample("1D").last().dropna()
    sma = daily.rolling(trend_window, min_periods=max(20, trend_window // 4)).mean()
    gate_daily = (daily > sma).astype(float)
    gate = gate_daily.reindex(px.index, method="ffill")

    raw = (target_vol / rv.replace(0, np.nan)).clip(upper=max_leverage).fillna(0.0)
    w = (raw * gate).fillna(0.0)

    # Optional trailing stop on the signal asset; re-arm above the trend SMA.
    if stop_dd is not None:
        rolling_high = px.rolling(252, min_periods=60).max()
        drawdown = px / rolling_high - 1.0
        recovery_sma = daily.rolling(50, min_periods=20).mean()
        stopped = False
        values = w.to_numpy(copy=True)
        for i, price in enumerate(px.to_numpy()):
            if not stopped and drawdown.iloc[i] < -stop_dd:
                stopped = True
            elif stopped and price > recovery_sma.reindex(px.index, method="ffill").iloc[i]:
                stopped = False
            if stopped:
                values[i] = 0.0
        w = pd.Series(values, index=w.index, name="weight")

    # hysteresis: avoid churn on tiny adjustments
    out = np.zeros(len(w))
    cur = 0.0
    for i, v in enumerate(w.values):
        if abs(v - cur) >= min_change:
            cur = float(v)
        out[i] = cur
    return pd.Series(out, index=w.index, name="weight")


# ---------------------------------------------------------------------------
# Strategy B - A-share weekly cycle rotation
# ---------------------------------------------------------------------------
ASSETS_B = {
    "sz399006": {"name": "创业板指", "etf": "159915 创业板ETF", "class": "周期成长"},
    "sh000300": {"name": "沪深300", "etf": "510300 沪深300ETF", "class": "大盘核心"},
    "sh000015": {"name": "上证红利", "etf": "510880 红利ETF", "class": "红利价值"},
    "sh511010": {"name": "国债ETF", "etf": "511010 国债ETF", "class": "防御"},
}

RISKY_B = ["sz399006", "sh000300", "sh000015"]
SAFE_B = "sh511010"


def cycle_rotation_a(
    frames: dict[str, pd.DataFrame],
    *,
    mom_window: int = 60,       # trading days momentum lookback
    vol_window: int = 60,
    ma_window: int = 40,        # absolute-trend gate
    weekly: bool = True,
    buffer_pct: float = 0.0,    # challenger must beat incumbent by this margin
) -> pd.DataFrame:
    """Weekly rotation: hold the risky asset with best risk-adjusted momentum
    if it is above its MA; otherwise hide in the bond ETF.

    Returns DataFrame with columns: date, pick(code), weight(1.0), reason.
    Index = decision dates (signal generated at these closes, executed next day).
    """
    closes = pd.DataFrame({c: f["close"] for c, f in frames.items()}).sort_index()
    rets = closes.pct_change()

    score = (
        closes.pct_change(mom_window)
        / rets.rolling(vol_window).std() * np.sqrt(252)
    )
    above_ma = closes > closes.rolling(ma_window).mean()

    if weekly:
        # decision on the last trading day of each ISO week
        key = closes.index.to_period("W")
        is_last = pd.Series(key, index=closes.index) != pd.Series(key, index=closes.index).shift(-1)
        dec_idx = closes.index[is_last.fillna(False)]
    else:
        dec_idx = closes.index

    rows = []
    prev_pick = SAFE_B
    for t in dec_idx:
        s = score.loc[t]
        ok = above_ma.loc[t]
        candidates = [c for c in RISKY_B if bool(ok.get(c, False)) and np.isfinite(s.get(c, np.nan))]
        if candidates:
            leader = max(candidates, key=lambda c: s[c])
            incumbent_ok = prev_pick in candidates and np.isfinite(s.get(prev_pick, np.nan))
            if incumbent_ok and s[leader] <= s[prev_pick] * (1.0 + buffer_pct):
                pick = prev_pick
                reason = "在位标的仍有效，挑战者优势未达换仓阈值"
            else:
                pick = leader
                reason = "动量最优且在均线上方"
        else:
            pick = SAFE_B
            reason = "风险资产趋势走弱，转入防御"
        rows.append({"date": t, "pick": pick, "weight": 1.0, "reason": reason})
        prev_pick = pick
    return pd.DataFrame(rows).set_index("date")
