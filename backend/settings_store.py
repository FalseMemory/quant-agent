"""Persistent, validated strategy settings with legacy-data compatibility."""
from __future__ import annotations

import json
import math
import os
import re
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
SETTINGS_FILE = ROOT / "strategy_settings.json"

DEFAULT_A_ASSETS = [
    {"code": "TQQQ", "name": "TQQQ", "class": "用户候选"},
]
DEFAULT_C_ASSETS = [
    {"code": "7200.HK", "name": "7200.HK", "class": "用户候选"},
]
DEFAULT_B_ASSETS = [
    {"code": "sz159915", "name": "创业板ETF", "class": "周期成长"},
    {"code": "sh510300", "name": "沪深300ETF", "class": "大盘核心"},
    {"code": "sh510880", "name": "红利ETF", "class": "红利价值"},
    {"code": "sh518880", "name": "黄金ETF", "class": "商品避险"},
]
DEFAULT_B_SAFE_ASSET = {"code": "sh511010", "name": "国债ETF", "class": "系统防御"}

DEFAULT_PARAMS = {
    "params_a": {"target_vol": 0.35, "trend_window": 200, "assets": DEFAULT_A_ASSETS},
    "params_b": {
        "mom_window": 180,
        "ma_window": 60,
        "assets": DEFAULT_B_ASSETS,
        "safe_asset": DEFAULT_B_SAFE_ASSET,
    },
    "params_c": {"target_vol": 0.40, "trend_window": 200, "assets": DEFAULT_C_ASSETS},
}


def _deep_defaults() -> dict[str, dict]:
    return json.loads(json.dumps(DEFAULT_PARAMS, ensure_ascii=False))


def _finite_number(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{label}必须是数值")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label}必须是数值") from exc
    if not math.isfinite(number):
        raise ValueError(f"{label}必须是有限数值")
    return number


def _canonical_a_etf(raw: Any, label: str = "ETF") -> dict[str, str]:
    if isinstance(raw, str):
        raw = {"code": raw}
    if not isinstance(raw, dict):
        raise ValueError(f"{label}配置格式无效")
    code = str(raw.get("code", "")).strip().lower()
    digits = code[-6:] if len(code) >= 6 else code
    if not digits.isdigit() or len(digits) != 6:
        raise ValueError(f"{label}代码必须是6位A股ETF代码")
    if not code.startswith(("sh", "sz")):
        code = ("sh" if digits.startswith(("5", "6")) else "sz") + digits
    if not digits.startswith(("15", "16", "18", "50", "51", "52", "53", "56", "58")):
        raise ValueError(f"{label}代码不在A股场内基金常用代码范围，请确认是ETF")
    name = str(raw.get("name") or "").strip()[:40]
    asset_class = str(raw.get("class") or "用户候选").strip()[:20]
    return {"code": code, "name": name, "class": asset_class}


def _canonical_us_etf(raw: Any, label: str = "ETF") -> dict[str, str]:
    if isinstance(raw, str):
        raw = {"code": raw}
    if not isinstance(raw, dict):
        raise ValueError(f"{label}配置格式无效")
    code = str(raw.get("code", "")).strip().upper()
    if not code or code.endswith(".HK") or not re.fullmatch(r"[A-Z0-9.-]+", code):
        raise ValueError(f"{label}代码必须是有效美股代码，且不能使用 .HK 后缀")
    name = str(raw.get("name") or "").strip()[:40]
    asset_class = str(raw.get("class") or "用户候选").strip()[:20]
    return {"code": code, "name": name, "class": asset_class}


def _canonical_hk_etf(raw: Any, label: str = "ETF") -> dict[str, str]:
    if isinstance(raw, str):
        raw = {"code": raw}
    if not isinstance(raw, dict):
        raise ValueError(f"{label}配置格式无效")
    code = str(raw.get("code", "")).strip().upper()
    digits = code[:-3] if code.endswith(".HK") else code
    if not digits.isdigit() or len(digits) not in (4, 5):
        raise ValueError(f"{label}代码必须是4位或5位港股代码，可带 .HK 后缀")
    code = f"{digits.zfill(4)}.HK"
    name = str(raw.get("name") or "").strip()[:40]
    asset_class = str(raw.get("class") or "用户候选").strip()[:20]
    return {"code": code, "name": name, "class": asset_class}


def _validate_assets(raw: Any, canonicalizer) -> list[dict[str, str]]:
    if not isinstance(raw, list):
        raise ValueError("ETF候选池必须是列表")
    if not 1 <= len(raw) <= 10:
        raise ValueError("ETF候选池应包含1至10只ETF")
    assets, seen = [], set()
    for item in raw:
        asset = canonicalizer(item)
        if asset["code"] in seen:
            raise ValueError(f"ETF候选池存在重复代码：{asset['code']}")
        seen.add(asset["code"])
        assets.append(asset)
    return assets


def _validate_b_assets(raw: Any) -> list[dict[str, str]]:
    return _validate_assets(raw, _canonical_a_etf)


def validate_params(key: str, raw: dict | None) -> dict:
    """Validate one partial/full strategy block and return canonical fields only."""
    if key not in DEFAULT_PARAMS:
        raise ValueError("未知策略配置")
    if not isinstance(raw, dict):
        raise ValueError("配置格式无效")

    base = json.loads(json.dumps(DEFAULT_PARAMS[key], ensure_ascii=False))
    if key in ("params_a", "params_c"):
        if "target_vol" in raw:
            target = _finite_number(raw["target_vol"], "波动率目标")
            if not 0.05 <= target <= 1.50:
                raise ValueError("波动率目标应在 0.05 至 1.50 之间")
            base["target_vol"] = round(target, 4)
        if "trend_window" in raw:
            trend_raw = _finite_number(raw["trend_window"], "趋势闸门 SMA")
            if not trend_raw.is_integer():
                raise ValueError("趋势闸门 SMA 必须是整数")
            trend = int(trend_raw)
            if not 20 <= trend <= 500:
                raise ValueError("趋势闸门 SMA 应在 20 至 500 日之间")
            base["trend_window"] = trend
        if "assets" in raw:
            canonicalizer = _canonical_us_etf if key == "params_a" else _canonical_hk_etf
            base["assets"] = _validate_assets(raw["assets"], canonicalizer)
    else:
        if "mom_window" in raw:
            mom_raw = _finite_number(raw["mom_window"], "动量窗口")
            if not mom_raw.is_integer():
                raise ValueError("动量窗口必须是整数")
            mom = int(mom_raw)
            if not 20 <= mom <= 500:
                raise ValueError("动量窗口应在 20 至 500 日之间")
            base["mom_window"] = mom
        if "ma_window" in raw:
            ma_raw = _finite_number(raw["ma_window"], "均线闸门")
            if not ma_raw.is_integer():
                raise ValueError("均线闸门必须是整数")
            ma = int(ma_raw)
            if not 20 <= ma <= 500:
                raise ValueError("均线闸门应在 20 至 500 日之间")
            base["ma_window"] = ma
        if "assets" in raw:
            base["assets"] = _validate_b_assets(raw["assets"])
        if "safe_asset" in raw:
            base["safe_asset"] = _canonical_a_etf(raw["safe_asset"], "防御ETF")
        risky_codes = {item["code"] for item in base["assets"]}
        if base["safe_asset"]["code"] in risky_codes:
            raise ValueError("防御ETF不能同时出现在用户候选池中")
    return base


def _migrate(raw: Any) -> dict[str, Any]:
    """Accept current schema plus older A/B/C or flat A/C parameter files."""
    if not isinstance(raw, dict):
        return {}
    migrated = dict(raw)
    aliases = {"A": "params_a", "B": "params_b", "C": "params_c",
               "a": "params_a", "b": "params_b", "c": "params_c"}
    for old, new in aliases.items():
        if new not in migrated and isinstance(raw.get(old), dict):
            migrated[new] = raw[old]
    if not any(k in migrated for k in DEFAULT_PARAMS):
        if "target_vol" in raw or "trend_window" in raw:
            migrated["params_a"] = raw
    return migrated


def load_settings() -> dict[str, dict[str, Any]]:
    settings = _deep_defaults()
    if not SETTINGS_FILE.exists():
        return settings
    try:
        raw = _migrate(json.loads(SETTINGS_FILE.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        return settings
    for key in DEFAULT_PARAMS:
        if isinstance(raw.get(key), dict):
            try:
                settings[key] = validate_params(key, raw[key])
            except ValueError:
                # Corrupt/old values fall back per block instead of breaking first visit.
                pass
    return settings


def save_settings(settings: dict[str, dict]) -> dict[str, dict]:
    clean = load_settings()
    for key in DEFAULT_PARAMS:
        if key in settings:
            clean[key] = validate_params(key, settings[key])
    if SETTINGS_FILE.exists():
        try:
            existing = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            if isinstance(existing, dict) and isinstance(existing.get("ext"), dict):
                clean["ext"] = existing["ext"]
        except (OSError, json.JSONDecodeError):
            pass
    tmp = SETTINGS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, SETTINGS_FILE)
    return clean


# ---------------------------------------------------------------------------
# Extended strategies ("ext" key) — independent of params_a/b/c above.
# Validation of ext params lives in ext_strategy; this store only does
# durable read/merge/write of the raw block with atomic replace.
# ---------------------------------------------------------------------------
def load_ext_settings() -> dict:
    """Return the raw 'ext' block, e.g. {"strategies": {sid: {...}}}. {} if absent/broken."""
    if not SETTINGS_FILE.exists():
        return {}
    try:
        raw = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    ext = raw.get("ext") if isinstance(raw, dict) else None
    return ext if isinstance(ext, dict) else {}


def save_ext_settings(ext: dict) -> dict:
    """Merge the 'ext' block into the settings file without touching A/B/C params."""
    if not isinstance(ext, dict):
        raise ValueError("ext 配置格式无效")
    base: dict = {}
    if SETTINGS_FILE.exists():
        try:
            loaded = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                base = loaded
        except (OSError, json.JSONDecodeError):
            base = {}
    base["ext"] = ext
    tmp = SETTINGS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(base, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, SETTINGS_FILE)
    return ext
