"""Stateless browser-supplied AI configuration execution."""
from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor

from . import ai_advisor


def clean_config(raw: dict) -> dict:
    if not isinstance(raw, dict):
        raise ai_advisor.AIError("模型配置必须是对象")
    cfg = {
        "profile": str(raw.get("name") or raw.get("profile") or "默认模型").strip()[:80],
        "group": str(raw.get("group") or "默认组").strip()[:80],
        "base_url": str(raw.get("base_url") or "").strip().rstrip("/"),
        "api_key": str(raw.get("api_key") or "").strip(),
        "model": str(raw.get("model") or "").strip()[:160],
    }
    if not cfg["base_url"].startswith(("http://", "https://")):
        raise ai_advisor.AIError("base_url 必须以 http:// 或 https:// 开头")
    if not cfg["api_key"] or not cfg["model"]:
        raise ai_advisor.AIError(f"模型配置「{cfg['profile']}」缺少 API Key 或模型名")
    return cfg


def decide_many(summary: dict, plan_payload: dict, configs: list[dict], **common) -> dict:
    clean = [clean_config(cfg) for cfg in configs]
    if not clean:
        raise ai_advisor.AIError("未提供任何模型配置")
    try:
        workers = max(1, min(len(clean), int(os.environ.get("QUANT_MULTI_WORKERS", "4"))))
    except ValueError:
        workers = min(len(clean), 4)
    results: list[dict | None] = [None] * len(clean)
    errors: list[dict | None] = [None] * len(clean)

    def run_one(index: int, cfg: dict) -> None:
        try:
            results[index] = ai_advisor.decide(
                summary, plan_payload, config=cfg, persist_audit=False, **common)
        except Exception as exc:  # noqa: BLE001
            errors[index] = {"profile": cfg["profile"], "error": str(exc)[:240]}

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="browser-llm") as pool:
        futures = [pool.submit(run_one, index, cfg) for index, cfg in enumerate(clean)]
        for future in futures:
            future.result()

    ok_results = [item for item in results if item is not None]
    clean_errors = [item for item in errors if item is not None]
    if not ok_results:
        raise ai_advisor.AIError(clean_errors[0]["error"] if clean_errors else "全部模型均失败")
    if len(clean) == 1 and len(ok_results) == 1:
        return ok_results[0]
    return {
        "ok": True,
        "multi": True,
        "parallel": workers > 1,
        "workers": workers,
        "results": ok_results,
        "errors": clean_errors,
    }
