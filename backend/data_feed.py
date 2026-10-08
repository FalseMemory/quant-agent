"""
Data feed layer: free sources, no API keys.
- US/HK daily kline: primary Yahoo Finance v8 chart; fallback Sina US daily
  (US tickers) / Tencent gtimg fqkline (.HK tickers).

Two rules govern every series here:

1. **Bars are labelled in the exchange's OWN local time.** Yahoo stamps daily
   bars at the session open, so a HK bar arrives as 01:30 UTC (= 09:30 HKT).
   Converting every market to America/New_York used to shift HK bars one
   calendar day backwards, which made "as of" dates lie by a day.
2. **An in-progress session is not a close.** Intraday, Yahoo already returns
   today's bar with a live price in `close`. Feeding that into the backtest
   makes signals drift with the tape, so the current session's bar is dropped
   until the exchange's own closing time has passed.

Caching: each series is written to data_cache/ as CSV. A cached file is reused
only while it is still *current* — i.e. it was written after the market's most
recent close. Once a session closes the next request refetches, so a refresh
right after the bell picks up the new bar instead of waiting out a timer.
`forcing(True)` bypasses the cache entirely. If every live source fails, an
EXPIRED cache is served as a last resort instead of raising.
"""
from __future__ import annotations

import os
import time
import json
import contextlib
import contextvars
import datetime as dt
from pathlib import Path

import pandas as pd
import requests

CACHE_DIR = Path(__file__).resolve().parent.parent / "data_cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# Bump whenever bar labelling or shaping changes. Cached CSVs written by an
# older scheme carry the old semantics (e.g. HK dates shifted a day) and must be
# discarded rather than replayed — otherwise a code fix appears to do nothing.
CACHE_VERSION = "2"
_version_ready: set[str] = set()

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
_SESSION = requests.Session()
_SESSION.headers.update({"User-Agent": _UA})

# Per-market exchange timezone and regular-session close, in exchange local time.
MARKETS: dict[str, dict] = {
    "US": {"tz": "America/New_York", "close": dt.time(16, 0)},
    "HK": {"tz": "Asia/Hong_Kong", "close": dt.time(16, 0)},
    "CN": {"tz": "Asia/Shanghai", "close": dt.time(15, 0)},
}

_FORCE = contextvars.ContextVar("data_feed_force", default=False)


@contextlib.contextmanager
def forcing(flag: bool = True):
    """Within this block every cache read is skipped, so data is refetched."""
    token = _FORCE.set(flag)
    try:
        yield
    finally:
        _FORCE.reset(token)


def market_of(symbol: str) -> str:
    """.HK tickers trade in Hong Kong, everything else in the US."""
    return "HK" if symbol.upper().endswith(".HK") else "US"


def _cache_path(key: str) -> Path:
    return CACHE_DIR / f"{key}.csv"


def _ensure_cache_version() -> None:
    """Wipe caches left over from an older labelling/shaping scheme.

    Cheap (runs once per directory) and keeps a fix from being swallowed by
    stale files that still parse fine but mean something different.
    """
    root = str(CACHE_DIR)
    if root in _version_ready:
        return
    _version_ready.add(root)
    marker = CACHE_DIR / "_version"
    try:
        if marker.read_text(encoding="utf-8").strip() == CACHE_VERSION:
            return
    except OSError:
        pass
    for stale in CACHE_DIR.glob("*.csv"):
        try:
            stale.unlink()
        except OSError:
            pass
    try:
        marker.write_text(CACHE_VERSION, encoding="utf-8")
    except OSError:
        pass


def _read_cache_csv(p: Path):
    try:
        df = pd.read_csv(p, index_col=0)
        df.index = pd.to_datetime(df.index)
        return df
    except Exception:  # corrupted cache -> refetch
        return None


def _last_close_before(now_local: pd.Timestamp, market: str) -> pd.Timestamp:
    """Most recent regular-session close at or before `now_local`.

    Weekends are skipped; holidays are not modelled, which deliberately errs
    toward refetching once after a holiday close closes (harmless) rather than
    serving a stale series for days.
    """
    spec = MARKETS[market]
    delta = pd.Timedelta(hours=spec["close"].hour, minutes=spec["close"].minute)
    day = now_local.normalize()
    close_today = day + delta
    if day.weekday() < 5 and now_local >= close_today:
        return close_today
    day -= pd.Timedelta(days=1)
    while day.weekday() >= 5:
        day -= pd.Timedelta(days=1)
    return day + delta


def _cache_is_current(p: Path, market: str,
                      now: pd.Timestamp | None = None) -> bool:
    """True when the cache was written after the market's latest close.

    Replaces a fixed TTL: a timer cannot know that a session just ended, so it
    either refetched too often or kept serving yesterday's bar all evening.
    """
    spec = MARKETS[market]
    now_local = now if now is not None else pd.Timestamp.now(tz=spec["tz"])
    written = pd.Timestamp(
        dt.datetime.fromtimestamp(p.stat().st_mtime, tz=dt.timezone.utc)
    ).tz_convert(spec["tz"])
    return written >= _last_close_before(now_local, market)


def _load_cache(key: str, market: str):
    _ensure_cache_version()
    p = _cache_path(key)
    if not p.exists():
        return None
    if _FORCE.get():
        return None
    if _cache_is_current(p, market):
        return _read_cache_csv(p)
    return None


def drop_incomplete_session(df: pd.DataFrame, market: str,
                            now: pd.Timestamp | None = None) -> pd.DataFrame:
    """Drop the last bar while its session is still trading.

    Index is expected to be naive exchange-local timestamps.
    """
    if df.empty:
        return df
    spec = MARKETS[market]
    now_local = (now if now is not None else pd.Timestamp.now(tz=spec["tz"])).tz_localize(None)
    if df.index[-1].normalize() == now_local.normalize() and now_local.time() < spec["close"]:
        return df.iloc[:-1]
    return df


def _covers_latest_close(df: pd.DataFrame, market: str,
                         now: pd.Timestamp | None = None) -> bool:
    """Does the series reach the market's most recent closed session?

    "Has data" is not enough to accept a source. Yahoo occasionally returns a
    whole row as null — HK 2026-10-07 came back with close=None — and since
    nulls are dropped, the series just silently ends a day early. Comparing the
    last bar against the latest close catches that, and lets the next source in
    the chain fill the hole.
    """
    if df is None or df.empty:
        return False
    spec = MARKETS[market]
    now_local = now if now is not None else pd.Timestamp.now(tz=spec["tz"])
    return df.index[-1].date() >= _last_close_before(now_local, market).date()


def _load_stale_cache(key: str):
    """Load cache regardless of TTL — last-resort fallback when all live sources fail."""
    _ensure_cache_version()
    p = _cache_path(key)
    if p.exists():
        return _read_cache_csv(p)
    return None


def _save_cache(key: str, df: pd.DataFrame):
    _ensure_cache_version()
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
def _yahoo_chart_host(symbol: str, period: str, host: str) -> pd.DataFrame:
    url = f"https://{host}/v8/finance/chart/{symbol}"
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
    # Label bars in the exchange's own timezone. Using a single timezone for all
    # markets shifted HK bars back by one calendar day (09:30 HKT showed up as
    # the previous evening in New York), which corrupted every "as of" date.
    tz = MARKETS[market_of(symbol)]["tz"]
    df = df[~df["close"].isna()].tz_convert(tz).tz_localize(None)
    return df[["open", "high", "low", "close", "volume"]]


def _yahoo_chart(symbol: str, period: str) -> pd.DataFrame:
    return _yahoo_chart_host(symbol, period, "query1.finance.yahoo.com")


def _yahoo_chart_query2(symbol: str, period: str) -> pd.DataFrame:
    """Second Yahoo chart host; useful when query1 is temporarily blocked."""
    return _yahoo_chart_host(symbol, period, "query2.finance.yahoo.com")


def _stooq_us_daily(symbol: str, period: str) -> pd.DataFrame:
    """Stooq CSV fallback for ordinary US tickers (daily, no API key)."""
    ticker = symbol.lower().replace(".", "-") + ".us"
    r = _SESSION.get("https://stooq.com/q/d/l/", params={"s": ticker, "i": "d"}, timeout=30)
    r.raise_for_status()
    from io import StringIO
    df = pd.read_csv(StringIO(r.text))
    expected = {"Date", "Open", "High", "Low", "Close", "Volume"}
    if df.empty or not expected.issubset(df.columns):
        raise ValueError(f"stooq us empty/invalid: {r.text[:80]}")
    df = df.rename(columns={"Date": "date", "Open": "open", "High": "high",
                            "Low": "low", "Close": "close", "Volume": "volume"})
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date", "close"]).set_index("date")
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df[df.index >= _period_cutoff(period)]
    if df.empty:
        raise ValueError("stooq us empty after window slice")
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
        return [_yahoo_chart, _yahoo_chart_query2, _tencent_hk_daily]
    return [_yahoo_chart, _yahoo_chart_query2, _sina_us_daily, _stooq_us_daily]


def get_us(symbol: str, interval: str = "1d", period: str = "5y") -> pd.DataFrame:
    """Return OHLCV DataFrame for a US/HK ticker. interval in {1d,1h,1wk};
    period like 1y/3y/5y/10y. Fallback mirrors cover 1d only; other intervals
    stay Yahoo-only. If everything fails, serve an expired cached copy."""
    key = f"us_{symbol}_{interval}_{period}"
    market = market_of(symbol)
    cached = _load_cache(key, market)
    if cached is not None:
        return cached

    df, errors = None, []
    fallback = None  # first non-empty result, used when no source reaches the latest close
    sources = [_yahoo_chart] if interval != "1d" else _us_sources(symbol)
    for src in sources:
        try:
            frame = src(symbol, period)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{src.__name__}: {type(e).__name__} {str(e)[:80]}")
            continue
        if frame is None or frame.empty:
            errors.append(f"{src.__name__}: empty")
            continue
        if interval == "1d":
            frame = drop_incomplete_session(frame, market)
            if frame.empty:
                errors.append(f"{src.__name__}: empty after dropping live bar")
                continue
        if fallback is None:
            fallback = frame
        # Keep moving down the chain only while the data is behind; a source that
        # already reaches the latest close ends the search with no extra requests.
        if interval != "1d" or _covers_latest_close(frame, market):
            df = frame
            break
    if df is None:
        df = fallback
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
    cached = _load_cache(key, "CN")
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
    # Sina normally withholds the live session, but guard anyway: the A-share
    # decision is made on closes, never on an unfinished bar.
    df = drop_incomplete_session(df, "CN")
    df = df[df.index >= start_dt]
    if end:
        df = df[df.index <= pd.to_datetime(end)]
    if len(df) == 0:
        raise RuntimeError(f"get_a({code}): no rows in requested window "
                           f"(source series may be stale; try another code)")
    _save_cache(key, df)
    return df


def _tencent_quote_code(symbol: str, market: str) -> str:
    if market == "hk":
        return _hk_code(symbol)
    if market == "us":
        return "us" + symbol.upper()
    return symbol.lower()


def _tencent_quote(symbol: str, market: str) -> dict:
    code = _tencent_quote_code(symbol, market)
    r = _SESSION.get("https://qt.gtimg.cn/q=" + code, timeout=10)
    r.raise_for_status()
    raw = r.text.split('="', 1)[1].rstrip('";')
    parts = raw.split("~")
    if len(parts) < 5 or not parts[1].strip():
        raise ValueError(f"tencent quote({code}) invalid")
    price = float(parts[3])
    prev_close = float(parts[4])
    if price <= 0:
        raise ValueError(f"tencent quote({code}) has no price")
    return {"price": price, "prev_close": prev_close, "name": parts[1].strip(),
            "as_of": parts[30] if len(parts) > 30 and parts[30] else None,
            "source": "腾讯行情"}


def _yahoo_quote(symbol: str, market: str) -> dict:
    errors = []
    for host in ("query1.finance.yahoo.com", "query2.finance.yahoo.com"):
        try:
            url = f"https://{host}/v8/finance/chart/{symbol}"
            r = _SESSION.get(url, params={"interval": "1d", "range": "5d"}, timeout=15)
            r.raise_for_status()
            result = r.json()["chart"]["result"][0]
            meta = result.get("meta") or {}
            price = meta.get("regularMarketPrice")
            prev = meta.get("chartPreviousClose") or meta.get("previousClose")
            if price is None:
                closes = ((result.get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
                closes = [x for x in closes if x is not None]
                price = closes[-1] if closes else None
                prev = prev or (closes[-2] if len(closes) > 1 else None)
            if price is None:
                raise ValueError("no price")
            return {"price": float(price), "prev_close": float(prev or price),
                    "name": (meta.get("longName") or meta.get("shortName") or "").strip() or None,
                    "as_of": None, "source": "Yahoo Finance"}
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{host}: {type(exc).__name__}")
    raise RuntimeError("; ".join(errors))


def get_quote(symbol: str, market: str) -> dict:
    """Lightweight cross-market quote/name lookup, independent from history."""
    errors = []
    for source in (_tencent_quote, _yahoo_quote):
        try:
            return source(symbol, market)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{source.__name__}: {str(exc)[:100]}")
    raise RuntimeError(f"get_quote({symbol},{market}) all sources failed: {'; '.join(errors)}")


def get_a_live(code: str) -> dict | None:
    """Live snapshot from gtimg quote service. Returns dict or None."""
    try:
        return _tencent_quote(code, "a")
    except Exception:
        return None


if __name__ == "__main__":
    tqqq = get_us("TQQQ", "1d", "3y")
    print("TQQQ daily rows:", len(tqqq), "range", tqqq.index.min(), tqqq.index.max())
    tqqq_h = get_us("TQQQ", "1h", "1mo")
    print("TQQQ hourly rows:", len(tqqq_h), "range", tqqq_h.index.min(), tqqq_h.index.max())
    cyb = get_a("sz399006", "20240101")
    print("创业板 rows:", len(cyb), "range", cyb.index.min(), cyb.index.max())
