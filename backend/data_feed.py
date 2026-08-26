"""
Data feed layer: free sources, no API keys.
- US/HK daily kline: primary Yahoo Finance v8 chart; fallback Sina US daily
  (US tickers) / Tencent gtimg fqkline (.HK tickers).
All series are cached to data_cache/ as CSV and re-used for 6h; if every live
source fails, an EXPIRED cache is served as a last resort instead of raising.
"""
from __future__ import annotations

import os
import time
import json
import datetime as dt
from pathlib import Path

import pandas as pd
import requests

CACHE_DIR = Path(__file__).resolve().parent.parent / "data_cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
_SESSION = requests.Session()
_SESSION.headers.update({"User-Agent": _UA})

CACHE_TTL = 6 * 3600  # seconds


def _cache_path(key: str) -> Path:
    return CACHE_DIR / f"{key}.csv"


def _read_cache_csv(p: Path):
    try:
        df = pd.read_csv(p, index_col=0)
        df.index = pd.to_datetime(df.index)
        return df
    except Exception:  # corrupted cache -> refetch
        return None


def _load_cache(key: str):
    p = _cache_path(key)
    if p.exists() and (time.time() - p.stat().st_mtime) < CACHE_TTL:
        return _read_cache_csv(p)
    return None


def _load_stale_cache(key: str):
    """Load cache regardless of TTL — last-resort fallback when all live sources fail."""
    p = _cache_path(key)
    if p.exists():
        return _read_cache_csv(p)
    return None


def _save_cache(key: str, df: pd.DataFrame):
    p = _cache_path(key)
    df.to_csv(p)


def _period_cutoff(period: str) -> pd.Timestamp:
    """Map a Yahoo-style range (5y/2y/1y...) to a start timestamp."""
    unit = period[-1]
    n = float(period[:-1]) if len(period) > 1 else 1.0
    days = {"y": 365.25, "m": 30.4, "w": 7, "d": 1}.get(unit, 365.25) * n
    return pd.Timestamp.now().normalize() - pd.Timedelta(days=days)


# ---------------------------------------------------------------------------
# US/HK: primary Yahoo v8 chart; fallbacks Sina US daily / Tencent HK kline
# ---------------------------------------------------------------------------
def _yahoo_chart(symbol: str, period: str) -> pd.DataFrame:
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    params = {"interval": "1d", "range": period}
    r = _SESSION.get(url, params=params, timeout=30)
    r.raise_for_status()
    payload = r.json()
    res = payload["chart"]["result"][0]
    ts = res["timestamp"]
    q = res["indicators"]["quote"][0]
    df = pd.DataFrame({
        "open": q["open"],
        "high": q["high"],
        "low": q["low"],
        "close": q["close"],
        "volume": q["volume"],
    }, index=pd.to_datetime(ts, unit="s", utc=True))
    df = df[~df["close"].isna()].tz_convert("America/New_York").tz_localize(None)
    return df[["open", "high", "low", "close", "volume"]]


def _sina_us_daily(symbol: str, period: str) -> pd.DataFrame:
    """Sina US daily kline, full listing history; slice by period."""
    url = ("https://stock.finance.sina.com.cn/usstock/api/json_v2.php/"
           "US_MinKService.getDailyK")
    r = _SESSION.get(url, params={"symbol": symbol.upper()}, timeout=30)
    r.raise_for_status()
    arr = r.json()
    if not isinstance(arr, list) or not arr:
        raise ValueError(f"sina us non-list/empty: {str(arr)[:80]}")
    df = pd.DataFrame(arr)[["d", "o", "h", "l", "c", "v"]]
    df.columns = ["date", "open", "high", "low", "close", "volume"]
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date").astype(float)
    df = df[~df["close"].isna()]
    cutoff = _period_cutoff(period)
    df = df[df.index >= cutoff]
    if df.empty:
        raise ValueError("sina us empty after window slice")
    return df[["open", "high", "low", "close", "volume"]]


def _hk_code(symbol: str) -> str:
    digits = "".join(ch for ch in symbol.split(".")[0] if ch.isdigit())
    return "hk" + digits.zfill(5)


def _tencent_hk_daily(symbol: str, period: str) -> pd.DataFrame:
    """Tencent gtimg HK daily kline (qfq). Single request caps at ~800 bars and
    returns the NEWEST bars inside [start,end], so page BACKWARD via end date."""
    code = _hk_code(symbol)
    url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
    cutoff = _period_cutoff(period).strftime("%Y-%m-%d")
    today = dt.date.today().strftime("%Y-%m-%d")

    frames: list[pd.DataFrame] = []
    end = today
    for _ in range(8):
        r = _SESSION.get(url, params={"param": f"{code},day,{cutoff},{end},800,qfq"},
                         timeout=30)
        r.raise_for_status()
        node = ((r.json().get("data") or {}).get(code)) or {}
        bars = node.get("qfqday") or node.get("day") or []
        if not bars:
            break
        tmp = pd.DataFrame(bars).iloc[:, :6]
        tmp.columns = ["date", "open", "close", "high", "low", "volume"]
        tmp["date"] = pd.to_datetime(tmp["date"])
        tmp = tmp.set_index("date").astype(float)
        frames.append(tmp[["open", "high", "low", "close", "volume"]])
        first = bars[0][0]
        if len(bars) < 800 or first <= cutoff:
            break
        end = (pd.to_datetime(first) - pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    if not frames:
        raise ValueError(f"tencent hk({code}) no bars")
    df = pd.concat(frames)
    df = df[~df.index.duplicated(keep="last")].sort_index()
    df = df[df.index >= pd.to_datetime(cutoff)]
    if df.empty:
        raise ValueError(f"tencent hk({code}) empty after merge")
    return df


def _us_sources(symbol: str) -> list:
    """Ordered fetchers for a symbol: Yahoo first, then a domestic mirror."""
    if symbol.upper().endswith(".HK"):
        return [_yahoo_chart, _tencent_hk_daily]
    return [_yahoo_chart, _sina_us_daily]


def get_us(symbol: str, interval: str = "1d", period: str = "5y") -> pd.DataFrame:
    """Return OHLCV DataFrame for a US/HK ticker. interval in {1d,1h,1wk};
    period like 1y/3y/5y/10y. Fallback mirrors cover 1d only; other intervals
    stay Yahoo-only. If everything fails, serve an expired cached copy."""
    key = f"us_{symbol}_{interval}_{period}"
    cached = _load_cache(key)
    if cached is not None:
        return cached

    df, errors = None, []
    sources = [_yahoo_chart] if interval != "1d" else _us_sources(symbol)
    for src in sources:
        try:
            df = src(symbol, period)
            break
        except Exception as e:  # noqa: BLE001
            errors.append(f"{src.__name__}: {type(e).__name__} {str(e)[:80]}")
    if df is None or df.empty:
        stale = _load_stale_cache(key)
        if stale is not None:
            age_h = round((time.time() - _cache_path(key).stat().st_mtime) / 3600, 1)
            print(f"[data_feed] WARN {key}: live sources failed ({'; '.join(errors)[:160]}); "
                  f"serving STALE cache (~{age_h}h old)")
            return stale
        raise RuntimeError(f"get_us({symbol},{interval},{period}) all sources failed: "
                           f"{'; '.join(errors)[:250]}")
    _save_cache(key, df)
    return df


# ---------------------------------------------------------------------------
# A-share: Sina kline (indices + ETFs). Tencent is only used for live quotes.
# ---------------------------------------------------------------------------
def get_a(code: str, start: str = "20180101", end: str | None = None) -> pd.DataFrame:
    """code like 'sh000001'(上证), 'sz399006'(创业板), 'sh000300'(沪深300),
    'sh000922'(中证红利), 'sh511010'(国债ETF). Returns daily OHLCV indexed by date.
    Sina returns up to 2000 daily bars (scale=240)."""
    start_dt = pd.to_datetime(start)
    key = f"a_{code}_{start}_{end or 'now'}"
    cached = _load_cache(key)
    if cached is not None:
        return cached
    url = "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData"
    # Sina quirk: large datalen may return an old segment only; small datalen
    # returns the freshest bars. Merge a long window + a short fresh window.
    frames = []
    last_err = None
    for datalen in (2000, 260):
        for attempt in range(3):
            try:
                time.sleep(0.6 * attempt)  # gentle pacing to dodge rate limits
                r = _SESSION.get(url, params={"symbol": code, "scale": 240, "ma": "no", "datalen": datalen}, timeout=30)
                r.raise_for_status()
                arr = r.json()
                if not isinstance(arr, list) or len(arr) == 0:
                    raise ValueError(f"sina non-list/empty: {str(arr)[:80]}")
                tmp = pd.DataFrame(arr).rename(columns={"day": "date"})
                missing = {"open", "high", "low", "close", "volume"} - set(tmp.columns)
                if missing:
                    raise ValueError(f"sina missing columns {missing}")
                tmp["date"] = pd.to_datetime(tmp["date"])
                tmp = tmp.set_index("date").astype(float)
                frames.append(tmp[["open", "high", "low", "close", "volume"]])
                break
            except Exception as e:  # noqa: BLE001
                last_err = e
    if not frames:
        stale = _load_stale_cache(key)
        if stale is not None and len(stale) > 0:
            age_h = round((time.time() - _cache_path(key).stat().st_mtime) / 3600, 1)
            print(f"[data_feed] WARN get_a({code}): live sources failed "
                  f"({str(last_err)[:120]}); serving STALE cache (~{age_h}h old)")
            return stale
        raise RuntimeError(f"get_a({code}) failed after retries: {last_err}")
    df = pd.concat(frames)
    df = df[~df.index.duplicated(keep="last")].sort_index()
    if len(df) == 0:
        raise RuntimeError(f"get_a({code}) empty after merge")
    df = df[df.index >= start_dt]
    if end:
        df = df[df.index <= pd.to_datetime(end)]
    if len(df) == 0:
        raise RuntimeError(f"get_a({code}): no rows in requested window "
                           f"(source series may be stale; try another code)")
    _save_cache(key, df)
    return df


def get_a_live(code: str) -> dict | None:
    """Live snapshot from gtimg quote service. Returns dict or None."""
    try:
        r = _SESSION.get("https://qt.gtimg.cn/q=" + code, timeout=10)
        # format: v_code="1~name~code~price~昨收~开盘~..."
        raw = r.text.split('="', 1)[1].rstrip('";')
        parts = raw.split("~")
        return {"price": float(parts[3]), "prev_close": float(parts[4]), "name": parts[1]}
    except Exception:
        return None


if __name__ == "__main__":
    tqqq = get_us("TQQQ", "1d", "3y")
    print("TQQQ daily rows:", len(tqqq), "range", tqqq.index.min(), tqqq.index.max())
    tqqq_h = get_us("TQQQ", "1h", "1mo")
    print("TQQQ hourly rows:", len(tqqq_h), "range", tqqq_h.index.min(), tqqq_h.index.max())
    cyb = get_a("sz399006", "20240101")
    print("创业板 rows:", len(cyb), "range", cyb.index.min(), cyb.index.max())
