"""核心 B 策略权重构造的回归测试。

背景（2026-08-28 修复）：
engine.build_strategy_b 原先只把新选中标的置为 1.0，却从不把旧标的清零，
导致历史持仓叠加成 3~4 倍名义杠杆（avg_exposure 318%~400%），
回测收益因此显著虚高且不可复现。以下用例锁定"单选全仓"语义。
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import allure


def _dynamic_frames() -> dict[str, pd.DataFrame]:
    idx = pd.date_range("2024-01-01", periods=320, freq="B")
    noise = 0.001 * np.sin(np.arange(len(idx)) / 4)
    fast = 100 * np.cumprod(1 + 0.002 + noise)
    slow = 100 * np.cumprod(1 + 0.0005 + noise)
    return {
        "TQQQ": pd.DataFrame({"close": fast}, index=idx),
        "SPY": pd.DataFrame({"close": slow}, index=idx),
    }


def _run_dynamic_with_spy(monkeypatch, sid="A", fail=None, frames=None, params=None):
    from backend import data_feed as dfd
    from backend import engine

    frames = frames or _dynamic_frames()

    def fake_get(code, interval="1d", period="5y"):
        if code == fail:
            raise RuntimeError("模拟行情失败")
        return frames[code]

    monkeypatch.setattr(dfd, "get_us", fake_get)
    captured = {}
    original = engine.run_multi_asset

    def spy(weights, prices, cost_rate=0.0015):
        captured["weights"] = weights.copy()
        return original(weights, prices, cost_rate=cost_rate)

    monkeypatch.setattr(engine, "run_multi_asset", spy)
    params = params or {
        "assets": [
            {"code": "TQQQ", "name": "核心"},
            {"code": "SPY", "name": "标普"},
        ],
        "trend_window": 60,
        "target_vol": 0.35,
    }
    result = (
        engine.build_strategy_a(params)
        if sid == "A"
        else engine.build_strategy_c(params)
    )
    return result, captured["weights"]


@allure.feature("动态波动率目标")
@allure.story("A/C动态候选池")
def test_dynamic_pool_selects_top_asset_and_returns_curves(monkeypatch):
    result, weights = _run_dynamic_with_spy(monkeypatch)

    assert (weights.sum(axis=1) <= 1.0 + 1e-9).all()
    assert weights["TQQQ"].iloc[-1] > 0 and weights["SPY"].iloc[-1] == 0
    assert result["current"]["pick"] == "TQQQ"
    assert {x["code"] for x in result["pool"]} == {"TQQQ", "SPY"}
    assert {x["name"] for x in result["windows"]["full"]["benchmark_curves"]} == {
        "TQQQ 核心", "SPY 标普"}


@allure.feature("动态波动率目标")
@allure.story("失败隔离与动态缓存")
def test_dynamic_pool_isolates_failure_and_replaces_raw_curves(monkeypatch):
    from backend import engine

    engine.RAW["A"] = {"benchmark_curves": {"OLD": pd.Series(dtype=float)}}
    result, _ = _run_dynamic_with_spy(monkeypatch, fail="TQQQ")

    assert result["failed_assets"][0]["code"] == "TQQQ"
    assert result["windows"]["full"]["bench_name"] == "SPY 买入持有"
    assert set(engine.RAW["A"]["benchmark_curves"]) == {"SPY 标普"}


@allure.feature("动态波动率目标")
@allure.story("港股动态候选池")
def test_c_dynamic_pool_uses_hk_assets_and_falls_back_benchmark(monkeypatch):
    idx = pd.date_range("2024-01-01", periods=320, freq="B")
    noise = 0.001 * np.sin(np.arange(len(idx)) / 4)
    frames = {
        "7200.HK": pd.DataFrame(
            {"close": 100 * np.cumprod(1 + 0.001 + noise)}, index=idx
        ),
        "2800.HK": pd.DataFrame(
            {"close": 100 * np.cumprod(1 + 0.002 + noise)}, index=idx
        ),
    }
    params = {
        "assets": [
            {"code": "7200.HK", "name": "核心", "class": "用户候选"},
            {"code": "2800.HK", "name": "盈富基金", "class": "用户候选"},
        ],
        "trend_window": 60,
        "target_vol": 0.40,
    }

    result, weights = _run_dynamic_with_spy(
        monkeypatch, sid="C", fail="7200.HK", frames=frames, params=params
    )

    assert result["current"]["pick"] == "2800.HK"
    assert result["current"]["etf"] == "2800.HK 盈富基金"
    assert result["windows"]["full"]["bench_name"] == "2800.HK 买入持有"
    assert {item["code"] for item in result["pool"]} == {"2800.HK"}
    assert {curve["name"] for curve in result["windows"]["full"]["benchmark_curves"]} == {
        "2800.HK 盈富基金"
    }
    assert weights["2800.HK"].iloc[-1] > 0


@allure.feature("动态波动率目标")
@allure.story("趋势闸门")
def test_dynamic_pool_moves_to_cash_when_all_assets_are_ineligible(monkeypatch):
    idx = pd.date_range("2024-01-01", periods=320, freq="B")
    noise = 0.001 * np.sin(np.arange(len(idx)) / 4)
    frames = {
        "TQQQ": pd.DataFrame(
            {"close": 100 * np.cumprod(1 - 0.002 + noise)}, index=idx
        ),
        "SPY": pd.DataFrame(
            {"close": 100 * np.cumprod(1 - 0.001 + noise)}, index=idx
        ),
    }

    result, weights = _run_dynamic_with_spy(monkeypatch, frames=frames)

    assert result["current"]["pick"] == "现金"
    assert result["current"]["etf"] == "现金"
    assert result["current"]["exposure"] == 0
    assert result["current"]["trend_gate_on"] is False
    assert float(weights.iloc[-1].sum()) == 0.0
    assert not any(item["above_ma"] for item in result["pool"])


@allure.feature("动态波动率目标")
@allure.story("仓位滞回")
def test_exposure_hysteresis_updates_at_exact_ten_point_boundary():
    from backend.engine import _apply_exposure_hysteresis

    assert _apply_exposure_hysteresis("TQQQ", 0.59, "TQQQ", 0.50, 0.10) == 0.50
    assert _apply_exposure_hysteresis("TQQQ", 0.60, "TQQQ", 0.50, 0.10) == 0.60
    assert _apply_exposure_hysteresis("SPY", 0.55, "TQQQ", 0.50, 0.10) == 0.55


@allure.feature("动态波动率目标")
@allure.story("仓位滞回")
def test_dynamic_pool_applies_ten_point_hysteresis(monkeypatch):
    idx = pd.date_range("2024-01-01", periods=360, freq="B")
    rng = np.random.default_rng(20260830)
    rets = np.concatenate([
        rng.normal(0.0020, 0.012, 180),
        rng.normal(0.0020, 0.014, 90),
        rng.normal(0.0020, 0.028, 90),
    ])
    frame = pd.DataFrame({"close": 100 * np.cumprod(1 + rets)}, index=idx)
    params = {
        "assets": [{"code": "TQQQ", "name": "核心"}],
        "trend_window": 40,
        "target_vol": 0.12,
    }

    _, weights = _run_dynamic_with_spy(
        monkeypatch, frames={"TQQQ": frame}, params=params
    )

    realized = frame["close"].pct_change().rolling(40).std() * np.sqrt(252)
    desired = (0.12 / realized).clip(0.0, 1.0)
    above_ma = frame["close"] > frame["close"].rolling(40).mean()
    held_small_change = False
    updated_large_change = False
    previous = 0.0
    for date in idx:
        actual = float(weights.at[date, "TQQQ"])
        if not above_ma.at[date] or not np.isfinite(desired.at[date]):
            previous = 0.0
            continue
        gap = abs(float(desired.at[date]) - previous)
        if 0 < gap < 0.10 - 1e-9:
            held_small_change = held_small_change or abs(actual - previous) < 1e-12
        if gap >= 0.10:
            updated_large_change = updated_large_change or abs(
                actual - float(desired.at[date])
            ) < 1e-12
        previous = actual

    assert held_small_change, "不足10个百分点的仓位变化应保持原仓位"
    assert updated_large_change, "达到10个百分点的仓位变化应更新目标仓位"


def _run_b_with_spy(monkeypatch, frames, params=None) -> tuple[dict, pd.DataFrame]:
    """运行 B 策略并捕获实际送入回测的权重矩阵。"""
    from backend import data_feed as dfd
    from backend import engine

    monkeypatch.setattr(dfd, "get_a", lambda code, start="20180101", end=None: frames[code])

    captured: dict[str, pd.DataFrame] = {}
    original = engine.run_multi_asset

    def spy(weights, prices, cost_rate=0.0015):
        captured["weights"] = weights.copy()
        return original(weights, prices, cost_rate=cost_rate)

    monkeypatch.setattr(engine, "run_multi_asset", spy)
    result = engine.build_strategy_b(params)
    return result, captured["weights"]


@allure.feature("核心策略B")
@allure.story("权重构造回归")
@allure.severity(allure.severity_level.CRITICAL)
@allure.title("B 策略任意时点持仓不得超过单一标的（100%）")
def test_b_weights_never_exceed_single_position(monkeypatch, rotation_frames):
    from backend import engine

    _, weights = _run_b_with_spy(monkeypatch, rotation_frames)
    row_max = float(weights.sum(axis=1).max())

    allure.attach(f"最大行和 = {row_max}", "持仓上限校验", allure.attachment_type.TEXT)
    assert row_max <= 1.0 + 1e-9, (
        f"检测到持仓叠加：某一时点权重合计 {row_max:.2f}，超出单选全仓的 100% 上限"
    )
    assert hasattr(engine, "build_strategy_b")


@allure.feature("核心策略B")
@allure.story("权重构造回归")
@allure.severity(allure.severity_level.CRITICAL)
@allure.title("B 策略切换后旧标的必须被清零")
def test_b_old_position_is_cleared_after_switch(monkeypatch, rotation_frames):
    _, weights = _run_b_with_spy(monkeypatch, rotation_frames)
    held_per_row = (weights > 0).sum(axis=1)

    allure.attach(held_per_row.value_counts().to_string(), "同时持仓数量分布",
                  allure.attachment_type.TEXT)
    assert int(held_per_row.max()) <= 1, "同一时点最多只能持有一个标的"


@allure.feature("核心策略B")
@allure.story("权重构造回归")
@allure.title("B 策略平均暴露度为 100%（非杠杆）")
def test_b_avg_exposure_is_one_hundred_percent(monkeypatch, rotation_frames):
    result, _ = _run_b_with_spy(monkeypatch, rotation_frames)
    exposure = result["windows"]["full"]["strategy"]["avg_exposure"]

    allure.attach(f"avg_exposure = {exposure}%", "平均暴露度", allure.attachment_type.TEXT)
    assert abs(exposure - 100.0) < 1e-6, (
        f"修复前该指标为 318%~400%（杠杆叠加），现在应为 100%，实际 {exposure}%"
    )


@allure.feature("核心策略B")
@allure.story("权重构造回归")
@allure.title("回归用例确实覆盖了换仓路径")
def test_b_regression_covers_at_least_one_switch(monkeypatch, rotation_frames):
    """确保合成数据真的发生了换仓，否则上面的清零校验形同虚设。"""
    result, weights = _run_b_with_spy(monkeypatch, rotation_frames)
    switched = int((weights.diff().abs().sum(axis=1) > 0).sum())

    allure.attach(f"换仓次数 = {switched}", "换仓统计", allure.attachment_type.TEXT)
    assert switched >= 2, f"合成数据应触发至少 2 次换仓，实际 {switched}"
    assert result["windows"]["full"]["strategy"]["total_return"] is not None


@allure.feature("核心策略B")
@allure.story("基准口径")
@allure.title("B 策略基准为沪深300 而非首列资产")
def test_b_benchmark_is_hs300(monkeypatch, rotation_frames):
    result, _ = _run_b_with_spy(monkeypatch, rotation_frames)
    bench = result["windows"]["full"]["benchmark"]

    allure.attach(str(bench), "基准指标", allure.attachment_type.TEXT)
    assert bench["total_return"] is not None
    assert result["windows"]["full"]["bench_name"] == "沪深300 买入持有"
    assert "沪深300" in str(result.get("assets", {}).values())


@allure.feature("核心策略B")
@allure.story("图表悬浮持仓")
@allure.title("B 策略曲线逐日显示选中 ETF 且与曲线长度对齐")
def test_b_chart_positions_are_aligned_and_named(monkeypatch, rotation_frames):
    result, _ = _run_b_with_spy(monkeypatch, rotation_frames)
    payload = result["windows"]["full"]

    assert len(payload["dates"]) == len(payload["equity"]) == len(payload["bench_equity"])
    assert len(payload["dates"]) == len(payload["drawdown"]) == len(payload["positions"])
    assets = {item["asset"] for pos in payload["positions"] for item in pos["items"]}
    allowed = {item["etf"] for item in result["pool"]} | {result["safe_asset"]["etf"]}
    assert assets <= allowed | {"现金"}
    assert assets & allowed, "Tooltip 至少应包含一个带代码和名称的池内 ETF"
    assert all(sum(item["weight"] for item in pos["items"]) == 100
               for pos in payload["positions"])


@allure.feature("核心策略B")
@allure.story("多基准曲线")
@allure.title("B 策略返回用户风险池的独立买入持有曲线")
def test_b_has_dynamic_switchable_etf_benchmark_curves(monkeypatch, rotation_frames):
    result, _ = _run_b_with_spy(monkeypatch, rotation_frames)
    payload = result["windows"]["full"]
    curves = payload["benchmark_curves"]

    assert {curve["name"] for curve in curves} == {item["etf"] for item in result["pool"]}
    assert len(curves) == len(result["pool"]) == 4
    assert all(len(curve["values"]) == len(payload["dates"]) for curve in curves)
    assert all(curve["values"][0] == 1.0 for curve in curves), "每条基准必须按窗口起点归一为1"


@allure.feature("核心策略B")
@allure.story("多基准曲线")
@allure.title("B 策略自定义窗口同步重置动态风险池基准起点")
def test_b_custom_window_keeps_dynamic_etf_benchmarks(monkeypatch, rotation_frames):
    from backend import engine

    _run_b_with_spy(monkeypatch, rotation_frames)
    try:
        start = str(engine.RAW["B"]["index"][220].date())
        payload = engine.custom_window("B", start)
    finally:
        engine.RAW.pop("B", None)

    assert len(payload["benchmark_curves"]) == 4
    assert all(len(curve["values"]) == len(payload["dates"])
               for curve in payload["benchmark_curves"])
    assert all(curve["values"][0] == 1.0 for curve in payload["benchmark_curves"])
