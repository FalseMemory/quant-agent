"""共享测试夹具。

设计原则：
- 全部测试离线运行，不依赖网络与真实行情缓存；
- 行情数据由确定性合成序列生成，保证结果可重复；
- 涉及文件写入的用例一律重定向到临时目录，绝不污染用户真实配置。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def synth_frame(start: str, n: int, *, daily: float = 0.0, seed: int | None = None,
                vol: float = 0.0, price0: float = 100.0) -> pd.DataFrame:
    """生成确定性日线。daily=日均涨幅；vol>0 时叠加可复现的随机波动。"""
    idx = pd.date_range(start, periods=n, freq="B")
    if vol > 0:
        rng = np.random.default_rng(seed or 42)
        rets = rng.normal(daily, vol, n)
    else:
        rets = np.full(n, daily)
    close = price0 * np.cumprod(1.0 + rets)
    return pd.DataFrame(
        {"open": close, "high": close * 1.01, "low": close * 0.99,
         "close": close, "volume": np.full(n, 1_000.0)},
        index=idx,
    )


def phased_frame(start: str, n: int, *, first: float, second: float,
                 price0: float = 100.0) -> pd.DataFrame:
    """前一半按 first 日涨、后一半按 second 日涨，用于制造明确的动量切换。"""
    idx = pd.date_range(start, periods=n, freq="B")
    half = n // 2
    rets = np.concatenate([np.full(half, first), np.full(n - half, second)])
    close = price0 * np.cumprod(1.0 + rets)
    return pd.DataFrame(
        {"open": close, "high": close * 1.01, "low": close * 0.99,
         "close": close, "volume": np.full(n, 1_000.0)},
        index=idx,
    )


@pytest.fixture
def rotation_frames() -> dict[str, pd.DataFrame]:
    """B 策略标的池（2026-08-29 起：ETF 池 + 黄金）。

    两只风险资产先后领涨，保证换仓路径被覆盖；sh000300 为基准指数，
    engine 会单独取它作为业绩基准。
    """
    return {
        "sz159915": phased_frame("2022-06-01", 600, first=0.004, second=-0.002),
        "sh510300": phased_frame("2022-06-01", 600, first=-0.002, second=0.005),
        "sh510880": phased_frame("2022-06-01", 600, first=0.0002, second=0.0002),
        "sh518880": phased_frame("2022-06-01", 600, first=0.006, second=0.001),
        "sh511010": synth_frame("2022-06-01", 600, daily=0.0001),
        "sh000300": phased_frame("2022-06-01", 600, first=-0.001, second=0.003),
    }


@pytest.fixture
def tmp_settings_file(tmp_path, monkeypatch):
    """把策略配置文件重定向到临时目录，避免测试污染用户真实配置。"""
    from backend import settings_store

    target = tmp_path / "strategy_settings.json"
    monkeypatch.setattr(settings_store, "SETTINGS_FILE", target)
    return target


@pytest.fixture
def client(tmp_settings_file, monkeypatch):
    """构造测试客户端，并把三策略与扩展策略的构建替换为确定性桩数据。

    供 test_app_api 与 test_plan_custom 共用。
    """
    from fastapi.testclient import TestClient

    from app import app
    import app as app_mod
    from backend import engine, ext_strategy, settings_store

    def fake_build_all(pa=None, pb=None, pc=None):
        def stub(sid, name, market, exposure, ret_total, dd):
            return {
                "id": sid, "name": name, "market": market, "freq_label": "测试",
                "windows": {w: {
                    "window": w, "label": w, "start": "2024-01-01", "end": "2024-12-31",
                    "strategy": {"total_return": ret_total, "cagr": 5.0, "ann_vol": 20.0,
                                 "sharpe": 0.5, "max_dd": dd, "calmar": 0.2,
                                 "avg_exposure": exposure},
                    "benchmark": {"total_return": 3.0, "cagr": 3.0, "ann_vol": 15.0,
                                  "sharpe": 0.3, "max_dd": -8.0, "calmar": 0.4},
                    "beats_benchmark": ret_total > 3.0,
                    "dates": ["2024-01-01", "2024-12-31"], "equity": [1.0, 1.1],
                    "bench_equity": [1.0, 1.03], "drawdown": [0.0, -5.0],
                } for w in ("full", "3y", "1y", "since_jun")},
                "current": {"exposure": exposure, "as_of": "2024-12-31"},
            }

        result = {
            "A": stub("A", "策略A", "美股", 70.0, 10.0, -20.0),
            "B": {
                **stub("B", "策略B", "A股", 100.0, 8.0, -25.0),
                "pool": [{"code": "sz159915", "name": "创业板ETF", "class": "周期成长",
                          "etf": "159915 创业板ETF", "above_ma": True,
                          "momentum_score": 1.25, "as_of": "2024-12-31"}],
                "safe_asset": {"code": "sh511010", "name": "国债ETF", "class": "系统防御",
                               "etf": "511010 国债ETF"},
                "failed_assets": [], "pool_limit": 10,
                "recent_picks": [],
            },
            "C": stub("C", "策略C", "港股", 50.0, 6.0, -30.0),
        }
        result["B"]["current"].update({
            "pick": "sz159915", "pick_name": "创业板ETF", "etf": "159915 创业板ETF",
            "reason": "测试推荐", "decision_date": "2024-12-27", "ma_window": 60,
            "mom_window": 180,
        })
        return result

    monkeypatch.setattr(engine, "build_all", fake_build_all)
    monkeypatch.setattr(app_mod, "build_all", fake_build_all)

    def fake_build_one(sid, params, enabled=True):
        base = {k: v for k, v in ext_strategy.EXT_STRATEGIES[sid].items()
                if k in ("group", "name", "kind", "kind_label", "freq_label",
                         "description", "trigger")}
        base.update({"id": sid, "group_name": ext_strategy.EXT_GROUPS[
            ext_strategy.EXT_STRATEGIES[sid]["group"]]["name"], "enabled": enabled,
            "params": dict(params), "status": "ok" if enabled else "disabled"})
        base["windows"] = {"full": {
            "window": "full", "label": "全历史", "start": "2024-01-01", "end": "2024-12-31",
            "strategy": {"total_return": 12.0, "cagr": 6.0, "ann_vol": 18.0, "sharpe": 0.7,
                         "max_dd": -15.0, "calmar": 0.4, "avg_exposure": 100.0},
            "benchmark": {"total_return": 9.0, "cagr": 4.5, "ann_vol": 16.0, "sharpe": 0.4,
                          "max_dd": -20.0, "calmar": 0.2},
            "beats_benchmark": True, "dates": ["2024-01-01", "2024-12-31"],
            "equity": [1.0, 1.12], "bench_equity": [1.0, 1.09], "drawdown": [0.0, -5.0]}}
        base["current"] = {"as_of": "2024-12-31"}
        return base

    monkeypatch.setattr(ext_strategy, "build_one", fake_build_one)

    app_mod._STATE["data"] = None
    app_mod._STATE["params_a"] = {"target_vol": 0.35, "trend_window": 200}
    app_mod._STATE["params_b"] = settings_store.validate_params(
        "params_b", {"mom_window": 180, "ma_window": 60})
    app_mod._STATE["params_c"] = {"target_vol": 0.40, "trend_window": 200}
    app_mod._STATE["error"] = None
    app_mod._EXT_STATE["data"] = None
    return TestClient(app)
