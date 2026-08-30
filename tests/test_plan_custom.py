"""交易计划标的池快照 与 自定义起点收益对比 的测试。"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
import allure


# --------------------------------------------------------------------------- 交易计划：标的池快照
def _fake_data() -> dict:
    """build_plan 所需的最小 data 结构。"""
    vol = {"exposure": 80, "realized_vol": 20.0, "target_vol": 35.0,
           "trend_gate_on": True, "as_of": "2026-08-28", "trend_window": 200}
    return {
        "A": {"current": {**vol, "etf": "SPY 标普500ETF"}, "pool": [
            {"code": "SPY", "name": "标普500ETF", "etf": "SPY 标普500ETF",
             "above_ma": True, "momentum_score": 1.4},
        ], "failed_assets": [], "pool_limit": 10},
        "C": {"current": {**vol, "etf": "2800.HK 盈富基金"}, "pool": [
            {"code": "2800.HK", "name": "盈富基金", "etf": "2800.HK 盈富基金",
             "above_ma": True, "momentum_score": 0.9},
        ], "failed_assets": [], "pool_limit": 10},
        "B": {
            "current": {"etf": "510880 红利ETF", "pick_name": "红利ETF",
                        "reason": "动量最优", "decision_date": "2026-08-28"},
            "pool": [
                {"code": "sz159915", "name": "创业板ETF", "etf": "159915 创业板ETF", "above_ma": True, "momentum_score": 1.1},
                {"code": "sh510300", "name": "沪深300ETF", "etf": "510300 沪深300ETF", "above_ma": True, "momentum_score": 0.8},
                {"code": "sh510880", "name": "红利ETF", "etf": "510880 红利ETF", "above_ma": True, "momentum_score": 1.3},
                {"code": "sh518880", "name": "黄金ETF", "etf": "518880 黄金ETF", "above_ma": False, "momentum_score": -0.2},
            ],
            "safe_asset": {"code": "sh511010", "name": "国债ETF", "etf": "511010 国债ETF"},
            "failed_assets": [], "pool_limit": 10,
        },
    }


@pytest.fixture
def plan_env(tmp_path, monkeypatch):
    from backend import advisor, watchlist as wl_mod

    monkeypatch.setattr(advisor, "HOLDINGS_FILE", tmp_path / "holdings.json")
    monkeypatch.setattr(wl_mod, "snapshot_one",
                        lambda ref: {"close": 1.23, "chg_1d_pct": 0.5, "chg_5d_pct": -1.0,
                                     "chg_20d_pct": 2.0, "realized_vol_20d_pct": 18.0,
                                     "vs_sma50_pct": 3.0, "vs_sma200_pct": 8.0,
                                     "as_of": "2026-08-28"})
    return advisor


@allure.feature("交易计划")
@allure.story("标的池全景快照")
def test_plan_pool_includes_every_pool_asset(plan_env):
    """B 的计划必须带出全部池内标的快照（含黄金ETF），而不仅是持仓/目标标的。"""
    plan = plan_env.build_plan(_fake_data())
    pool = plan["plans"]["B"]["pool"]
    labels = " ".join(x["asset"] for x in pool)

    allure.attach(labels, "B 池内标的", allure.attachment_type.TEXT)
    assert "518880" in labels, f"黄金ETF 未出现在交易计划标的池中：{labels}"
    assert len(pool) == 5, f"B 池应有 5 个标的，实际 {len(pool)}"
    assert all(x["snap"] for x in pool), "每个池内标的都应带行情快照"


@allure.feature("交易计划")
@allure.story("跨市场动态池")
def test_a_c_targets_and_legacy_holdings_follow_dynamic_pool(plan_env):
    holdings = {
        "A": {"TQQQ": 20, "现金": 80},
        "B": {"510880 红利ETF": 100},
        "C": {"7200.HK": 30, "现金": 70},
    }
    plan = plan_env.build_plan(_fake_data(), holdings)

    assert plan["plans"]["A"]["target"] == {"SPY 标普500ETF": 80.0, "现金": 20.0}
    assert plan["plans"]["C"]["target"] == {"2800.HK 盈富基金": 80.0, "现金": 20.0}
    assert plan["plans"]["A"]["legacy_holdings"] == [{"asset": "TQQQ", "weight": 20.0}]
    assert plan["plans"]["C"]["legacy_holdings"] == [{"asset": "7200.HK", "weight": 30.0}]
    assert plan["plans"]["A"]["pool"][0]["above_ma"] is True
    assert plan["plans"]["C"]["pool"][0]["momentum_score"] == 0.9


@allure.feature("交易计划")
@allure.story("标的池全景快照")
def test_plan_pool_snapshot_failure_is_isolated(plan_env, monkeypatch):
    """单个标的行情失败时，该条目 snap 为空，但整个计划不受影响。"""
    from backend import watchlist as wl_mod

    def flaky(ref):
        if ref["code"].endswith("518880"):
            raise RuntimeError("行情超时")
        return {"close": 1.0, "chg_1d_pct": 0.0, "chg_5d_pct": 0.0, "chg_20d_pct": 0.0,
                "realized_vol_20d_pct": 0.0, "vs_sma50_pct": 0.0, "vs_sma200_pct": 0.0,
                "as_of": "2026-08-28"}

    monkeypatch.setattr(wl_mod, "snapshot_one", flaky)
    plan = plan_env.build_plan(_fake_data())
    pool = plan["plans"]["B"]["pool"]

    gold = next(x for x in pool if "518880" in x["asset"])
    assert gold["snap"] is None, "失败标的的快照应为空"
    assert gold["quote_error"] == "行情超时", "失败原因应透传给前端诊断"
    assert plan["plans"]["B"]["headline"], "单个标的失败不应影响计划生成"


@allure.feature("交易计划")
@allure.story("轻量行情兜底")
def test_snapshot_uses_light_quote_when_history_fails(monkeypatch):
    from backend import data_feed as dfd
    from backend import watchlist as wl_mod

    monkeypatch.setattr(dfd, "get_us", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("历史源失败")))
    monkeypatch.setattr(dfd, "get_quote", lambda *args, **kwargs: {
        "price": 26.5, "prev_close": 26.0, "name": "盈富基金",
        "source": "腾讯行情", "as_of": "20260828160000",
    })
    snap = wl_mod.snapshot_one({"code": "2800.HK", "market": "hk"})
    assert snap["quote_level"] == "light"
    assert snap["close"] == 26.5
    assert snap["chg_1d_pct"] == 1.92
    assert snap["source"] == "腾讯行情"


# --------------------------------------------------------------------------- 自定义起点收益
@pytest.fixture
def raw_curves():
    """合成 500 个交易日的净值曲线：策略稳定上行，基准先跌后涨。"""
    idx = pd.date_range("2024-01-01", periods=500, freq="B")
    equity = pd.Series(np.cumprod(1 + np.full(500, 0.001)), index=idx)
    bench = pd.Series(np.cumprod(1 + np.concatenate([
        np.full(250, -0.0005), np.full(250, 0.0015)])), index=idx)
    weights = pd.Series(np.where(np.arange(500) % 40 < 20, 0.6, 0.0), index=idx)
    return {"index": idx, "equity": equity, "bench": bench,
            "weights": weights, "picks": None, "asset_label": "TQQQ",
            "bench_name": "TQQQ 买入持有"}


@allure.feature("自定义区间")
@allure.story("起算口径")
def test_custom_window_rebases_to_one(raw_curves):
    from backend import engine

    engine.RAW["A"] = raw_curves
    try:
        payload = engine.custom_window("A", "2024-06-01")
    finally:
        engine.RAW.pop("A", None)

    allure.attach(str({k: payload[k] for k in ("start", "end",
                                               "strategy", "benchmark")}), "结果",
                  allure.attachment_type.TEXT)
    assert payload["equity"][0] == 1.0, "自定义区间起点应归一为 1"
    assert payload["bench_equity"][0] == 1.0
    assert payload["start"] == "2024-06-03", "非交易日应顺延到下一个数据日"
    assert payload["end"] == "2025-10-01" or payload["end"] >= "2025-09-30"
    assert payload["bench_name"] == "TQQQ 买入持有"
    assert len(payload["dates"]) == len(payload["equity"]) == len(payload["bench_equity"])
    assert len(payload["dates"]) == len(payload["drawdown"]) == len(payload["positions"])
    assert all(sum(item["weight"] for item in pos["items"]) == 100
               for pos in payload["positions"]), "风险资产与现金仓位合计必须为 100%"
    assert any(pos["cash_weight"] == 40 for pos in payload["positions"])


@allure.feature("自定义区间")
@allure.story("起算口径")
def test_custom_window_beats_benchmark_when_starting_after_dip(raw_curves):
    """从基准低点后起算时，策略应跑赢基准（合成数据的确定性结论）。"""
    from backend import engine

    engine.RAW["A"] = raw_curves
    try:
        payload = engine.custom_window("A", "2024-06-01")
    finally:
        engine.RAW.pop("A", None)

    s = payload["strategy"]["total_return"]
    b = payload["benchmark"]["total_return"]
    assert s > b, f"该起点的合成数据下策略({s}%)应跑赢基准({b}%)"


@allure.feature("自定义区间")
@allure.story("异常输入")
def test_custom_window_rejects_bad_inputs(raw_curves):
    from backend import engine

    engine.RAW["A"] = raw_curves
    try:
        for start in ("2099-01-01", "not-a-date"):
            with allure.step(f"非法输入：{start}"):
                try:
                    engine.custom_window("A", start)
                except ValueError as e:
                    allure.attach(str(e), "拒绝原因", allure.attachment_type.TEXT)
                    continue
                raise AssertionError(f"{start} 应被拒绝")
        with allure.step("未构建的策略"):
            try:
                engine.custom_window("Z", "2024-01-01")
            except ValueError:
                pass
            else:
                raise AssertionError("未构建的策略应被拒绝")
    finally:
        engine.RAW.pop("A", None)


@allure.feature("自定义区间")
@allure.story("接口")
def test_custom_endpoint_roundtrip(client, monkeypatch):
    """/api/custom 全链路：合成 RAW -> 200 返回 payload -> 前端可直接作为窗口渲染。"""
    from backend import engine as eng

    idx = pd.date_range("2024-01-01", periods=300, freq="B")
    eng.RAW["A"] = {"index": idx,
                    "equity": pd.Series(np.cumprod(1 + np.full(300, 0.001)), index=idx),
                    "bench": pd.Series(np.cumprod(1 + np.full(300, 0.0005)), index=idx)}
    try:
        r = client.get("/api/custom", params={"sid": "A", "start": "2024-03-01"})
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        p = body["payload"]
        assert p["equity"][0] == 1.0 and p["bench_equity"][0] == 1.0
        assert p["strategy"]["total_return"] > p["benchmark"]["total_return"]
    finally:
        eng.RAW.pop("A", None)


@allure.feature("自定义区间")
@allure.story("接口")
def test_custom_endpoint_rejects_unknown_sid(client):
    r = client.get("/api/custom", params={"sid": "Z", "start": "2024-03-01"})

    assert r.status_code == 400
