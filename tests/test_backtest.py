"""回测引擎测试：次日执行、换手成本、指标口径与窗口切片。"""
from __future__ import annotations

import numpy as np
import pandas as pd
import allure


@allure.feature("回测引擎")
@allure.story("次日执行")
def test_weight_applies_from_next_bar():
    """当日收盘产生的权重只影响次日收益，保证无未来函数。"""
    from backend.backtest import run_weight_backtest

    idx = pd.date_range("2024-01-01", periods=3, freq="B")
    prices = pd.Series([100.0, 110.0, 121.0], index=idx)      # 每日 +10%
    weights = pd.Series([0.0, 1.0, 1.0], index=idx)           # 第2天才建仓
    bt = run_weight_backtest(weights, prices, cost_rate=0.0)

    first_day_ret = float(bt["ret"].iloc[1])
    assert first_day_ret == 0.0, "建仓当日不应产生收益（权重次日生效）"
    assert abs(float(bt["ret"].iloc[2]) - 0.10) < 1e-9, "次日应完整吃到 +10% 涨幅"

    with allure.step("核对净值曲线"):
        allure.attach(bt.to_csv(), "回测明细", allure.attachment_type.CSV)
        assert abs(float(bt["equity"].iloc[-1]) - 1.10) < 1e-9


@allure.feature("回测引擎")
@allure.story("换手成本")
def test_turnover_is_charged_on_the_bar_after_trade():
    """换手成本按成交名义金额的 15bp 计，且在成交后的那根K线扣除。"""
    from backend.backtest import run_weight_backtest

    idx = pd.date_range("2024-01-01", periods=3, freq="B")
    prices = pd.Series([100.0, 100.0, 100.0], index=idx)      # 价格不变
    weights = pd.Series([0.0, 1.0, 1.0], index=idx)           # 0 -> 100% 换手
    bt = run_weight_backtest(weights, prices, cost_rate=0.0015)

    assert abs(float(bt["ret"].iloc[1]) + 0.0015) < 1e-9, "建仓次日应扣除 15bp 成本"
    assert abs(float(bt["ret"].iloc[2])) < 1e-12, "未再换手则不再扣费"


@allure.feature("回测引擎")
@allure.story("多资产组合")
def test_multi_asset_sums_weighted_returns():
    """多资产按各自权重加权求和，权重行和不应超过 1（单选全仓语义）。"""
    from backend.backtest import run_multi_asset

    idx = pd.date_range("2024-01-01", periods=3, freq="B")
    prices = pd.DataFrame({"a": [10.0, 11.0, 11.0], "b": [10.0, 10.0, 12.0]}, index=idx)
    # 权重同样次日生效：第0日选 a -> 第1日吃到 a 的 +10%；第1日换 b -> 第2日吃到 b 的 +20%
    weights = pd.DataFrame({"a": [1.0, 0.0, 0.0], "b": [0.0, 1.0, 1.0]}, index=idx)
    bt = run_multi_asset(weights, prices, cost_rate=0.0)

    assert abs(float(bt["ret"].iloc[1]) - 0.10) < 1e-9, "次日应只有 a 贡献 +10%"
    assert abs(float(bt["ret"].iloc[2]) - 0.20) < 1e-9, "第三日应只有 b 贡献 +20%"


@allure.feature("回测引擎")
@allure.story("绩效指标")
def test_metrics_reports_standard_fields():
    """指标口径：总收益、年化、波动、夏普、最大回撤、Calmar、平均暴露。"""
    from backend.backtest import metrics, run_weight_backtest

    idx = pd.date_range("2024-01-01", periods=253, freq="B")
    rets = np.full(253, 0.0004)
    prices = pd.Series(100 * np.cumprod(1 + rets), index=idx)
    w = pd.Series(1.0, index=idx)
    bt = run_weight_backtest(w, prices, cost_rate=0.0)
    m = metrics(bt["equity"], bt["ret"], bt["weight"])

    with allure.step("核对指标"):
        allure.attach(str(m), "metrics", allure.attachment_type.TEXT)
    assert m["total_return"] > 0
    assert m["max_dd"] == 0.0, "单调上涨不应出现回撤"
    assert abs(m["avg_exposure"] - 100.0) < 1e-6, "满仓时平均暴露应为 100%"
    assert m["sharpe"] is not None and m["sharpe"] > 0


@allure.feature("回测引擎")
@allure.story("绩效指标")
def test_max_drawdown_matches_manual_calculation():
    """最大回撤口径 = 净值相对历史峰值的最低点。"""
    from backend.backtest import metrics

    idx = pd.date_range("2024-01-01", periods=4, freq="B")
    equity = pd.Series([1.0, 1.2, 0.6, 0.9], index=idx)   # 峰值1.2 -> 谷底0.6 = -50%
    m = metrics(equity, equity.pct_change().fillna(0))

    assert abs(m["max_dd"] + 50.0) < 1e-6, f"最大回撤应为 -50%，实际 {m['max_dd']}"


@allure.feature("回测引擎")
@allure.story("窗口切片")
def test_slice_window_since_jun_falls_back_for_short_history():
    """since_jun 窗口在样本不足时回退到最近 60 根，避免空序列。"""
    from backend.backtest import slice_window

    idx = pd.date_range("2024-01-01", periods=100, freq="B")
    df = pd.DataFrame({"equity": np.linspace(1, 2, 100)}, index=idx)
    seg = slice_window(df, "since_jun")

    assert len(seg) == 60, f"短历史应回退到 60 根，实际 {len(seg)}"


@allure.feature("回测引擎")
@allure.story("窗口切片")
def test_slice_window_3y_and_1y_bounds():
    """3年/1年窗口按末日向前推，且不会越界。"""
    from backend.backtest import slice_window

    idx = pd.date_range("2020-01-01", periods=1500, freq="B")
    df = pd.DataFrame({"equity": np.linspace(1, 3, 1500)}, index=idx)
    y1 = slice_window(df, "1y")
    y3 = slice_window(df, "3y")

    assert len(y1) < len(y3) < len(df)
    assert y1.index[-1] == df.index[-1]


@allure.feature("回测引擎")
@allure.story("归一化")
def test_renorm_rescales_both_curves_to_one():
    from backend.backtest import renorm

    idx = pd.date_range("2024-01-01", periods=3, freq="B")
    df = pd.DataFrame({"equity": [2.0, 3.0, 4.0], "bench_equity": [5.0, 6.0, 7.0]}, index=idx)
    out = renorm(df)

    assert out["equity"].iloc[0] == 1.0
    assert out["bench_equity"].iloc[0] == 1.0
