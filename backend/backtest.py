"""Backtest engine: next-bar execution, turnover costs, metrics, windows."""
from __future__ import annotations

import numpy as np
import pandas as pd

TRADING_DAYS = 252


def run_weight_backtest(
    weights: pd.Series,
    prices: pd.Series,
    *,
    cost_rate: float = 0.0015,   # 15 bps on traded notional
) -> pd.DataFrame:
    """weights decided at close t apply to return t->t+1 (shift by 1).
    Returns df with columns [ret, equity, bench, dd, dd_bench, weight]."""
    w = weights.reindex(prices.index).ffill().fillna(0.0)
    r = prices.pct_change().fillna(0.0)

    strat_ret_raw = w.shift(1).fillna(0.0) * r
    turnover = w.diff().abs().fillna(0.0)
    cost = turnover * cost_rate
    # costs hit the bar after the trade executes
    strat_ret = strat_ret_raw - cost

    eq = (1 + strat_ret).cumprod()
    bench = prices / prices.iloc[0]
    out = pd.DataFrame({
        "ret": strat_ret,
        "equity": eq,
        "bench_equity": bench,
        "weight": w,
    }, index=prices.index)
    out["dd"] = out["equity"] / out["equity"].cummax() - 1
    out["dd_bench"] = out["bench_equity"] / out["bench_equity"].cummax() - 1
    return out


def run_multi_asset(
    weights: pd.DataFrame,
    prices: pd.DataFrame,
    *,
    cost_rate: float = 0.0015,
) -> pd.DataFrame:
    """weights: DataFrame (dates × assets) of target fractions decided at close t.
    Applies to returns t->t+1 with turnover costs."""
    w = weights.reindex(prices.index).ffill().fillna(0.0)
    r = prices.pct_change()
    strat_ret_raw = (w.shift(1).fillna(0.0) * r.fillna(0.0)).sum(axis=1)
    turnover = w.diff().abs().sum(axis=1).fillna(0.0)
    strat_ret = strat_ret_raw - turnover * cost_rate

    eq = (1 + strat_ret).cumprod()
    bench = prices.iloc[:, 0] / prices.iloc[:, 0].iloc[0]
    out = pd.DataFrame({"ret": strat_ret, "equity": eq, "bench_equity": bench, "weight": w.sum(axis=1)},
                       index=prices.index)
    out["dd"] = out["equity"] / out["equity"].cummax() - 1
    out["dd_bench"] = out["bench_equity"] / out["bench_equity"].cummax() - 1
    return out


def metrics(eq: pd.Series, rets: pd.Series, weights: pd.Series | None = None) -> dict:
    if len(eq) < 2:
        return {}
    total = float(eq.iloc[-1] / eq.iloc[0] - 1)
    days = max((eq.index[-1] - eq.index[0]).days, 1)
    years = days / 365.0
    cagr = (eq.iloc[-1] / eq.iloc[0]) ** (1 / max(years, 1e-9)) - 1 if years > 0.02 else np.nan
    vol = float(rets.std() * np.sqrt(TRADING_DAYS))
    sharpe = float(rets.mean() / rets.std() * np.sqrt(TRADING_DAYS)) if rets.std() > 0 else np.nan
    dd = float((eq / eq.cummax() - 1).min())
    calmar = float(cagr / abs(dd)) if (np.isfinite(cagr) and dd < 0) else np.nan
    res = {
        "total_return": round(total * 100, 1),
        "cagr": round(float(cagr) * 100, 1) if np.isfinite(cagr) else None,
        "ann_vol": round(vol * 100, 1),
        "sharpe": round(sharpe, 2) if np.isfinite(sharpe) else None,
        "max_dd": round(dd * 100, 1),
        "calmar": round(calmar, 2) if np.isfinite(calmar) else None,
        "avg_exposure": round(float(weights.mean()) * 100, 0) if weights is not None and len(weights) else None,
    }
    return res


def slice_window(df: pd.DataFrame, window: str) -> pd.DataFrame:
    """window in {full, 3y, 1y, since_jun}."""
    if window == "full":
        return df
    if window == "3y":
        return df[df.index >= df.index[-1] - pd.DateOffset(years=3)]
    if window == "1y":
        return df[df.index >= df.index[-1] - pd.DateOffset(years=1)]
    if window == "since_jun":
        # live-tracking segment starting 2026-06-01
        start = pd.Timestamp("2026-06-01")
        seg = df[df.index >= start]
        if len(seg) < 2:
            seg = df.tail(60).copy()  # fallback for short series
        return seg
    raise ValueError(window)


def renorm(seg: pd.DataFrame) -> pd.DataFrame:
    for col in ("equity", "bench_equity"):
        seg = seg.copy()
        seg[col] = seg[col] / seg[col].iloc[0]
    return seg


def beats(strat_total: float | None, bench_total: float | None) -> bool | None:
    if strat_total is None or bench_total is None:
        return None
    return strat_total > bench_total
