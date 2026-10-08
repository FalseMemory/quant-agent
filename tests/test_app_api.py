"""接口层测试：核心与扩展端点行为、参数校验与错误码。

说明：为保证离线与可重复，策略构建通过 monkeypatch 替换为内存桩数据；
配置文件统一重定向到临时目录，绝不触碰用户真实配置。
`client` 夹具定义在 conftest.py，供多个测试文件共享。
"""
from __future__ import annotations

import allure

from conftest import phased_frame, synth_frame


# --------------------------------------------------------------------------- 核心接口
@allure.feature("接口")
@allure.story("健康检查")
def test_health_returns_fixed_safe_payload(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


@allure.feature("接口")
@allure.story("核心摘要")
def test_summary_returns_three_strategies_and_settings(client):
    r = client.get("/api/summary")
    assert r.status_code == 200
    body = r.json()

    allure.attach(str(list(body.keys())), "返回字段", allure.attachment_type.TEXT)
    assert body["ok"] is True
    for sid in ("A", "B", "C"):
        assert sid in body and body[sid]["windows"]["full"]["strategy"]["total_return"] is not None
    assert set(body["settings"].keys()) == {"params_a", "params_b", "params_c"}


@allure.feature("接口")
@allure.story("核心摘要")
def test_summary_b_exposure_reflects_fixed_single_position(client):
    """接口层复核：B 的平均暴露必须是 100%，防止权重叠加缺陷回归。"""
    body = client.get("/api/summary").json()
    exposure = body["B"]["windows"]["full"]["strategy"]["avg_exposure"]

    allure.attach(f"B avg_exposure = {exposure}%", "暴露度", allure.attachment_type.TEXT)
    assert exposure == 100.0


@allure.feature("接口")
@allure.story("核心重跑")
def test_rerun_accepts_valid_params_without_server_persistence(client, tmp_settings_file):
    import json

    r = client.post("/api/rerun", json={
        "params_a": {"target_vol": 0.38, "trend_window": 190}})
    assert r.status_code == 200
    body = r.json()

    allure.attach(json.dumps(body["settings"], ensure_ascii=False), "保存后的配置",
                  allure.attachment_type.JSON)
    assert body["settings"]["params_a"]["target_vol"] == 0.38
    assert body["settings"]["params_a"]["trend_window"] == 190
    assert body["settings"]["params_a"]["assets"][0]["code"] == "TQQQ"
    assert body["settings"]["params_a"]["assets"][0]["name"] != "TQQQ"
    assert body["settings"]["params_c"]["target_vol"] == 0.40
    assert body["settings"]["params_c"]["trend_window"] == 200
    assert body["settings"]["params_c"]["assets"], "修改 A 不应覆盖 C 的候选池"
    assert not tmp_settings_file.exists(), "浏览器策略参数不得写入服务器文件"


@allure.feature("接口")
@allure.story("核心重跑")
def test_rerun_refresh_flag_reaches_the_data_layer(client, monkeypatch):
    """「刷新数据」的 refresh 必须一路传到数据层。

    回归背景：缓存曾经只看 6 小时定时器，force 又传不下去，于是收盘后点刷新
    只是回放旧 CSV，右上角日期纹丝不动。
    """
    import app as app_mod
    from backend import data_feed as dfd

    seen: list[bool] = []
    original = app_mod.build_all

    def spy(*args, **kwargs):
        seen.append(dfd._FORCE.get())
        return original(*args, **kwargs)

    monkeypatch.setattr(app_mod, "build_all", spy)

    client.post("/api/rerun", json={"refresh": True})
    client.post("/api/rerun", json={})

    allure.attach(str(seen), "build_all 见到的 force 值", allure.attachment_type.TEXT)
    assert seen[-2:] == [True, False], "refresh=true 应透传，缺省应保持 False"


@allure.feature("接口")
@allure.story("核心摘要")
def test_summary_force_param_reaches_the_data_layer(client, monkeypatch):
    import app as app_mod
    from backend import data_feed as dfd

    seen: list[bool] = []
    original = app_mod.build_all

    def spy(*args, **kwargs):
        seen.append(dfd._FORCE.get())
        return original(*args, **kwargs)

    monkeypatch.setattr(app_mod, "build_all", spy)

    app_mod._STATE["data"] = None
    client.get("/api/summary?force=true")
    assert seen[-1] is True, "force=true 应透传到数据层"

    app_mod._STATE["data"] = None
    client.get("/api/summary")
    assert seen[-1] is False, "普通加载不应强制重抓"


@allure.feature("接口")
@allure.story("核心摘要")
def test_summary_force_param_invalidates_memory_cache(client, monkeypatch):
    """不带 force 时命中内存缓存不重算；带 force 时必须重算。"""
    import app as app_mod

    calls: list[int] = []
    original = app_mod.build_all

    def spy(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(app_mod, "build_all", spy)
    app_mod._STATE["data"] = None

    client.get("/api/summary")
    assert len(calls) == 1, "首次访问应构建"

    client.get("/api/summary")
    assert len(calls) == 1, "内存缓存命中时不应重复构建"

    client.get("/api/summary?force=true")
    assert len(calls) == 2, "force 应强制重建"


@allure.feature("接口")
@allure.story("ETF名称自动补全")
def test_rerun_resolves_blank_name_and_preserves_manual_name(client, monkeypatch):
    from backend import watchlist

    monkeypatch.setattr(watchlist, "fetch_name", lambda item: {
        "SPY": "SPDR标普500ETF", "QQQ": "纳指100ETF",
    }.get(item["code"]))
    r = client.post("/api/rerun", json={"params_a": {
        "target_vol": 0.35, "trend_window": 200,
        "assets": [
            {"code": "SPY", "name": "SPY"},
            {"code": "QQQ", "name": "用户自定义名称"},
        ],
    }})
    assert r.status_code == 200
    assets = r.json()["settings"]["params_a"]["assets"]
    assert assets[0]["name"] == "SPDR标普500ETF"
    assert assets[1]["name"] == "用户自定义名称"


@allure.feature("接口")
@allure.story("核心重跑")
def test_rerun_rejects_invalid_params_with_400(client):
    r = client.post("/api/rerun", json={"params_a": {"target_vol": 9}})

    allure.attach(r.text, "错误响应", allure.attachment_type.TEXT)
    assert r.status_code == 400
    assert r.json()["ok"] is False


@allure.feature("接口")
@allure.story("设置读取")
def test_settings_endpoint_returns_current_params(client):
    body = client.get("/api/settings").json()

    assert body["ok"] is True
    assert body["params_a"]["target_vol"] == 0.35


# --------------------------------------------------------------------------- 扩展接口
@allure.feature("接口")
@allure.story("扩展摘要")
def test_ext_summary_shape(client):
    from backend import ext_strategy as ex

    body = client.get("/api/ext/summary").json()

    allure.attach(str({k: v["status"] for k, v in body["results"].items()}), "策略状态",
                  allure.attachment_type.TEXT)
    assert body["ok"] is True
    assert len(body["groups"]) == len(ex.EXT_GROUPS)
    assert len(body["results"]) == len(ex.EXT_STRATEGIES)
    assert len(body["strategies"]) == len(ex.EXT_STRATEGIES), "注册表元信息不应被结果覆盖"
    sample = body["strategies"]["us_qqq_vt"]
    assert "default_params" in sample and "description" in sample


@allure.feature("接口")
@allure.story("扩展重跑")
def test_ext_rerun_updates_single_strategy(client, tmp_settings_file):
    import json

    r = client.post("/api/ext/rerun", json={
        "strategy_id": "us_spy_dma", "params": {"fast": 30, "slow": 100}})
    assert r.status_code == 200
    body = r.json()

    allure.attach(json.dumps(body["strategy"]["params"], ensure_ascii=False), "策略参数",
                  allure.attachment_type.JSON)
    assert body["strategy"]["params"] == {"fast": 30, "slow": 100}
    saved = json.loads(tmp_settings_file.read_text(encoding="utf-8"))
    assert saved["ext"]["strategies"]["us_spy_dma"]["params"]["fast"] == 30


@allure.feature("接口")
@allure.story("扩展重跑")
def test_ext_rerun_rejects_invalid_params(client):
    r = client.post("/api/ext/rerun", json={
        "strategy_id": "us_spy_dma", "params": {"fast": 900, "slow": 100}})

    allure.attach(r.text, "错误响应", allure.attachment_type.TEXT)
    assert r.status_code == 400
    assert "快线" in r.json()["error"]


@allure.feature("接口")
@allure.story("扩展重跑")
def test_ext_rerun_rejects_unknown_strategy(client):
    r = client.post("/api/ext/rerun", json={"strategy_id": "ghost", "params": {}})

    assert r.status_code == 400


@allure.feature("接口")
@allure.story("扩展重跑")
def test_ext_rerun_disable_marks_disabled(client, tmp_settings_file):
    import json

    body = client.post("/api/ext/rerun", json={
        "strategy_id": "hk_vt", "enabled": False}).json()

    allure.attach(str(body["strategy"]["status"]), "状态", allure.attachment_type.TEXT)
    assert body["strategy"]["status"] == "disabled"
    saved = json.loads(tmp_settings_file.read_text(encoding="utf-8"))
    assert saved["ext"]["strategies"]["hk_vt"]["enabled"] is False

    back = client.get("/api/ext/summary").json()
    assert back["results"]["hk_vt"]["enabled"] is False
    assert back["results"]["us_qqq_vt"]["enabled"] is True, "停用一套不应影响其他策略"


@allure.feature("接口")
@allure.story("浏览器持仓")
def test_holdings_validation_is_stateless(client, tmp_settings_file, monkeypatch):
    from backend import advisor

    target = tmp_settings_file.parent / "holdings.json"
    monkeypatch.setattr(advisor, "HOLDINGS_FILE", target)
    r = client.post("/api/holdings/validate", json={
        "holdings": {"A": {"TQQQ": 60, "现金": 40}}})

    assert r.status_code == 200
    assert r.json()["holdings"]["A"]["TQQQ"] == 60
    assert not target.exists(), "浏览器持仓校验不得写入服务器文件"


@allure.feature("接口")
@allure.story("浏览器持仓")
def test_holdings_rejects_non_numeric_weight_with_json_400(client):
    r = client.post("/api/holdings/validate", json={
        "holdings": {"A": {"TQQQ": "非法"}}})

    assert r.status_code == 400
    assert r.headers["content-type"].startswith("application/json")
    assert "必须是数值" in r.json()["error"]
