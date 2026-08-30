"""扩展策略层测试：注册表完整性、参数校验、新策略逻辑与故障隔离。"""
from __future__ import annotations

import pandas as pd
import allure

from conftest import phased_frame, synth_frame
from backend import ext_strategy as ex


# --------------------------------------------------------------------------- 注册表
@allure.feature("扩展策略")
@allure.story("注册表完整性")
def test_every_strategy_has_a_valid_group_and_defaults():
    for sid, meta in ex.EXT_STRATEGIES.items():
        with allure.step(f"校验 {sid}"):
            assert meta["group"] in ex.EXT_GROUPS, f"{sid} 所属组不存在"
            assert meta["default_params"], f"{sid} 缺少默认参数"
            assert meta["description"] and meta["trigger"], f"{sid} 缺少规则/触发说明"
            kind = meta["kind"]
            if kind == "rotation":
                assert meta.get("candidates") and meta.get("safe"), f"{sid} 缺少候选池或防御资产"
            else:
                assert meta.get("sig") and meta.get("veh"), f"{sid} 缺少信号源/执行标的"


@allure.feature("扩展策略")
@allure.story("注册表完整性")
def test_default_params_always_pass_validation():
    for sid, meta in ex.EXT_STRATEGIES.items():
        validated = ex.validate_ext_params(sid, dict(meta["default_params"]))
        assert validated == meta["default_params"], f"{sid} 默认参数未通过自身校验"


@allure.feature("扩展策略")
@allure.story("注册表完整性")
def test_group_assets_cover_all_candidate_symbols():
    """候选池与防御资产必须在所属组的资产清单里登记，便于前端展示。"""
    for sid, meta in ex.EXT_STRATEGIES.items():
        if meta["kind"] != "rotation":
            continue
        group_assets = ex.EXT_GROUPS[meta["group"]]["assets"]
        for code in list(meta["candidates"]) + [meta["safe"]]:
            assert code in group_assets, f"{sid} 的标的 {code} 未登记在组资产清单"


# --------------------------------------------------------------------------- 参数校验
@allure.feature("扩展策略")
@allure.story("参数校验")
def test_validate_rejects_out_of_range_and_bad_types():
    cases = [
        ("us_qqq_vt", {"target_vol": 9}, "目标波动越界"),
        ("us_qqq_vt", {"vol_window": 20.5}, "窗口非整数"),
        ("us_qqq_vt", {"trend_window": 10}, "趋势窗口越界"),
        ("us_spy_dma", {"fast": 300, "slow": 200}, "快线越界"),
        ("us_spy_dma", {"fast": 200, "slow": 100}, "快线不小于慢线"),
        ("a_industry_rot", {"weekly": "yes"}, "布尔字段类型错误"),
        ("a_industry_rot", {"bogus": 1}, "未知参数"),
    ]
    for sid, raw, label in cases:
        with allure.step(f"{label}：{sid} {raw}"):
            try:
                ex.validate_ext_params(sid, raw)
            except ValueError as e:
                allure.attach(str(e), "拒绝原因", allure.attachment_type.TEXT)
                continue
            raise AssertionError(f"{label} 应被拒绝却通过了校验")


@allure.feature("扩展策略")
@allure.story("参数校验")
def test_rotation_target_vol_allows_zero_to_disable_scaling():
    """轮动的波动缩放可关闭（0），而波动目标类策略不接受 0。"""
    assert ex.validate_ext_params("a_industry_rot", {"target_vol": 0})["target_vol"] == 0
    try:
        ex.validate_ext_params("us_qqq_vt", {"target_vol": 0})
    except ValueError:
        return
    raise AssertionError("波动目标类策略的目标波动不应允许为 0")


@allure.feature("扩展策略")
@allure.story("参数校验")
def test_unknown_strategy_is_rejected():
    try:
        ex.validate_ext_params("not_a_strategy", {})
    except ValueError:
        return
    raise AssertionError("未知策略应被拒绝")


# --------------------------------------------------------------------------- 新策略逻辑
@allure.feature("扩展策略")
@allure.story("双均线择时")
def test_dual_ma_timing_follows_golden_cross():
    up = synth_frame("2024-01-01", 200, daily=0.004)
    down = synth_frame("2024-10-01", 60, daily=-0.006)
    down.index = pd.date_range(up.index[-1], periods=60, freq="B")
    close = pd.concat([up["close"].iloc[:-1], down["close"]])

    w = ex.dual_ma_timing(close, fast=20, slow=60)

    assert float(w.iloc[100]) == 1.0, "上涨段快线在上方应持有"
    assert float(w.iloc[-1]) == 0.0, "转跌后死叉应空仓"
    assert set(w.unique()) <= {0.0, 1.0}, "双均线择时只输出 0 或 1"


@allure.feature("扩展策略")
@allure.story("双均线择时")
def test_dual_ma_timing_stays_flat_during_cold_start():
    close = synth_frame("2024-01-01", 30, daily=0.001)["close"]
    w = ex.dual_ma_timing(close, fast=20, slow=60)

    assert float(w.iloc[-1]) == 0.0, "数据不足以计算慢线时必须空仓，避免冷启动误信号"


@allure.feature("扩展策略")
@allure.story("动量轮动")
def test_rotation_generic_falls_back_to_safe_asset():
    frames = {
        "a": synth_frame("2022-06-01", 300, daily=-0.004),
        "b": synth_frame("2022-06-01", 300, daily=-0.005),
        "safe": synth_frame("2022-06-01", 300, daily=0.0001),
    }
    decisions = ex.rotation_generic(frames, candidates=["a", "b"], safe_code="safe",
                                    mom_window=60, vol_window=40, ma_window=40,
                                    weekly=True, buffer_pct=0.05)

    assert set(decisions["pick"]) == {"safe"}, f"全线下跌应只持防御资产，实际 {set(decisions['pick'])}"


@allure.feature("扩展策略")
@allure.story("动量轮动")
def test_rotation_generic_vol_target_scaling_reduces_exposure():
    """开启波动缩放后平均暴露应低于满仓，关闭时等于满仓。"""
    frames = {
        "a": synth_frame("2022-06-01", 600, daily=0.0, seed=11, vol=0.018),
        "b": synth_frame("2022-06-01", 600, daily=0.0, seed=12, vol=0.018),
        "safe": synth_frame("2022-06-01", 600, daily=0.0001),
    }
    closes = pd.DataFrame({c: f["close"] for c, f in frames.items()}).sort_index()
    base = dict(mom_window=60, vol_window=40, ma_window=40, weekly=True, buffer_pct=0.05)

    def exposure_with(target_vol: float) -> float:
        decisions = ex.rotation_generic(frames, candidates=["a", "b"], safe_code="safe",
                                        **base)
        w = pd.DataFrame(0.0, index=closes.index, columns=closes.columns)
        for t, row in decisions.iterrows():
            pos = closes.index.searchsorted(t)
            idx = closes.index[min(pos + 1, len(closes) - 1)]
            w.loc[idx:, :] = 0.0
            w.loc[idx:, row["pick"]] = 1.0
        if target_vol:
            rv = closes.pct_change().rolling(base["vol_window"]).std() * (252 ** 0.5)
            scale = (target_vol / rv.replace(0, float("nan"))).clip(upper=1.0).fillna(0.0)
            w = w.mul((scale / 0.05).round() * 0.05, axis=0)
        # 首个决策日之前权重为 0（冷启动），统计时从首次建仓起算
        row_sum = w.sum(axis=1)
        first = row_sum[row_sum > 0].index[0]
        return float(row_sum.loc[first:].mean())

    full = exposure_with(0)
    scaled = exposure_with(0.20)

    allure.attach(f"满仓 {full:.3f} / 缩放后 {scaled:.3f}", "暴露度对比",
                  allure.attachment_type.TEXT)
    assert abs(full - 1.0) < 1e-6, "关闭缩放时应为满仓单只"
    assert scaled < full, "开启波动缩放后暴露度应下降"


# --------------------------------------------------------------------------- 故障隔离
@allure.feature("扩展策略")
@allure.story("故障隔离")
def test_build_one_marks_error_without_raising(monkeypatch):
    """单一策略的数据源异常必须被捕获并标记，不能抛出中断整体构建。"""
    from backend import data_feed as dfd

    def boom(*args, **kwargs):
        raise RuntimeError("模拟数据源不可用")

    monkeypatch.setattr(dfd, "get_us", boom)
    result = ex.build_one("us_qqq_vt", ex.EXT_STRATEGIES["us_qqq_vt"]["default_params"], True)

    allure.attach(str(result.get("error")), "错误信息", allure.attachment_type.TEXT)
    assert result["status"] == "error"
    assert result["current"] is None and result["windows"] == {}
    assert "模拟数据源不可用" in result["error"]


@allure.feature("扩展策略")
@allure.story("故障隔离")
def test_disabled_strategy_is_not_computed(monkeypatch):
    from backend import data_feed as dfd

    def boom(*args, **kwargs):
        raise RuntimeError("停用的策略不应触发任何取数")

    monkeypatch.setattr(dfd, "get_us", boom)
    result = ex.build_one("us_qqq_vt", ex.EXT_STRATEGIES["us_qqq_vt"]["default_params"], False)

    assert result["status"] == "disabled", "停用策略应直接跳过计算"


@allure.feature("扩展策略")
@allure.story("故障隔离")
def test_build_all_ext_keeps_other_strategies_healthy(monkeypatch):
    """注册表里每一条策略在故障注入下都应独立降级，且返回值结构完整。"""
    from backend import data_feed as dfd

    monkeypatch.setattr(dfd, "get_us", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("断网")))
    monkeypatch.setattr(dfd, "get_a", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("断网")))

    data = ex.build_all_ext({})
    statuses = {sid: p["status"] for sid, p in data["strategies"].items()}

    allure.attach(str(statuses), "各策略状态", allure.attachment_type.TEXT)
    assert len(statuses) == len(ex.EXT_STRATEGIES), "所有策略都应返回结果条目"
    assert set(statuses.values()) == {"error"}, "全部数据源失效时应整体降级为 error"
    assert all("name" in p and "params" in p for p in data["strategies"].values()), \
        "故障状态下仍需保留元信息供前端渲染"
    assert data["settings"]["strategies"], "故障不影响配置回读"
