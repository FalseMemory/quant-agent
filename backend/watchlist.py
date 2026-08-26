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
    if item["market"] == "a":
        return _gtimg_name(item["code"])
    if item["market"] == "hk":
        # gtimg uses 5-digit HK codes: hk00700
        digits = item["code"].split(".")[0]
        return _gtimg_name("hk" + str(int(digits)).zfill(5))
    return None  # US: symbol doubles as display name


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
    from . import data_feed as dfd
    from .ai_advisor import _snap
    if item["market"] == "a":
        px = dfd.get_a(item["code"], "20240101")["close"]
    else:
        px = dfd.get_us(item["code"], "1d", "2y")["close"]
    return _snap(px)


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
