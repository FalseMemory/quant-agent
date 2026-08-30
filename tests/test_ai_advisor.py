"""AI 动态用户池与护栏测试。"""
from __future__ import annotations

import allure


@allure.feature("AI交易指令")
@allure.story("动态ETF池")
def test_b_ai_assets_come_from_current_user_pool():
    from backend import ai_advisor

    summary = {"B": {
        "pool": [
            {"code": "sh510300", "name": "沪深300ETF", "etf": "510300 沪深300ETF",
             "above_ma": True, "momentum_score": 1.2},
            {"code": "sz159915", "name": "创业板ETF", "etf": "159915 创业板ETF",
             "above_ma": False, "momentum_score": -0.4},
        ],
        "safe_asset": {"code": "sh511010", "name": "国债ETF", "etf": "511010 国债ETF"},
        "failed_assets": [], "pool_limit": 10,
        "current": {"ma_window": 120, "mom_window": 180},
    }}

    known, snapshots, state = ai_advisor._b_ai_assets(summary)

    assert known == ["510300 沪深300ETF", "159915 创业板ETF", "511010 国债ETF"]
    assert {x[0] for x in snapshots} == {"sh510300", "sz159915", "sh511010"}
    assert state["ma_window"] == 120
    assert state["risk_pool"][1]["above_ma"] is False


@allure.feature("AI交易指令")
@allure.story("动态ETF池")
def test_a_c_ai_assets_come_from_each_current_pool():
    from backend import ai_advisor

    summary = {
        "A": {"pool": [{"code": "SPY", "etf": "SPY 标普500ETF"}],
              "failed_assets": [], "current": {"trend_window": 120, "target_vol": 35}},
        "C": {"pool": [{"code": "2800.HK", "etf": "2800.HK 盈富基金"}],
              "failed_assets": [], "current": {"trend_window": 180, "target_vol": 40}},
    }
    a_known, a_snaps, a_state = ai_advisor._strategy_ai_assets(summary, "A")
    c_known, c_snaps, c_state = ai_advisor._strategy_ai_assets(summary, "C")

    assert a_known == ["SPY 标普500ETF"] and a_snaps[0][:2] == ("SPY", "us")
    assert c_known == ["2800.HK 盈富基金"] and c_snaps[0][:2] == ("2800.HK", "hk")
    assert a_state["trend_window"] == 120
    assert c_state["target_vol"] == 40


@allure.feature("AI交易指令")
@allure.story("市场隔离")
def test_build_context_only_requests_selected_market_pool(monkeypatch):
    from backend import ai_advisor

    metrics = {"total_return": 0.0, "max_dd": 0.0}
    summary = {
        sid: {"pool": [{"code": code, "etf": label}], "failed_assets": [],
              "current": {}, "windows": {"full": {"label": "全历史", "strategy": metrics,
                                                       "benchmark": metrics, "beats_benchmark": False}}}
        for sid, code, label in (("A", "SPY", "SPY 标普500ETF"),
                                 ("B", "sh510300", "510300 沪深300ETF"),
                                 ("C", "2800.HK", "2800.HK 盈富基金"))
    }
    summary["B"]["safe_asset"] = {"code": "sh511010", "etf": "511010 国债ETF"}
    plan = {"plans": {sid: {"market": market, "session": {"state_cn": "已闭盘", "plan_for": "next"},
                              "plan_date": "2026-08-31", "target": {label: 100}, "rationale": "测试"}
                       for sid, market, label in (("A", "美股", "SPY 标普500ETF"),
                                                  ("B", "A股", "510300 沪深300ETF"),
                                                  ("C", "港股", "2800.HK 盈富基金"))}}
    captured = {}
    monkeypatch.setattr(ai_advisor, "market_snapshot",
                        lambda source, assets, include_defaults=True: captured.update(
                            {"assets": assets, "include_defaults": include_defaults}) or [])
    monkeypatch.setattr(ai_advisor, "fetch_news", lambda **kwargs: [])

    ctx = ai_advisor.build_context(summary, plan, holdings={}, markets=["US"])

    assert set(ctx["strategy_user_pools"]) == {"A"}
    assert ctx["known_assets"] == {"A": ["SPY 标普500ETF"]}
    assert [x[0] for x in captured["assets"]] == ["SPY"]
    assert captured["include_defaults"] is False


@allure.feature("AI交易指令")
@allure.story("护栏")
def test_guardrails_reject_watch_and_assets_outside_user_pool():
    from backend import ai_advisor

    parsed = {"decisions": [
        {"strategy": "B", "asset": "510300 沪深300ETF", "target_pct": 100, "action": "buy"},
        {"strategy": "B", "asset": "518880 黄金ETF", "target_pct": 100, "action": "buy"},
        {"strategy": "WATCH", "asset": "NVDA", "target_pct": 20, "action": "buy"},
    ]}
    known = {"A": ["TQQQ"], "B": ["510300 沪深300ETF", "511010 国债ETF"], "C": ["7200.HK"]}

    decisions, warnings = ai_advisor.apply_guardrails(parsed, {"B": {}}, known)

    assert [(x["strategy"], x["asset"]) for x in decisions] == [("B", "510300 沪深300ETF")]
    assert any("未知资产" in x for x in warnings)
    assert any("未知策略" in x for x in warnings)
