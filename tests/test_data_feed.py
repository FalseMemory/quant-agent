"""数据层测试：缓存读写、周期换算与多源兜底（全部离线，不访问真实网络）。"""
from __future__ import annotations

import time

import pandas as pd
import allure

from conftest import synth_frame


@allure.feature("数据层")
@allure.story("周期换算")
def test_period_cutoff_maps_units():
    from backend.data_feed import _period_cutoff

    today = pd.Timestamp.now().normalize()
    # 支持的单位：y(年) / m(月,按30.4天) / w(周) / d(日)
    cases = {"1y": 365, "2y": 731, "6m": 182, "4w": 28, "30d": 30}
    for period, approx_days in cases.items():
        delta = (today - _period_cutoff(period)).days
        with allure.step(f"{period} -> {delta} 天"):
            assert abs(delta - approx_days) <= 2, f"{period} 换算偏差过大：{delta}"


@allure.feature("数据层")
@allure.story("缓存")
def test_cache_roundtrip_and_ttl(tmp_path, monkeypatch):
    from backend import data_feed as dfd

    monkeypatch.setattr(dfd, "CACHE_DIR", tmp_path)
    frame = synth_frame("2024-01-01", 50, daily=0.001)
    dfd._save_cache("probe_key", frame)

    assert dfd._load_cache("probe_key") is not None, "新鲜缓存应命中"
    loaded = dfd._load_cache("probe_key")
    assert len(loaded) == 50

    # 人为把文件时间推到 6 小时之前
    path = tmp_path / "probe_key.csv"
    old = time.time() - 7 * 3600
    import os
    os.utime(path, (old, old))

    assert dfd._load_cache("probe_key") is None, "超过 TTL 不应作为新鲜缓存返回"
    assert dfd._load_stale_cache("probe_key") is not None, "过期缓存仍应可作为兜底读取"


@allure.feature("数据层")
@allure.story("多源兜底")
def test_get_us_falls_back_to_secondary_source(tmp_path, monkeypatch):
    """主源异常时应自动切换到备用源，且结果写入缓存。"""
    from backend import data_feed as dfd

    monkeypatch.setattr(dfd, "CACHE_DIR", tmp_path)
    calls = []

    def primary(symbol, period):
        calls.append("primary")
        raise RuntimeError("主源 403")

    def secondary(symbol, period):
        calls.append("secondary")
        return synth_frame("2024-01-01", 30, daily=0.001)

    monkeypatch.setattr(dfd, "_us_sources", lambda symbol: [primary, secondary])
    df = dfd.get_us("FAKE", "1d", "1y")

    allure.attach(str(calls), "调用顺序", allure.attachment_type.TEXT)
    assert calls == ["primary", "secondary"], "应依次尝试主源与备用源"
    assert len(df) == 30
    assert dfd._load_cache("us_FAKE_1d_1y") is not None, "成功结果应写入缓存"


@allure.feature("数据层")
@allure.story("多源兜底")
def test_get_us_serves_stale_cache_when_all_sources_fail(tmp_path, monkeypatch):
    """全部数据源失败时退回过期缓存，避免整站 500。"""
    from backend import data_feed as dfd

    monkeypatch.setattr(dfd, "CACHE_DIR", tmp_path)
    dfd._save_cache("us_OLD_1d_5y", synth_frame("2024-01-01", 40, daily=0.002))
    path = tmp_path / "us_OLD_1d_5y.csv"
    old = time.time() - 48 * 3600
    import os
    os.utime(path, (old, old))

    monkeypatch.setattr(dfd, "_us_sources",
                        lambda symbol: [lambda s, p: (_ for _ in ()).throw(RuntimeError("全挂"))])
    df = dfd.get_us("OLD", "1d", "5y")

    assert len(df) == 40, "应返回过期缓存兜底"


@allure.feature("数据层")
@allure.story("多源兜底")
def test_get_us_raises_when_no_cache_and_all_sources_fail(tmp_path, monkeypatch):
    from backend import data_feed as dfd

    monkeypatch.setattr(dfd, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(dfd, "_us_sources",
                        lambda symbol: [lambda s, p: (_ for _ in ()).throw(RuntimeError("全挂"))])
    try:
        dfd.get_us("NOPE", "1d", "5y")
    except RuntimeError as e:
        allure.attach(str(e), "异常信息", allure.attachment_type.TEXT)
        assert "all sources failed" in str(e)
        return
    raise AssertionError("无缓存且全源失败时应抛出明确异常")


@allure.feature("数据层")
@allure.story("多源兜底")
def test_market_source_lists_include_extra_fallbacks():
    from backend import data_feed as dfd

    us = [fn.__name__ for fn in dfd._us_sources("SPY")]
    hk = [fn.__name__ for fn in dfd._us_sources("2800.HK")]
    assert us == ["_yahoo_chart", "_yahoo_chart_query2", "_sina_us_daily", "_stooq_us_daily"]
    assert hk == ["_yahoo_chart", "_yahoo_chart_query2", "_tencent_hk_daily"]


@allure.feature("数据层")
@allure.story("轻量行情")
def test_get_quote_falls_back_from_tencent_to_yahoo(monkeypatch):
    from backend import data_feed as dfd

    calls = []
    def tencent(symbol, market):
        calls.append("tencent")
        raise RuntimeError("腾讯暂不可用")
    def yahoo(symbol, market):
        calls.append("yahoo")
        return {"price": 100.0, "prev_close": 99.0, "name": "SPDR S&P 500 ETF Trust",
                "source": "Yahoo Finance"}

    monkeypatch.setattr(dfd, "_tencent_quote", tencent)
    monkeypatch.setattr(dfd, "_yahoo_quote", yahoo)
    quote = dfd.get_quote("SPY", "us")
    assert calls == ["tencent", "yahoo"]
    assert quote["name"] == "SPDR S&P 500 ETF Trust"


@allure.feature("数据层")
@allure.story("A股接口")
def test_get_a_raises_clear_error_without_cache(monkeypatch):
    """A 股取数失败且无缓存时应抛出可诊断的错误，而不是返回空数据。"""
    from backend import data_feed as dfd

    class _DeadSession:
        def get(self, *args, **kwargs):
            raise RuntimeError("网络不可达")

    monkeypatch.setattr(dfd, "_SESSION", _DeadSession())
    monkeypatch.setattr(dfd, "_load_stale_cache", lambda key: None)

    try:
        dfd.get_a("sh999999", "20220601")
    except RuntimeError as e:
        allure.attach(str(e), "异常信息", allure.attachment_type.TEXT)
        assert "get_a" in str(e)
        return
    raise AssertionError("取数失败应抛出 RuntimeError")


@allure.feature("数据层")
@allure.story("缓存")
def test_corrupted_cache_file_is_ignored(tmp_path, monkeypatch):
    from backend import data_feed as dfd

    monkeypatch.setattr(dfd, "CACHE_DIR", tmp_path)
    (tmp_path / "broken.csv").write_text("日期,乱码\n???", encoding="utf-8")

    assert dfd._read_cache_csv(tmp_path / "broken.csv") is None


@allure.feature("数据层")
@allure.story("行情快照")
def test_get_a_live_returns_none_on_failure(monkeypatch):
    from backend import data_feed as dfd

    class _DeadSession:
        def get(self, *args, **kwargs):
            raise RuntimeError("快照接口异常")

    monkeypatch.setattr(dfd, "_SESSION", _DeadSession())

    assert dfd.get_a_live("sh000300") is None, "实时快照失败应返回 None 而非抛错"
