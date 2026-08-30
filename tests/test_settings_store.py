"""配置持久化测试：校验规则、首次访问、旧数据兼容与扩展段独立写入。"""
from __future__ import annotations

import json

import allure


# --------------------------------------------------------------------------- 核心参数
@allure.feature("配置持久化")
@allure.story("首次访问")
def test_missing_file_returns_defaults(tmp_settings_file):
    from backend import settings_store as st

    settings = st.load_settings()

    assert not tmp_settings_file.exists(), "本用例不应创建配置文件"
    assert settings == st.DEFAULT_PARAMS


@allure.feature("配置持久化")
@allure.story("参数校验")
def test_core_validation_rules(tmp_settings_file):
    from backend import settings_store as st

    valid_cases = [
        ("params_a", {"target_vol": 0.35, "trend_window": 200}),
        ("params_c", {"target_vol": 0.40, "trend_window": 150}),
        ("params_b", {"mom_window": 120, "ma_window": 60}),
    ]
    for key, raw in valid_cases:
        with allure.step(f"合法：{key} {raw}"):
            validated = st.validate_params(key, raw)
            assert all(validated[k] == v for k, v in raw.items())
            assert validated["assets"]
            if key == "params_b":
                assert validated["safe_asset"]

    invalid_cases = [
        ("params_a", {"target_vol": 0}, "波动率目标过小"),
        ("params_a", {"target_vol": 1.51}, "波动率目标过大"),
        ("params_a", {"trend_window": 19}, "SMA 过小"),
        ("params_a", {"trend_window": 200.5}, "SMA 非整数"),
        ("params_a", {"target_vol": "abc"}, "非数值"),
        ("params_b", {"mom_window": 501}, "动量窗口过大"),
        ("params_b", {"ma_window": 10}, "均线闸门过小"),
    ]
    for key, raw, label in invalid_cases:
        with allure.step(f"非法：{label}（{key} {raw}）"):
            try:
                st.validate_params(key, raw)
            except ValueError as e:
                allure.attach(str(e), "拒绝原因", allure.attachment_type.TEXT)
                continue
            raise AssertionError(f"{label} 应被拒绝却通过了校验")


@allure.feature("配置持久化")
@allure.story("ETF候选池")
def test_b_pool_validates_limit_duplicates_and_safe_asset(tmp_settings_file):
    from backend import settings_store as st

    valid = st.validate_params("params_b", {
        "assets": [{"code": "510300", "name": "沪深300ETF"}, "159915"],
    })
    assert [x["code"] for x in valid["assets"]] == ["sh510300", "sz159915"]

    invalid_pools = [
        [],
        [f"51{i:04d}" for i in range(11)],
        ["510300", "sh510300"],
        ["600519"],
    ]
    for assets in invalid_pools:
        try:
            st.validate_params("params_b", {"assets": assets})
        except ValueError:
            continue
        raise AssertionError(f"非法ETF池应被拒绝：{assets}")

    try:
        st.validate_params("params_b", {
            "assets": ["511010"],
            "safe_asset": "511010",
        })
    except ValueError as exc:
        assert "防御ETF" in str(exc)
    else:
        raise AssertionError("防御ETF不得重复进入风险候选池")


@allure.feature("配置持久化")
@allure.story("跨市场ETF候选池")
def test_a_c_pool_normalization_limits_and_market_isolation(tmp_settings_file):
    from backend import settings_store as st

    us = st.validate_params("params_a", {"assets": ["spy", {"code": "BRK-B"}]})
    hk = st.validate_params("params_c", {"assets": ["2800", "03033.HK"]})
    assert [x["code"] for x in us["assets"]] == ["SPY", "BRK-B"]
    assert [x["code"] for x in hk["assets"]] == ["2800.HK", "03033.HK"]
    assert all(x["name"] == "" for x in us["assets"] + hk["assets"]), "空名称应保留给后端解析"

    invalid = [
        ("params_a", ["2800.HK"]),
        ("params_a", ["SPY", "spy"]),
        ("params_c", ["SPY"]),
        ("params_c", ["2800", "2800.HK"]),
        ("params_c", []),
        ("params_a", [f"ETF{i}" for i in range(11)]),
    ]
    for key, assets in invalid:
        try:
            st.validate_params(key, {"assets": assets})
        except ValueError:
            continue
        raise AssertionError(f"{key} 非法候选池应被拒绝：{assets}")


@allure.feature("配置持久化")
@allure.story("未知策略")
def test_unknown_strategy_key_is_rejected(tmp_settings_file):
    from backend import settings_store as st

    try:
        st.validate_params("params_z", {"target_vol": 0.3})
    except ValueError:
        return
    raise AssertionError("未知策略配置应被拒绝")


@allure.feature("配置持久化")
@allure.story("持久化往返")
def test_save_then_load_roundtrip(tmp_settings_file):
    from backend import settings_store as st

    saved = st.save_settings({
        "params_a": {"target_vol": 0.42, "trend_window": 180},
        "params_b": {"mom_window": 90, "ma_window": 50},
        "params_c": {"target_vol": 0.45, "trend_window": 160},
    })
    loaded = st.load_settings()

    allure.attach(json.dumps(loaded, ensure_ascii=False, indent=2), "回读配置",
                  allure.attachment_type.JSON)
    assert loaded == saved
    assert loaded["params_a"]["target_vol"] == 0.42
    assert loaded["params_b"]["mom_window"] == 90


@allure.feature("配置持久化")
@allure.story("部分更新")
def test_saving_one_block_keeps_others(tmp_settings_file):
    """只提交 A 的参数时，B/C 应保持默认值而非被清空。"""
    from backend import settings_store as st

    st.save_settings({"params_a": {"target_vol": 0.5, "trend_window": 210}})
    loaded = st.load_settings()

    assert loaded["params_a"]["target_vol"] == 0.5
    assert loaded["params_b"] == st.DEFAULT_PARAMS["params_b"]
    assert loaded["params_c"] == st.DEFAULT_PARAMS["params_c"]


@allure.feature("配置持久化")
@allure.story("旧数据兼容")
def test_legacy_schema_is_migrated(tmp_settings_file):
    """旧版以 A/B/C 为键的配置文件应被自动迁移。"""
    from backend import settings_store as st

    tmp_settings_file.write_text(
        json.dumps({"A": {"target_vol": 0.33, "trend_window": 190}}), encoding="utf-8")
    loaded = st.load_settings()

    assert loaded["params_a"]["target_vol"] == 0.33
    assert loaded["params_a"]["trend_window"] == 190
    assert loaded["params_a"]["assets"] == st.DEFAULT_A_ASSETS
    assert loaded["params_b"] == st.DEFAULT_PARAMS["params_b"]


@allure.feature("配置持久化")
@allure.story("旧数据兼容")
def test_flat_legacy_params_are_migrated(tmp_settings_file):
    from backend import settings_store as st

    tmp_settings_file.write_text(json.dumps({"target_vol": 0.31, "trend_window": 170}),
                                 encoding="utf-8")
    loaded = st.load_settings()

    assert loaded["params_a"]["target_vol"] == 0.31


@allure.feature("配置持久化")
@allure.story("损坏数据")
def test_corrupt_block_falls_back_per_block(tmp_settings_file):
    """某一块损坏时只回退该块，不影响其余配置。"""
    from backend import settings_store as st

    tmp_settings_file.write_text(
        json.dumps({
            "params_a": {"target_vol": 0.38, "trend_window": 220},
            "params_b": {"mom_window": "坏数据"},
        }), encoding="utf-8")
    loaded = st.load_settings()

    assert loaded["params_a"]["target_vol"] == 0.38, "正常块应保留"
    assert loaded["params_b"] == st.DEFAULT_PARAMS["params_b"], "损坏块应回退默认"


@allure.feature("配置持久化")
@allure.story("损坏数据")
def test_broken_json_returns_defaults(tmp_settings_file):
    from backend import settings_store as st

    tmp_settings_file.write_text("{ 这不是合法 JSON", encoding="utf-8")

    assert st.load_settings() == st.DEFAULT_PARAMS


# --------------------------------------------------------------------------- 扩展段
@allure.feature("配置持久化")
@allure.story("扩展段")
def test_ext_roundtrip_preserves_core_params(tmp_settings_file):
    """写扩展配置时，核心 A/B/C 参数必须原样保留。"""
    from backend import settings_store as st

    st.save_settings({"params_a": {"target_vol": 0.44, "trend_window": 175}})
    st.save_ext_settings({"strategies": {"us_qqq_vt": {"enabled": False,
                                                       "params": {"target_vol": 0.3}}}})

    ext = st.load_ext_settings()
    core = st.load_settings()

    allure.attach(json.dumps({"ext": ext, "core": core}, ensure_ascii=False, indent=2),
                  "回读结果", allure.attachment_type.JSON)
    assert ext["strategies"]["us_qqq_vt"]["enabled"] is False
    assert core["params_a"]["target_vol"] == 0.44
    assert core["params_a"]["trend_window"] == 175
    assert core["params_a"]["assets"] == st.DEFAULT_A_ASSETS


@allure.feature("配置持久化")
@allure.story("扩展段")
def test_ext_missing_returns_empty(tmp_settings_file):
    from backend import settings_store as st

    assert st.load_ext_settings() == {}


@allure.feature("配置持久化")
@allure.story("扩展段")
def test_ext_broken_json_returns_empty(tmp_settings_file):
    from backend import settings_store as st

    tmp_settings_file.write_text("not json at all", encoding="utf-8")

    assert st.load_ext_settings() == {}


@allure.feature("配置持久化")
@allure.story("扩展段")
def test_ext_rejects_non_dict(tmp_settings_file):
    from backend import settings_store as st

    try:
        st.save_ext_settings(["not", "a", "dict"])
    except ValueError:
        return
    raise AssertionError("扩展配置格式非法时应报错")
