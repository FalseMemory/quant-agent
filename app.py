"""FastAPI app serving the quant-agent dashboard."""
from __future__ import annotations

import time
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from backend.engine import build_all
from backend import advisor
from backend import ai_advisor
from backend import settings_store
from backend import watchlist as wl_mod

ROOT = Path(__file__).resolve().parent

app = FastAPI(title="Quant Agent")

_SAVED_PARAMS = settings_store.load_settings()
_STATE: dict = {"data": None, **_SAVED_PARAMS, "error": None}


def _get_data(force: bool = False):
    if force or _STATE["data"] is None:
        try:
            _STATE["data"] = build_all(_STATE["params_a"], _STATE["params_b"], _STATE["params_c"])
            _STATE["error"] = None
        except Exception as e:  # noqa: BLE001
            _STATE["error"] = str(e)[:300]
    return _STATE


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
    candidate = {key: dict(value) for key, value in previous.items()}
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

    for key, value in candidate.items():
        _STATE[key] = value
    st = _get_data(force=True)
    if st["error"]:
        for key, value in previous.items():
            _STATE[key] = value
        _STATE["data"] = None
        return JSONResponse({"ok": False, "error": st["error"]}, status_code=500)
    try:
        saved = settings_store.save_settings(candidate)
    except (OSError, ValueError) as e:
        for key, value in previous.items():
            _STATE[key] = value
        _STATE["data"] = None
        return JSONResponse({"ok": False, "error": f"参数保存失败：{e}"}, status_code=500)
    return {"ok": True, "settings": saved, **st["data"]}


@app.get("/api/plan")
def plan(refresh_holdings_only: bool = False):
    st = _get_data()
    if st["error"]:
        return JSONResponse({"ok": False, "error": st["error"]}, status_code=500)
    return {"ok": True, **advisor.build_plan(st["data"])}


@app.get("/api/holdings")
def get_holdings():
    return {"ok": True, "holdings": advisor.load_holdings()}


class HoldingsBody(BaseModel):
    holdings: dict


@app.post("/api/holdings")
def post_holdings(body: HoldingsBody):
    saved = advisor.save_holdings(body.holdings)
    return {"ok": True, "holdings": saved}


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


@app.post("/api/ai/test")
def ai_test():
    """Quick connectivity check against the configured LLM endpoint."""
    cfg = ai_advisor.load_config()
    if not (cfg["base_url"] and cfg["api_key"] and cfg["model"]):
        return JSONResponse(
            {"ok": False, "error": "请先填写并保存 base_url / API Key / 模型名"},
            status_code=400)
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
    model_groups: list[str] | None = None   # 配置组: every saved profile in these groups runs
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
        common = dict(holdings=body.holdings or advisor.load_holdings(),
                      markets=body.markets, data_source=body.data_source,
                      news_source=body.news_source, views=body.views)
        if body.profiles or body.model_groups:
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


# --------------------------- user watchlist ---------------------------
@app.get("/api/watchlist")
def watchlist_get():
    try:
        return {"ok": True, "items": wl_mod.snapshots()}
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(e)[:200]}, status_code=500)


class WlCodeBody(BaseModel):
    code: str
    market: str = "auto"


@app.post("/api/watchlist/add")
def watchlist_add(body: WlCodeBody):
    try:
        items = wl_mod.add(body.code, body.market)
    except wl_mod.WatchError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    return {"ok": True, "items": items}


class WlRemoveBody(BaseModel):
    code: str


@app.post("/api/watchlist/remove")
def watchlist_remove(body: WlRemoveBody):
    return {"ok": True, "items": wl_mod.remove(body.code)}


@app.get("/")
def index():
    return FileResponse(ROOT / "static" / "index.html")


app.mount("/static", StaticFiles(directory=str(ROOT / "static")), name="static")


if __name__ == "__main__":  # allow: python app.py  (port via QUANT_AGENT_PORT env, default 8643)
    import os

    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("QUANT_AGENT_PORT", "8643")))
