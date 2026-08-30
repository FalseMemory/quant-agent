"""
User watchlist: arbitrary stocks/ETFs the user wants observed.

- Codes are normalized; market auto-detected when unspecified.
  US: letters (NVDA) · HK: digits or xxx.HK · A-share: 6-digit (auto sh/sz
  prefix by first digit) or explicit sh000300 / sz399006 form.
- Chinese names resolved via gtimg quotes for A/HK; US shows the symbol.
- Snapshots reuse ai_advisor._snap on top of data_feed series.
- Watchlist items enter the AI context and can be analyzed as WATCH assets. The
  user may use them for sell/hold decisions even when they are not strategy assets.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
WL_FILE = ROOT / "watchlist.json"

_SESSION = requests.Session()
_SESSION.headers.update({"User-Agent": "Mozilla/5.0"})


class WatchError(Exception):
    """Friendly, user-facing watchlist error."""


def _normalize_a(code: str) -> str:
    c = code.lower()
    if re.fullmatch(r"(sh|sz|bj)\d{6}", c):
        return c
    if re.fullmatch(r"\d{6}", c):
        head = c[0]
        if head in ("5", "6", "9"):
            return "sh" + c
        if head in ("0", "1", "3"):
            return "sz" + c
    raise WatchError(
        f"A股代码「{code}」无法识别：6位数字按首位自动加 sh/sz；指数请直接写 sh000300 形式")


def normalize(code: str, market: str = "auto") -> dict:
    raw = re.sub(r"\s+", "", code or "")
    if not raw:
        raise WatchError("代码不能为空")
    m = (market or "auto").lower()

    if m == "us":
        u = raw.upper()
        if not re.fullmatch(r"[A-Z][A-Z.\-]{0,9}(\.[A-Z]{2})?", u):
            raise WatchError(f"美股代码「{raw}」格式不对，应为字母形式如 NVDA")
        return {"code": u, "market": "us"}

    if m == "hk":
        digits = re.sub(r"\D", "", raw)
        if not digits:
            raise WatchError(f"港股代码「{raw}」格式不对，应为数字如 0700 或 2800.HK")
        return {"code": f"{int(digits):04d}.HK", "market": "hk"}

    if m == "a":
        return {"code": _normalize_a(raw), "market": "a"}

    # auto-detect
    if raw.upper().endswith(".HK"):
        return normalize(raw, "hk")
    if re.fullmatch(r"(?i)(sh|sz|bj)\d{6}", raw):
        return {"code": raw.lower(), "market": "a"}
    if re.fullmatch(r"\d{6}", raw):
        return {"code": _normalize_a(raw), "market": "a"}
    if re.fullmatch(r"\d{4,5}", raw):
        return normalize(raw, "hk")
    if re.fullmatch(r"[A-Za-z][A-Za-z.\-]{0,9}", raw):
        return {"code": raw.upper(), "market": "us"}
    raise WatchError(f"无法识别代码「{raw}」，可指定市场后重试")


def load_items() -> list[dict]:
    if WL_FILE.exists():
        try:
            data = json.loads(WL_FILE.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return [x for x in data if isinstance(x, dict) and x.get("code")]
        except Exception:  # noqa: BLE001
            pass
    return []


def save_items(items: list[dict]) -> None:
    WL_FILE.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")


def _gtimg_name(gt_code: str) -> str | None:
    try:
        r = _SESSION.get("https://qt.gtimg.cn/q=" + gt_code, timeout=8)
        raw = r.text.split('="', 1)[1].rstrip('";')
        name = raw.split("~")[1].strip()
        return name or None
    except Exception:  # noqa: BLE001
        return None


def fetch_name(item: dict) -> str | None:
    """Resolve a security name without requiring long historical data."""
    if item["market"] == "a":
        return _gtimg_name(item["code"])
    if item["market"] == "hk":
        # gtimg uses 5-digit HK codes: hk00700
        digits = item["code"].split(".")[0]
        name = _gtimg_name("hk" + str(int(digits)).zfill(5))
        if name:
            return name
    if item["market"] == "us":
        name = _gtimg_name("us" + item["code"].upper())
        if name:
            return name
    try:
        from . import data_feed as dfd
        return dfd.get_quote(item["code"], item["market"]).get("name")
    except Exception:  # noqa: BLE001
        return None


def resolve_asset_names(settings: dict[str, dict]) -> dict[str, dict]:
    """Fill only blank candidate names; user-entered names always win."""
    out = json.loads(json.dumps(settings, ensure_ascii=False))
    market_by_key = {"params_a": "us", "params_b": "a", "params_c": "hk"}
    for key, market in market_by_key.items():
        block = out.get(key) or {}
        for asset in block.get("assets") or []:
            current_name = str(asset.get("name") or "").strip()
            # Older frontend versions stored the code itself when name was blank.
            if current_name and current_name.upper() != str(asset["code"]).upper():
                continue
            item = {"code": asset["code"], "market": market}
            asset["name"] = fetch_name(item) or asset["code"]
            asset["name_resolved"] = asset["name"] != asset["code"]
        if key == "params_b" and isinstance(block.get("safe_asset"), dict):
            asset = block["safe_asset"]
            if not str(asset.get("name") or "").strip():
                asset["name"] = fetch_name({"code": asset["code"], "market": "a"}) or asset["code"]
    return out


def add(code: str, market: str = "auto") -> list[dict]:
    norm = normalize(code, market)
    items = load_items()
    if any(i["code"] == norm["code"] and i["market"] == norm["market"] for i in items):
        raise WatchError(f"{norm['code']} 已在自选中")
    name = fetch_name(norm)
    items.append({"code": norm["code"], "market": norm["market"],
                  "name": name or norm["code"], "name_resolved": bool(name)})
    save_items(items)
    return items


def remove(code: str) -> list[dict]:
    items = [i for i in load_items() if i["code"] != code]
    save_items(items)
    return items


def snapshot_one(item: dict) -> dict | None:
    """Return rich history metrics, or a lightweight quote when history fails."""
    from . import data_feed as dfd
    from .ai_advisor import _snap
    history_error = None
    try:
        if item["market"] == "a":
            px = dfd.get_a(item["code"], "20240101")["close"]
        else:
            px = dfd.get_us(item["code"], "1d", "2y")["close"]
        snap = _snap(px)
        if snap:
            snap["source"] = "历史日线"
            snap["quote_level"] = "full"
            return snap
    except Exception as exc:  # noqa: BLE001
        history_error = str(exc)[:180]

    quote = dfd.get_quote(item["code"], item["market"])
    price = float(quote["price"])
    prev = float(quote.get("prev_close") or price)
    chg = round((price / prev - 1.0) * 100.0, 2) if prev else None
    return {"close": round(price, 4), "chg_1d_pct": chg, "chg_5d_pct": None,
            "chg_20d_pct": None, "realized_vol_20d_pct": None,
            "vs_sma50_pct": None, "vs_sma200_pct": None,
            "as_of": quote.get("as_of") or "最新快照", "source": quote.get("source"),
            "quote_level": "light", "history_error": history_error,
            "name": quote.get("name")}


def snapshots() -> list[dict]:
    out = []
    for it in load_items():
        row = dict(it)
        try:
            s = snapshot_one(it)
            if s is None:
                row["error"] = "行情数据不足"
            else:
                row["snap"] = s
        except Exception as e:  # noqa: BLE001
            row["error"] = str(e)[:80]
        out.append(row)
    return out
