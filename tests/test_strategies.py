"""核心策略逻辑测试：波动率目标闸门与止损、周度轮动的选基与防御。"""
from __future__ import annotations

import numpy as np
import pandas as pd
import allure

from conftest import phased_frame, synth_frame


def _closes(frames: dict[str, pd.DataFrame]) -> pd.DataFrame:
    return pd.DataFrame({c: f["close"] for c, f in frames.items()}).sort_index()


@allure.feature("核心策略A/C")
@allure.story("趋势闸门")
def test_vol_target_gate_closes_position_in_downtrend():
    """标的价格长期低于趋势均线时，闸门关闭、目标仓位应为 0。"""
    from backend.strategies import vol_target_tqqq

    sig = synth_frame("2022-01-03", 500, daily=-0.002)      # 持续下跌
    veh = sig.copy()
    w = vol_target_tqqq(sig, veh["close"], vol_window=20, target_vol=0.35,
                        trend_window=200, min_change=0.0)

    assert float(w.iloc[-1]) == 0.0, f"下跌趋势中仓位应为 0，实际 {w.iloc[-1]}"
    allure.attach(str(w.tail(5)), "尾部权重", allure.attachment_type.TEXT)


@allure.feature("核心策略A/C")
@allure.story("波动率目标")
def test_vol_target_higher_volatility_means_lower_weight():
    """同等趋势下，实现波动越高目标仓位越低（波动率溢价的核心机制）。"""
    from backend.strategies import vol_target_tqqq

    calm = synth_frame("2022-01-03", 500, daily=0.001, seed=1, vol=0.004)
    wild = synth_frame("2022-01-03", 500, daily=0.001, seed=2, vol=0.030)
    w_calm = vol_target_tqqq(calm, calm["close"], vol_window=20, target_vol=0.35,
                             trend_window=200, min_change=0.0)
    w_wild = vol_target_tqqq(wild, wild["close"], vol_window=20, target_vol=0.35,
                             trend_window=200, min_change=0.0)

    assert float(w_calm.iloc[-1]) > float(w_wild.iloc[-1]), "低波动应对应更高仓位"
    assert float(w_calm.iloc[-1]) <= 1.0 and float(w_wild.iloc[-1]) <= 1.0, "仓位上限为 100%"


@allure.feature("核心策略A/C")
@allure.story("回撤止损")
def test_vol_target_stop_drawdown_zeroes_position():
    """触发跟踪回撤止损后仓位归零，直到重新站上复活均线。"""
    from backend.strategies import vol_target_tqqq

    up = synth_frame("2022-01-03", 300, daily=0.003)
    down = synth_frame("2022-03-01", 120, daily=-0.004)
    down.index = pd.date_range(up.index[-1], periods=120, freq="B")
    sig = pd.concat([up.iloc[:-1], down])
    w = vol_target_tqqq(sig, sig["close"], vol_window=20, target_vol=0.35,
                        trend_window=200, min_change=0.0, stop_dd=0.10)

    assert float(w.iloc[-1]) == 0.0, "深跌后应被止损清零"


@allure.feature("核心策略B")
@allure.story("动量选基")
def test_rotation_picks_the_leading_risky_asset(rotation_frames):
    """动量最强且在均线上方的风险资产应被选中，而非防御资产。"""
    from backend.strategies import cycle_rotation_a, RISKY_B, SAFE_B

    decisions = cycle_rotation_a(rotation_frames, mom_window=120, vol_window=60,
                                 ma_window=60, weekly=True, buffer_pct=0.0)
    picks = set(decisions["pick"])

    assert picks & set(RISKY_B), f"上行段应持有风险资产，实际 {picks}"
    allure.attach(str(list(picks)), "被选中的标的", allure.attachment_type.TEXT)
    assert SAFE_B in decisions["pick"].values or len(picks) >= 2, "应至少出现过一次换仓"


@allure.feature("核心策略B")
@allure.story("防御切换")
def test_rotation_falls_back_to_safe_when_all_risky_below_ma(rotation_frames):
    """所有风险资产都跌破均线时，应整体转入国债防御。"""
    from backend.strategies import cycle_rotation_a, SAFE_B

    from backend.strategies import RISKY_B

    falling = {code: synth_frame("2022-06-01", 400, daily=-0.004) for code in RISKY_B}
    falling[SAFE_B] = synth_frame("2022-06-01", 400, daily=0.0001)
    decisions = cycle_rotation_a(falling, mom_window=120, vol_window=60,
                                 ma_window=60, weekly=True, buffer_pct=0.0)

    assert set(decisions["pick"]) == {SAFE_B}, f"全线下跌应只持有防御资产，实际 {set(decisions['pick'])}"


@allure.feature("核心策略B")
@allure.story("换仓缓冲")
def test_rotation_buffer_reduces_turnover(rotation_frames):
    """换仓缓冲应减少无意义的换手：缓冲越大，切换次数不增。

    注意：缓冲只在"在位标的仍然合格"时生效——若在位标的跌破均线被剔除候选，
    则无论缓冲多大都必须换仓（这是风控优先于摩擦成本的设计）。
    """
    from backend.strategies import cycle_rotation_a

    def switches(buffer_pct: float) -> int:
        d = cycle_rotation_a(rotation_frames, mom_window=120, vol_window=60,
                             ma_window=60, weekly=True, buffer_pct=buffer_pct)
        return int((d["pick"] != d["pick"].shift(1)).sum() - 1)

    free, buffered = switches(0.0), switches(0.5)

    allure.attach(f"无缓冲 {free} 次 / 缓冲50% {buffered} 次", "换手次数",
                  allure.attachment_type.TEXT)
    assert buffered <= free, f"缓冲不应增加换手：{buffered} > {free}"


@allure.feature("核心策略B")
@allure.story("决策频率")
def test_rotation_decides_weekly_not_daily(rotation_frames):
    """周频模式下决策次数应接近周数，而非交易日数。"""
    from backend.strategies import cycle_rotation_a

    weekly = cycle_rotation_a(rotation_frames, mom_window=120, vol_window=60,
                              ma_window=60, weekly=True)
    daily = cycle_rotation_a(rotation_frames, mom_window=120, vol_window=60,
                             ma_window=60, weekly=False)
    closes = _closes(rotation_frames)
    weeks = closes.index.to_period("W").nunique()

    assert abs(len(weekly) - weeks) <= 2, f"周频决策数应接近 {weeks}，实际 {len(weekly)}"
    assert len(daily) > len(weekly) * 3, "日频决策数应显著多于周频"
