"""FastAPI app serving the quant-agent dashboard."""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from backend.engine import build_all
from backend import engine
from backend import advisor
from backend import ai_advisor
from backend import browser_ai
from backend import settings_store
from backend import ext_strategy
from backend import watchlist

ROOT = Path(__file__).resolve().parent

app = FastAPI(title="Quant Agent")

_SAVED_PARAMS = settings_store.load_settings()

# Resolve genuine security names once at boot so the very first page load shows
# "TQQQ 3倍做多纳斯达克100ETF" instead of repeating the code. Skipped under
# pytest (keeps the suite offline and deterministic); set
# QUANT_AGENT_RESOLVE_NAMES=0 to disable it elsewhere.
if "pytest" not in sys.modules and os.environ.get(
    "QUANT_AGENT_RESOLVE_NAMES", "1"
) == "1":
    try:
        _SAVED_PARAMS = watchlist.resolve_asset_names(_SAVED_PARAMS)
    except Exception:  # noqa: BLE001
        pass

_STATE: dict = {"data": None, **_SAVED_PARAMS, "error": None}


def _get_data(force: bool = False):
    if force or _STATE["data"] is None:
        try:
            _STATE["data"] = build_all(_STATE["params_a"], _STATE["params_b"], _STATE["params_c"])
            _STATE["error"] = None
        except Exception as e:  # noqa: BLE001
            _STATE["error"] = str(e)[:300]
    return _STATE


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/api/summary")
def summary(force: bool = False):
    st = _get_data(force=force)
    if st["error"]:
        return JSONResponse({"ok": False, "error": st["error"]}, status_code=500)
    return {"ok": True, "settings": _current_settings(), **st["data"]}


def _current_settings() -> dict:
    return {key: dict(_STATE[key]) for key in ("params_a", "params_b", "params_c")}


@app.get("/api/settings")
def settings_get():
    return {"ok": True, **_current_settings()}


class Params(BaseModel):
    params_a: dict | None = None
    params_b: dict | None = None
    params_c: dict | None = None


@app.post("/api/rerun")
def rerun(body: Params):
    previous = _current_settings()
    candidate = {
        key: settings_store.validate_params(key, value)
        for key, value in previous.items()
    }
    updates = {
        "params_a": body.params_a,
        "params_b": body.params_b,
        "params_c": body.params_c,
    }
    try:
        for key, raw in updates.items():
            if raw is not None:
                candidate[key] = settings_store.validate_params(key, raw)
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)

    # Blank names are resolved only after code validation. Manual names are preserved.
    candidate = watchlist.resolve_asset_names(candidate)

    try:
        data = build_all(candidate["params_a"], candidate["params_b"], candidate["params_c"])
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(e)[:300]}, status_code=500)
    # Browser clients own persistence. Keep only the latest calculation in memory
    # so custom-window requests can reuse RAW curves; never write user settings.
    for key, value in candidate.items():
        _STATE[key] = value
    _STATE["data"] = data
    _STATE["error"] = None
    return {"ok": True, "settings": candidate, **data}


# ------------------- extended instrument groups & strategies -------------------
# Additive layer: independent of the core A/B/C path. A failure in any extended
# strategy is contained to that strategy and never touches /api/summary.
_EXT_STATE: dict = {"data": None}


def _get_ext_data(force: bool = False):
    if force or _EXT_STATE["data"] is None:
        _EXT_STATE["data"] = ext_strategy.build_all_ext(settings_store.load_ext_settings())
    return _EXT_STATE["data"]


@app.get("/api/ext/summary")
def ext_summary(force: bool = False):
    data = _get_ext_data(force=force)
    # NOTE: ext_meta() already carries a "strategies" key (registry metadata),
    # so the per-strategy backtest payloads are returned under "results".
    return {"ok": True, **ext_strategy.ext_meta(),
            "settings": data["settings"], "results": data["strategies"]}


class ExtBody(BaseModel):
    strategy_id: str
    enabled: bool | None = None
    params: dict | None = None


@app.post("/api/ext/rerun")
def ext_rerun(body: ExtBody):
    """Update (enable/params) ONE extended strategy, persist it, rebuild only that one."""
    sid = body.strategy_id
    if sid not in ext_strategy.EXT_STRATEGIES:
        return JSONResponse({"ok": False, "error": "未知的扩展策略"}, status_code=400)
    cfg = settings_store.load_ext_settings()
    strategies = cfg.get("strategies") or {}
    current = strategies.get(sid) or {}
    enabled = bool(body.enabled) if body.enabled is not None else bool(current.get("enabled", True))
    raw = body.params if body.params is not None else current.get("params")
    try:
        params = ext_strategy.validate_ext_params(sid, raw)
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)

    strategies[sid] = {"enabled": enabled, "params": params}
    try:
        settings_store.save_ext_settings({**cfg, "strategies": strategies})
    except (OSError, ValueError) as e:
        return JSONResponse({"ok": False, "error": f"参数保存失败：{e}"}, status_code=500)

    result = ext_strategy.build_one(sid, params, enabled)
    data = _get_ext_data()
    data["settings"]["strategies"][sid] = {"enabled": enabled, "params": params}
    data["strategies"][sid] = result
    return {"ok": True, "settings": data["settings"], "strategy": result}


@app.get("/api/custom")
def custom(sid: str, start: str):
    """从指定日期起算的策略 vs 基准对比（起买模拟，含成本假设的回测曲线切片）。"""
    st = _get_data()
    if st["error"]:
        return JSONResponse({"ok": False, "error": st["error"]}, status_code=500)
    if sid not in ("A", "B", "C"):
        return JSONResponse({"ok": False, "error": "未知策略"}, status_code=400)
    try:
        payload = engine.custom_window(sid, start)
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    return {"ok": True, "sid": sid, "payload": payload}


class HoldingsBody(BaseModel):
    holdings: dict


@app.post("/api/plan")
def plan(body: HoldingsBody):
    st = _get_data()
    if st["error"]:
        return JSONResponse({"ok": False, "error": st["error"]}, status_code=500)
    try:
        holdings = advisor.normalize_holdings(body.holdings)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return {"ok": True, **advisor.build_plan(st["data"], holdings=holdings)}


@app.post("/api/holdings/validate")
def validate_holdings(body: HoldingsBody):
    try:
        clean = advisor.normalize_holdings(body.holdings)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return {"ok": True, "holdings": clean}


# --------------------------- AI decision layer ---------------------------
@app.get("/api/ai/config")
def ai_config_get():
    return {"ok": True, **ai_advisor.masked_config()}


class AiConfigBody(BaseModel):
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    profile: str = "默认模型"
    group: str = "默认组"
    original_name: str | None = None  # set when renaming an existing config


@app.post("/api/ai/config")
def ai_config_post(body: AiConfigBody):
    api_key = body.api_key
    if not api_key.strip():  # blank key -> keep key of the same profile, or the pre-rename one
        api_key = ai_advisor.load_config(body.profile).get("api_key", "")
        if not api_key and body.original_name:
            api_key = ai_advisor.load_config(body.original_name).get("api_key", "")
    try:
        ai_advisor.save_config(body.base_url, api_key, body.model, body.profile, body.group)
    except ai_advisor.AIError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    return {"ok": True, **ai_advisor.masked_config()}


class AiDeleteBody(BaseModel):
    profile: str


@app.post("/api/ai/config/delete")
def ai_config_delete(body: AiDeleteBody):
    try:
        masked = ai_advisor.delete_config(body.profile)
    except ai_advisor.AIError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    return {"ok": True, **masked}


class AiTestBody(BaseModel):
    config: dict


@app.post("/api/ai/test")
def ai_test(body: AiTestBody):
    """Test a browser-supplied model config without persisting its API Key."""
    try:
        cfg = browser_ai.clean_config(body.config)
    except ai_advisor.AIError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    t0 = time.time()
    try:
        raw = ai_advisor.call_llm(
            cfg, [{"role": "user", "content": "请只回复四个字：连接正常"}], timeout=60)
    except ai_advisor.AIError as e:
        return JSONResponse({"ok": False, "error": str(e), "elapsed_s": round(time.time() - t0, 1)},
                            status_code=400)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": f"连接失败：{str(e)[:200]}",
                             "elapsed_s": round(time.time() - t0, 1)}, status_code=500)
    return {"ok": True, "model": cfg["model"], "reply": raw[:80],
            "elapsed_s": round(time.time() - t0, 1)}


@app.get("/api/ai/context")
def ai_context():
    """Preview exactly what would be fed to the LLM (transparency)."""
    st = _get_data()
    if st["error"]:
        return JSONResponse({"ok": False, "error": st["error"]}, status_code=500)
    plan = advisor.build_plan(st["data"])
    try:
        ctx = ai_advisor.build_context(st["data"], plan)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(e)[:300]}, status_code=500)
    return {"ok": True, "context": ctx}


class AiDecideBody(BaseModel):
    holdings: dict | None = None
    markets: list[str] | None = None
    model_profile: str | None = None
    profiles: list[str] | None = None       # explicit model names
    model_groups: list[str] | None = None   # legacy local-server compatibility
    model_configs: list[dict] | None = None # browser-owned configs, used once and never stored
    data_source: str = "auto"
    news_source: str = "sina"
    views: list[str] | None = None


@app.post("/api/ai/decide")
def ai_decide(body: AiDecideBody):
    st = _get_data()
    if st["error"]:
        return JSONResponse({"ok": False, "error": st["error"]}, status_code=500)
    try:
        plan = advisor.build_plan(st["data"], holdings=body.holdings or None)
        common = dict(holdings=body.holdings or {}, markets=body.markets,
                      data_source=body.data_source, news_source=body.news_source,
                      views=body.views)
        if body.model_configs:
            result = browser_ai.decide_many(st["data"], plan, body.model_configs, **common)
        elif body.profiles or body.model_groups:
            result = ai_advisor.decide_multi(st["data"], plan, profiles=body.profiles,
                                             model_groups=body.model_groups, **common)
        else:
            result = ai_advisor.decide(st["data"], plan,
                                       model_profile=body.model_profile, **common)
    except ai_advisor.AIError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": f"AI 决策失败：{str(e)[:300]}"}, status_code=500)
    return result


@app.get("/api/ai/history")
def ai_history(limit: int = 50):
    return {"ok": True, "history": ai_advisor.load_history(limit)}


@app.get("/api/ai/history/{decision_id}")
def ai_history_detail(decision_id: str):
    item = ai_advisor.get_history_detail(decision_id)
    if not item:
        return JSONResponse({"ok": False, "error": "找不到该决策记录"}, status_code=404)
    return {"ok": True, "record": item}


@app.get("/")
def index():
    return FileResponse(ROOT / "static" / "index.html")


app.mount("/static", StaticFiles(directory=str(ROOT / "static")), name="static")


if __name__ == "__main__":  # local: QUANT_AGENT_PORT; CloudBase Run: PORT
    import os

    import uvicorn

    port = int(os.environ.get("PORT") or os.environ.get("QUANT_AGENT_PORT", "8643"))
    uvicorn.run(app, host="0.0.0.0", port=port)
