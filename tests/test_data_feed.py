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
def test_cache_version_change_discards_old_files(tmp_path, monkeypatch):
    """bar 标注口径变了，旧缓存必须被丢弃。

    否则线上修完代码后，服务仍在回放"日期错位一天"的旧 CSV，看起来像没修。
    """
    from backend import data_feed as dfd

    monkeypatch.setattr(dfd, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(dfd, "_version_ready", set())

    (tmp_path / "us_OLD_1d_5y.csv").write_text("date,close\n2024-01-01,1\n", encoding="utf-8")
    (tmp_path / "_version").write_text("1", encoding="utf-8")  # 上一版口径

    dfd._ensure_cache_version()

    assert not (tmp_path / "us_OLD_1d_5y.csv").exists(), "旧版本缓存应被清掉"
    assert (tmp_path / "_version").read_text(encoding="utf-8").strip() == dfd.CACHE_VERSION

    with allure.step("同版本再次调用不应清空"):
        (tmp_path / "us_NEW_1d_5y.csv").write_text("date,close\n2024-01-01,1\n", encoding="utf-8")
        monkeypatch.setattr(dfd, "_version_ready", set())
        dfd._ensure_cache_version()
        assert (tmp_path / "us_NEW_1d_5y.csv").exists()


@allure.feature("数据层")
@allure.story("缓存")
def test_cache_roundtrip_and_stale_fallback(tmp_path, monkeypatch):
    from backend import data_feed as dfd

    monkeypatch.setattr(dfd, "CACHE_DIR", tmp_path)
    frame = synth_frame("2024-01-01", 50, daily=0.001)
    dfd._save_cache("probe_key", frame)

    assert dfd._load_cache("probe_key", "US") is not None, "刚写入的缓存应命中"
    loaded = dfd._load_cache("probe_key", "US")
    assert len(loaded) == 50

    # 把文件时间推回 7 天前：早于任何一次收盘，不再算新鲜缓存
    path = tmp_path / "probe_key.csv"
    old = time.time() - 7 * 86400
    import os
    os.utime(path, (old, old))

    assert dfd._load_cache("probe_key", "US") is None, "过期缓存不应作为新鲜缓存返回"
    assert dfd._load_stale_cache("probe_key") is not None, "过期缓存仍应可作为兜底读取"


@allure.feature("数据层")
@allure.story("缓存新鲜度")
def test_cache_current_tracks_session_close_not_a_timer(tmp_path, monkeypatch):
    """缓存的生死线是「收盘时刻」，不是固定的几小时。

    复现 2026-10-08 的现场：13:04 抓的 A 股缓存，跨过 15:00 收盘后必须立刻
    失效（否则当天收盘数据要等到晚上才看得到），收盘前则应继续复用。
    """
    from backend import data_feed as dfd

    monkeypatch.setattr(dfd, "CACHE_DIR", tmp_path)
    dfd._save_cache("a_probe_20220601_now", synth_frame("2024-01-01", 10, daily=0.001))
    path = tmp_path / "a_probe_20220601_now.csv"
    tz = "Asia/Shanghai"

    def stamp(ts):
        import os
        os.utime(path, (ts.timestamp(), ts.timestamp()))

    stamp(pd.Timestamp("2026-10-08 13:04", tz=tz))
    with allure.step("收盘前（13:25）应复用缓存"):
        assert dfd._cache_is_current(path, "CN", pd.Timestamp("2026-10-08 13:25", tz=tz)) is True
    with allure.step("越过 15:00 收盘（15:30）应立即作废，触发重抓"):
        assert dfd._cache_is_current(path, "CN", pd.Timestamp("2026-10-08 15:30", tz=tz)) is False

    stamp(pd.Timestamp("2026-10-08 15:31", tz=tz))
    with allure.step("收盘后重抓一次即恢复新鲜，不会反复重抓"):
        assert dfd._cache_is_current(path, "CN", pd.Timestamp("2026-10-08 16:00", tz=tz)) is True
        assert dfd._cache_is_current(path, "CN", pd.Timestamp("2026-10-09 09:00", tz=tz)) is True

    with allure.step("隔一个交易日（10-09 收盘后）应再次作废"):
        assert dfd._cache_is_current(path, "CN", pd.Timestamp("2026-10-09 15:30", tz=tz)) is False


@allure.feature("数据层")
@allure.story("缓存")
def test_forcing_bypasses_cache(tmp_path, monkeypatch):
    from backend import data_feed as dfd

    monkeypatch.setattr(dfd, "CACHE_DIR", tmp_path)
    dfd._save_cache("probe_key", synth_frame("2024-01-01", 10, daily=0.001))

    assert dfd._load_cache("probe_key", "US") is not None
    with dfd.forcing(True):
        assert dfd._load_cache("probe_key", "US") is None, "强制刷新应跳过缓存"
    assert dfd._load_cache("probe_key", "US") is not None, "退出上下文后应恢复正常"


@allure.feature("数据层")
@allure.story("市场归属")
def test_market_of_classifies_hk():
    from backend import data_feed as dfd

    assert dfd.market_of("7200.HK") == "HK"
    assert dfd.market_of("2800.hk") == "HK"
    assert dfd.market_of("TQQQ") == "US"
    assert dfd.market_of("SPY") == "US"


@allure.feature("数据层")
@allure.story("时区标注")
def test_yahoo_bars_use_exchange_local_date(monkeypatch):
    """同一个 UTC 时刻，港股 bar 必须标成香港当天。

    修复前所有市场一律转成纽约时区，港股 10-08 09:30 的 bar 被标成 10-07，
    导致「数据截至」永远少一天。
    """
    from backend import data_feed as dfd
    import datetime as _dt

    # 2026-10-08 01:30 UTC == 香港 10-08 09:30 开盘 == 纽约 10-07 21:30
    ts = int(_dt.datetime(2026, 10, 8, 1, 30, tzinfo=_dt.timezone.utc).timestamp())
    payload = {"chart": {"result": [{
        "timestamp": [ts],
        "indicators": {"quote": [{"open": [1.0], "high": [1.1], "low": [0.9],
                                  "close": [1.05], "volume": [1000]}]},
    }]}}

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return payload

    class _Sess:
        def get(self, *a, **k):
            return _Resp()

    monkeypatch.setattr(dfd, "_SESSION", _Sess())

    hk = dfd._yahoo_chart_host("7200.HK", "1mo", "example.invalid")
    with allure.step(f"港股 bar 日期 = {hk.index[-1]}"):
        assert str(hk.index[-1].date()) == "2026-10-08"
        assert str(hk.index[-1].time()) == "09:30:00"

    us = dfd._yahoo_chart_host("TQQQ", "1mo", "example.invalid")
    with allure.step(f"美股 bar 日期 = {us.index[-1]}"):
        assert str(us.index[-1].date()) == "2026-10-07"


@allure.feature("数据层")
@allure.story("未收盘K线")
def test_drop_incomplete_session_discards_live_bar():
    """盘中那根 K 线的 close 是实时价，绝不能当成收盘数据喂给回测。"""
    from backend import data_feed as dfd

    idx = pd.to_datetime(["2026-10-06", "2026-10-07", "2026-10-08"])
    frame = pd.DataFrame({"open": [1.0, 2.0, 3.0], "high": [1.0, 2.0, 3.0],
                          "low": [1.0, 2.0, 3.0], "close": [1.0, 2.0, 3.0],
                          "volume": [1, 2, 3]}, index=idx)

    with allure.step("港股盘中 13:19（<16:00 收盘）→ 丢掉当天"):
        trimmed = dfd.drop_incomplete_session(frame, "HK", pd.Timestamp("2026-10-08 13:19", tz="Asia/Hong_Kong"))
        assert len(trimmed) == 2
        assert str(trimmed.index[-1].date()) == "2026-10-07"

    with allure.step("港股收盘后 16:30 → 保留当天"):
        assert len(dfd.drop_incomplete_session(frame, "HK", pd.Timestamp("2026-10-08 16:30", tz="Asia/Hong_Kong"))) == 3

    with allure.step("美股盘前 08:00（10-08 session 尚未结束）→ 即便源给了 bar 也丢掉"):
        assert len(dfd.drop_incomplete_session(frame, "US", pd.Timestamp("2026-10-08 08:00", tz="America/New_York"))) == 2

    with allure.step("美股盘中 09:45 → 丢掉当天"):
        assert len(dfd.drop_incomplete_session(frame, "US", pd.Timestamp("2026-10-08 09:45", tz="America/New_York"))) == 2

    with allure.step("美股收盘后 16:30 → 保留当天"):
        assert len(dfd.drop_incomplete_session(frame, "US", pd.Timestamp("2026-10-08 16:30", tz="America/New_York"))) == 3

    with allure.step("最后一根不是今天 → 原样返回"):
        assert len(dfd.drop_incomplete_session(frame, "CN", pd.Timestamp("2026-10-09 10:00", tz="Asia/Shanghai"))) == 3


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
    assert dfd._load_cache("us_FAKE_1d_1y", "US") is not None, "成功结果应写入缓存"


@allure.feature("数据层")
@allure.story("多源兜底")
def test_get_us_skips_source_that_ends_early(tmp_path, monkeypatch):
    """有数据但不够新时必须继续往下试。

    现场：Yahoo 对港股 2026-10-07 整行返回 null，序列静悄悄少一天。旧的
    「拿到非空结果就用」逻辑会让「数据截至」倒退一天。
    """
    from backend import data_feed as dfd

    monkeypatch.setattr(dfd, "CACHE_DIR", tmp_path)

    def mk(end):
        idx = pd.bdate_range(end=end, periods=6)
        return pd.DataFrame({"open": 1.0, "high": 1.0, "low": 1.0,
                             "close": 1.0, "volume": 1.0}, index=idx)

    gapped, complete = mk("2024-06-10"), mk("2024-06-11")
    calls = []

    monkeypatch.setattr(dfd, "_us_sources", lambda symbol: [
        lambda s, p: (calls.append("gapped") or gapped),
        lambda s, p: (calls.append("complete") or complete),
    ])
    monkeypatch.setattr(dfd, "_covers_latest_close",
                        lambda frame, market, now=None: frame is complete)

    df = dfd.get_us("7200.HK", "1d", "1y")
    allure.attach(str(calls), "调用顺序", allure.attachment_type.TEXT)
    assert calls == ["gapped", "complete"], "不够新的源必须让位给下一个源"
    assert df is complete


@allure.feature("数据层")
@allure.story("多源兜底")
def test_get_us_stops_at_first_source_reaching_latest_close(tmp_path, monkeypatch):
    """够新的源应立刻收工，不能多打无关请求。"""
    from backend import data_feed as dfd

    monkeypatch.setattr(dfd, "CACHE_DIR", tmp_path)

    def mk(end):
        idx = pd.bdate_range(end=end, periods=6)
        return pd.DataFrame({"open": 1.0, "high": 1.0, "low": 1.0,
                             "close": 1.0, "volume": 1.0}, index=idx)

    first = mk("2024-06-11")
    calls = []
    monkeypatch.setattr(dfd, "_us_sources", lambda symbol: [
        lambda s, p: (calls.append("first") or first),
        lambda s, p: (calls.append("second") or mk("2024-06-12")),
    ])
    monkeypatch.setattr(dfd, "_covers_latest_close", lambda frame, market, now=None: True)

    dfd.get_us("TQQQ", "1d", "1y")
    assert calls == ["first"], "首个够新的源应直接结束搜索"


@allure.feature("数据层")
@allure.story("多源兜底")
def test_get_us_falls_back_to_earliest_result_when_none_reaches_latest_close(tmp_path, monkeypatch):
    """全都不够新（例如长假）时仍应返回数据，而不是报错。"""
    from backend import data_feed as dfd

    monkeypatch.setattr(dfd, "CACHE_DIR", tmp_path)

    def mk(end):
        idx = pd.bdate_range(end=end, periods=6)
        return pd.DataFrame({"open": 1.0, "high": 1.0, "low": 1.0,
                             "close": 1.0, "volume": 1.0}, index=idx)

    stale = mk("2024-06-10")
    newer = mk("2024-06-11")
    monkeypatch.setattr(dfd, "_us_sources", lambda symbol: [
        lambda s, p: stale, lambda s, p: newer])
    monkeypatch.setattr(dfd, "_covers_latest_close", lambda frame, market, now=None: False)

    df = dfd.get_us("TQQQ", "1d", "1y")
    assert len(df) == 6, "无源够新时仍应返回数据"


@allure.feature("数据层")
@allure.story("缓存新鲜度")
def test_covers_latest_close_detects_missing_day():
    """最近一根必须对齐「最近一个已收盘的工作日」。"""
    from backend import data_feed as dfd

    def mk(end):
        idx = pd.bdate_range(end=end, periods=6)
        return pd.DataFrame({"close": 1.0}, index=idx)

    with allure.step("港股盘中（13:25 < 16:00）：最近收盘日是 10-07"):
        mid = pd.Timestamp("2026-10-08 13:25", tz="Asia/Hong_Kong")
        assert dfd._covers_latest_close(mk("2026-10-07"), "HK", mid) is True
        assert dfd._covers_latest_close(mk("2026-10-06"), "HK", mid) is False

    with allure.step("港股收盘后（16:30）：最近收盘日变成 10-08"):
        after = pd.Timestamp("2026-10-08 16:30", tz="Asia/Hong_Kong")
        assert dfd._covers_latest_close(mk("2026-10-08"), "HK", after) is True
        assert dfd._covers_latest_close(mk("2026-10-07"), "HK", after) is False

    with allure.step("周末回退到周五 10-09"):
        weekend = pd.Timestamp("2026-10-10 12:00", tz="Asia/Hong_Kong")
        assert dfd._covers_latest_close(mk("2026-10-09"), "HK", weekend) is True
        assert dfd._covers_latest_close(mk("2026-10-08"), "HK", weekend) is False

    with allure.step("空序列不算覆盖"):
        assert dfd._covers_latest_close(pd.DataFrame(), "HK", mid) is False


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
