"""Persistent, validated strategy settings with legacy-data compatibility."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
SETTINGS_FILE = ROOT / "strategy_settings.json"

DEFAULT_PARAMS = {
    "params_a": {"target_vol": 0.35, "trend_window": 200},
    "params_b": {"mom_window": 120, "ma_window": 60},
    "params_c": {"target_vol": 0.40, "trend_window": 200},
}


def _deep_defaults() -> dict[str, dict[str, float | int]]:
    return {key: dict(value) for key, value in DEFAULT_PARAMS.items()}


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


def validate_params(key: str, raw: dict | None) -> dict[str, float | int]:
    """Validate one partial/full strategy block and return canonical fields only."""
    if key not in DEFAULT_PARAMS:
        raise ValueError("未知策略配置")
    if not isinstance(raw, dict):
        raise ValueError("配置格式无效")

    base = dict(DEFAULT_PARAMS[key])
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


def load_settings() -> dict[str, dict[str, float | int]]:
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


def save_settings(settings: dict[str, dict]) -> dict[str, dict[str, float | int]]:
    clean = _deep_defaults()
    for key in DEFAULT_PARAMS:
        clean[key] = validate_params(key, settings.get(key, {}))
    tmp = SETTINGS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, SETTINGS_FILE)
    return clean
