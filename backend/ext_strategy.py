"""Extended instrument groups & strategy registry — additive layer.

设计原则（与核心 A/B/C 的隔离）：
- 本模块不被 engine.py / 现有 /api 端点引用；只通过 /api/ext/* 暴露。
- 每套策略独立构建：单个标的或数据源失败只标记该策略为 error，
  不影响其他策略，更不影响 /api/summary。
- 参数持久化在 strategy_settings.json 的 "ext" 键下，与 params_a/b/c 互不读写。

标的组与策略矩阵
================
G1 美股核心杠杆 (us_core)   —— 筛选: 美股上市、规模>10亿美元、日均成交>500万股的
                               宽基杠杆 ETF；信号用一倍宽基，执行用对应杠杆品。
G2 A股行业轮动 (a_industry) —— 筛选: 规模>50亿、流动性充足的行业/主题/宽基 ETF + 防御资产。
G3 港股恒指家族 (hk)        —— 筛选: 港交所上市、流动性充足的恒指/恒科宽基与杠杆 ETF。
G4 避险资产 (safe)          —— 筛选: 与股票低相关的资产（黄金、国债、红利）。

每套策略四类能力覆盖：选股(rotation)、择时(dual_ma/vol_target)、风控(stop_dd/防御切换)、
执行(周频决策+次日执行+换手成本+滞回带)。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import data_feed as dfd
from .backtest import run_weight_backtest, run_multi_asset, metrics, slice_window, renorm
from .strategies import vol_target_tqqq

WINDOWS_EXT = ["full", "3y", "1y"]
WINDOW_LABELS_EXT = {"full": "全历史", "3y": "近3年", "1y": "近1年"}


# ---------------------------------------------------------------------------
# Generic strategy functions (new; core strategies.py untouched)
# ---------------------------------------------------------------------------
def dual_ma_timing(close: pd.Series, fast: int, slow: int) -> pd.Series:
    """双均线择时：fast 均线在 slow 均线上方 → 持有(1.0)，否则空仓(0.0)。
    均线数据不足时保持空仓，杜绝冷启动误信号。"""
    mf = close.rolling(fast, min_periods=fast).mean()
    ms = close.rolling(slow, min_periods=slow).mean()
    w = (mf > ms).astype(float)
    w[mf.isna() | ms.isna()] = 0.0
    return w.rename("weight")


def rotation_generic(
    frames: dict[str, pd.DataFrame],
    *,
    candidates: list[str],
    safe_code: str,
    mom_window: int = 120,
    vol_window: int = 60,
    ma_window: int = 60,
    weekly: bool = True,
    buffer_pct: float = 0.10,
) -> pd.DataFrame:
    """风险调整动量轮动（泛化版，与 B 同构但标的池可配置）。
    分数 = mom_window 日涨幅 ÷ vol_window 日波动 × √252；
    只有站上 ma_window 均线的候选才有资格；全部不合格 → 防御资产；
    在位标的享受 buffer_pct 换仓缓冲（挑战者优势不足不换）。"""
    closes = pd.DataFrame({c: f["close"] for c, f in frames.items()}).sort_index()
    rets = closes.pct_change()
    score = closes.pct_change(mom_window) / rets.rolling(vol_window).std() * np.sqrt(252)
    above_ma = closes > closes.rolling(ma_window).mean()

    if weekly:
        key = closes.index.to_period("W")
        is_last = pd.Series(key, index=closes.index) != pd.Series(key, index=closes.index).shift(-1)
        dec_idx = closes.index[is_last.fillna(False)]
    else:
        dec_idx = closes.index

    rows = []
    prev_pick = safe_code
    for t in dec_idx:
        s = score.loc[t]
        ok = above_ma.loc[t]
        cands = [c for c in candidates
                 if c in closes.columns and bool(ok.get(c, False)) and np.isfinite(s.get(c, np.nan))]
        if cands:
            leader = max(cands, key=lambda c: s[c])
            incumbent_ok = prev_pick in cands and np.isfinite(s.get(prev_pick, np.nan))
            if incumbent_ok and s[leader] <= s[prev_pick] * (1.0 + buffer_pct):
                pick, reason = prev_pick, "在位标的仍有效，挑战者优势未达换仓阈值"
            else:
                pick, reason = leader, "动量最优且在均线上方"
        else:
            pick, reason = safe_code, "候选资产趋势走弱，转入防御"
        rows.append({"date": t, "pick": pick, "weight": 1.0, "reason": reason})
        prev_pick = pick
    return pd.DataFrame(rows).set_index("date")


# ---------------------------------------------------------------------------
# Registry: groups (标的组)
# ---------------------------------------------------------------------------
EXT_GROUPS: dict[str, dict] = {
    "us_core": {
        "name": "G1 · 美股核心杠杆",
        "market": "美股",
        "screening": "美股上市、规模>10亿美元、日均成交>500万股的宽基杠杆 ETF；"
                     "信号用一倍宽基（QQQ/SPY，历史更长更干净），执行用对应杠杆品",
        "data_source": "Yahoo Finance 主源 + 新浪美股兜底（免费日线，缓存 6 小时）",
        "benchmark_note": "执行标的买入持有",
        "assets": {
            "QQQ": "纳指100（信号源）", "TQQQ": "纳指100三倍", "QLD": "纳指100两倍",
            "SPY": "标普500（信号源）", "UPRO": "标普500三倍",
        },
    },
    "a_industry": {
        "name": "G2 · A股行业轮动",
        "market": "A股",
        "screening": "规模>50亿元、日均成交充足、跟踪误差小的行业/主题/风格 ETF，"
                     "覆盖成长(创业板/半导体)、消费(酒)、医药、红利五类互补风格，"
                     "并保留沪深300 宽基作为低波动核心选项；国债 ETF 为防御资产",
        "data_source": "新浪财经 A 股 K 线（免费日线，缓存 6 小时；起点 2022-06）",
        "benchmark_note": "沪深300 买入持有",
        "assets": {
            "sz159915": "创业板ETF(成长)", "sh512480": "半导体ETF", "sh512690": "酒ETF",
            "sh512010": "医药ETF", "sh510880": "红利ETF(价值)", "sh510300": "沪深300ETF(核心)",
            "sh518880": "黄金ETF", "sh511010": "国债ETF(防御)",
        },
    },
    "hk": {
        "name": "G3 · 港股恒指家族",
        "market": "港股",
        "screening": "港交所上市、流动性充足的恒指/恒生科技宽基与杠杆 ETF",
        "data_source": "Yahoo Finance 主源 + 腾讯港股兜底（免费日线，缓存 6 小时）",
        "benchmark_note": "执行标的买入持有",
        "assets": {
            "2800.HK": "盈富基金(恒指一倍)", "7200.HK": "南方恒指两倍", "3033.HK": "华夏恒生科技",
        },
    },
    "safe": {
        "name": "G4 · 避险资产",
        "market": "A股(黄金/国债/红利)",
        "screening": "与股票相关性低的资产：黄金 ETF、国债 ETF、红利 ETF；"
                     "用于降低组合整体回撤",
        "data_source": "新浪财经 A 股 K 线（免费日线，缓存 6 小时；起点 2022-06）",
        "benchmark_note": "黄金 ETF 买入持有",
        "assets": {"sh518880": "黄金ETF", "sh510880": "红利ETF", "sh511010": "国债ETF(防御)"},
    },
}


# ---------------------------------------------------------------------------
# Registry: strategies (策略)
# ---------------------------------------------------------------------------
EXT_STRATEGIES: dict[str, dict] = {
    # ---------------- G1 美股核心杠杆 ----------------
    "us_qqq_vt": {
        "group": "us_core", "kind_label": "择时+风控", "kind": "vol_target",
        "name": "纳指波动率目标（TQQQ）",
        "freq_label": "日级信号 · 次日执行",
        "description": "与核心策略 A 同框架但参数独立：QQQ 一倍指数做信号，TQQQ 做执行。"
                       "仓位 = min(100%, 目标波动÷实现波动)，SMA200 趋势闸门之上才允许持仓；"
                       "跟踪回撤超过止损线后清仓，重新站上 SMA50 才复活。",
        "trigger": "每日收盘计算；仓位变化≥滞回带阈值才调仓；跌破 SMA200 或触发回撤止损 → 空仓",
        "default_params": {"vol_window": 40, "target_vol": 0.35, "trend_window": 200,
                           "min_change": 0.10, "stop_dd": 0.25},
        "sig": "QQQ", "veh": "TQQQ",
    },
    "us_spy_dma": {
        "group": "us_core", "kind_label": "择时", "kind": "dual_ma",
        "name": "标普双均线择时（UPRO）",
        "freq_label": "日级信号 · 次日执行",
        "description": "经典金叉死叉：SPY 的 fast 日均线在 slow 日均线上方 → 全仓 UPRO(标普三倍)；"
                       "下方 → 空仓。规则最简单、换手最低，作为波动率目标策略的对照组。",
        "trigger": "每日收盘比较两条均线；上穿金叉次日买入，下穿死叉次日清仓",
        "default_params": {"fast": 50, "slow": 200},
        "sig": "SPY", "veh": "UPRO",
    },
    "us_qld_vt": {
        "group": "us_core", "kind_label": "择时+风控", "kind": "vol_target",
        "name": "纳指稳健波动目标（QLD 两倍）",
        "freq_label": "日级信号 · 次日执行",
        "description": "TQQQ 的降杠杆替代：改持两倍 QLD 并压低目标波动到 25%，"
                       "不开回撤止损（依靠波动退出自动降仓）。"
                       "适合希望保留杠杆弹性但更怕深回撤的组合。",
        "trigger": "每日收盘计算目标仓位；变化≥10个百分点才动作；跌破 SMA200 清仓",
        "default_params": {"vol_window": 40, "target_vol": 0.25, "trend_window": 200,
                           "min_change": 0.10, "stop_dd": 0},
        "sig": "QQQ", "veh": "QLD",
    },
    # ---------------- G2 A股行业轮动 ----------------
    "a_industry_rot": {
        "group": "a_industry", "kind_label": "选股+执行", "kind": "rotation",
        "name": "行业动量轮动 Top1（周频）",
        "freq_label": "周频决策 · 次日执行",
        "description": "与核心策略 B 同构但候选池换成行业 ETF：创业板/半导体/酒/医药/红利/沪深300 六个风格，"
                       "风险调整动量打分，只有站上均线的候选有资格；全部不合格转入国债 ETF。"
                       "在位标的享受换仓缓冲，抑制无意义换手。"
                       "注意：行业 ETF 波动与回撤显著高于宽基，本组为高波动高换手定位。",
        "trigger": "每周最后交易日收盘打分；挑战者优势≥缓冲阈值才换仓；全池跌破均线 → 防御",
        "default_params": {"mom_window": 120, "vol_window": 60, "ma_window": 60,
                           "weekly": True, "buffer_pct": 0.10, "target_vol": 0.20},
        "param_specs": {"target_vol": {"label": "目标波动(0=关闭缩放)", "min": 0.0,
                                       "max": 1.50, "integer": False}},
        "candidates": ["sz159915", "sh512480", "sh512690", "sh512010", "sh510880", "sh510300"],
        "safe": "sh511010", "benchmark": "sh000300", "start": "20220601",
    },
    "a_gold_red_rot": {
        "group": "a_industry", "kind_label": "选股+风控", "kind": "rotation",
        "name": "黄金×红利防御轮动（日频）",
        "freq_label": "日频决策 · 次日执行",
        "description": "只在两个低回撤资产里二选一：黄金 ETF 与红利 ETF，日频动量打分，"
                       "趋势走坏立刻躲进国债 ETF。定位是组合的防御舱位，"
                       "与行业轮动形成互补而非替代。",
        "trigger": "每日收盘打分；动量领先者且在均线上方则持有，否则清仓转国债",
        "default_params": {"mom_window": 60, "vol_window": 40, "ma_window": 40,
                           "weekly": False, "buffer_pct": 0.05, "target_vol": 0},
        "param_specs": {"target_vol": {"label": "目标波动(0=关闭缩放)", "min": 0.0,
                                       "max": 1.50, "integer": False}},
        "candidates": ["sh518880", "sh510880"],
        "safe": "sh511010", "benchmark": "sh518880", "start": "20220601",
    },
    # ---------------- G3 港股 ----------------
    "hk_vt": {
        "group": "hk", "kind_label": "择时+风控", "kind": "vol_target",
        "name": "恒指两倍波动率目标（7200.HK）",
        "freq_label": "日级信号 · 次日执行",
        "description": "与核心策略 C 同框架但参数独立：2800.HK 盈富基金做信号，7200.HK 两倍杠杆执行。"
                       "恒指波动高于纳指，目标波动放宽到 40%；趋势闸门 SMA200 之上才持仓。",
        "trigger": "每日收盘计算；仓位变化≥10个百分点才调仓；跌破 SMA200 → 空仓",
        "default_params": {"vol_window": 20, "target_vol": 0.40, "trend_window": 200,
                           "min_change": 0.10, "stop_dd": 0},
        "sig": "2800.HK", "veh": "7200.HK",
    },
    "hk_tech_dma": {
        "group": "hk", "kind_label": "择时", "kind": "dual_ma",
        "name": "恒生科技双均线择时（3033.HK）",
        "freq_label": "日级信号 · 次日执行",
        "description": "恒生科技波动远大于恒指，用更灵敏的中周期均线（20/60 日）跟踪："
                       "快线在慢线上方持有 3033.HK，下方空仓。科技板块趋势性强，"
                       "中周期均线能比 SMA200 更早离场。",
        "trigger": "每日收盘比较 20/60 日均线；金叉次日买入，死叉次日清仓",
        "default_params": {"fast": 20, "slow": 60},
        "sig": "3033.HK", "veh": "3033.HK",
    },
    # ---------------- G4 避险资产 ----------------
    "safe_gold_dma": {
        "group": "safe", "kind_label": "择时", "kind": "dual_ma",
        "name": "黄金双均线择时（518880）",
        "freq_label": "日级信号 · 次日执行",
        "description": "黄金长期是股票的相关性低的避险资产，但也有 20%+ 的回撤期。"
                       "用 20/60 日双均线跟踪：趋势向上持有黄金 ETF，走弱离场，"
                       "目标是吃趋势同时躲开商品大级别回撤。",
        "trigger": "每日收盘比较 20/60 日均线；金叉次日买入，死叉次日清仓",
        "default_params": {"fast": 20, "slow": 60},
        "sig": "sh518880", "veh": "sh518880", "market": "a", "start": "20220601",
    },
    "safe_rot": {
        "group": "safe", "kind_label": "选股+执行", "kind": "rotation",
        "name": "避险资产轮动（黄金×红利，周频）",
        "freq_label": "周频决策 · 次日执行",
        "description": "黄金与红利是两类低相关防御资产，动量强者在两者间切换，"
                       "全都不合格时持有国债。作为账户的压舱石策略，"
                       "预期收益温和但回撤显著小于股票。",
        "trigger": "每周最后交易日收盘打分；挑战者优势≥5% 才换仓；全池走弱 → 国债",
        "default_params": {"mom_window": 60, "vol_window": 40, "ma_window": 40,
                           "weekly": True, "buffer_pct": 0.05, "target_vol": 0},
        "param_specs": {"target_vol": {"label": "目标波动(0=关闭缩放)", "min": 0.0,
                                       "max": 1.50, "integer": False}},
        "candidates": ["sh518880", "sh510880"],
        "safe": "sh511010", "benchmark": "sh518880", "start": "20220601",
    },
}

# 校验规格：min/max/integer/bool；未列出的参数不允许通过 API 修改
PARAM_SPECS: dict[str, dict[str, dict]] = {
    "vol_window":  {"label": "波动窗口(日)", "min": 10, "max": 120, "integer": True},
    "target_vol":  {"label": "目标波动(年化)", "min": 0.05, "max": 1.50, "integer": False},
    "trend_window": {"label": "趋势闸门SMA(日)", "min": 20, "max": 500, "integer": True},
    "min_change":  {"label": "滞回带阈值", "min": 0.0, "max": 0.50, "integer": False},
    "stop_dd":     {"label": "回撤止损(0=关闭)", "min": 0.0, "max": 0.60, "integer": False},
    "fast":        {"label": "快线(日)", "min": 5, "max": 120, "integer": True},
    "slow":        {"label": "慢线(日)", "min": 20, "max": 400, "integer": True},
    "mom_window":  {"label": "动量窗口(日)", "min": 20, "max": 250, "integer": True},
    "vol_window_r": {"label": "波动窗口(日)", "min": 20, "max": 120, "integer": True},
    "ma_window":   {"label": "均线门槛(日)", "min": 20, "max": 250, "integer": True},
    "buffer_pct":  {"label": "换仓缓冲", "min": 0.0, "max": 0.50, "integer": False},
}


def validate_ext_params(sid: str, raw: dict | None) -> dict:
    """Validate params for one extended strategy; returns canonical merged dict."""
    meta = EXT_STRATEGIES.get(sid)
    if meta is None:
        raise ValueError("未知扩展策略")
    base = dict(meta["default_params"])
    if raw is None:
        return base
    if not isinstance(raw, dict):
        raise ValueError("参数格式无效")
    for key, value in raw.items():
        if key not in base:
            raise ValueError(f"参数 {key} 不属于该策略")
        if key == "weekly":
            if not isinstance(value, bool):
                raise ValueError("weekly 必须是布尔值")
            base[key] = value
            continue
        spec = (meta.get("param_specs") or {}).get(key) or PARAM_SPECS.get(key)
        if spec is None:
            raise ValueError(f"参数 {key} 不可修改")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{spec['label']}必须是数值")
        num = float(value)
        if not np.isfinite(num):
            raise ValueError(f"{spec['label']}必须是有限数值")
        if not spec["min"] <= num <= spec["max"]:
            raise ValueError(f"{spec['label']}应在 {spec['min']} 至 {spec['max']} 之间")
        if spec["integer"] and not float(num).is_integer():
            raise ValueError(f"{spec['label']}必须是整数")
        base[key] = int(num) if spec["integer"] else round(num, 4)
    # cross-field rules
    if "fast" in base and "slow" in base and base["fast"] >= base["slow"]:
        raise ValueError("快线周期必须小于慢线周期")
    return base


def ext_meta() -> dict:
    """Registry metadata for the frontend (static description of groups/strategies)."""
    return {
        "groups": EXT_GROUPS,
        "strategies": {
            sid: {k: v for k, v in meta.items() if k not in ("sig", "veh", "candidates",
                                                             "safe", "benchmark", "start")}
            for sid, meta in EXT_STRATEGIES.items()
        },
    }


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------
def _fetch(sig: str, veh: str) -> tuple[pd.DataFrame, pd.DataFrame, str]:
    """Fetch signal+vehicle frames for whatever market the codes belong to."""
    market = "hk" if sig.endswith(".HK") else ("a" if len(sig) == 8 and sig[:2] in ("sh", "sz") else "us")
    if market == "a":
        sig_df = dfd.get_a(sig, "20220601")
        veh_df = dfd.get_a(veh, "20220601")
    else:
        sig_df = dfd.get_us(sig, "1d", "5y")
        veh_df = dfd.get_us(veh, "1d", "5y")
    return sig_df, veh_df, market


def _window_payload(bt: pd.DataFrame, window: str, bench_override: pd.Series | None = None) -> dict:
    seg = renorm(slice_window(bt, window))
    m_strat = metrics(seg["equity"], seg["ret"], seg["weight"])
    bench_eq = bench_override.reindex(seg.index).dropna() if bench_override is not None else seg["bench_equity"]
    m_bench = metrics(bench_eq, bench_eq.pct_change().fillna(0))
    step = max(1, len(seg) // 200)
    return {
        "window": window, "label": WINDOW_LABELS_EXT[window],
        "start": str(seg.index[0].date()), "end": str(seg.index[-1].date()),
        "strategy": m_strat, "benchmark": m_bench,
        "beats_benchmark": (m_strat.get("total_return") or -999) > (m_bench.get("total_return") or 999),
        "dates": [str(d.date()) for d in seg.index[::step]],
        "equity": [round(float(v), 4) for v in seg["equity"][::step]],
        "bench_equity": [round(float(v), 4) for v in bench_eq[::step]],
        "drawdown": [round(float(v) * 100, 2) for v in seg["dd"][::step]],
    }


def _meta_payload(sid: str, enabled: bool) -> dict:
    meta = EXT_STRATEGIES[sid]
    group = EXT_GROUPS[meta["group"]]
    return {
        "id": sid, "group": meta["group"], "group_name": group["name"],
        "name": meta["name"], "kind_label": meta["kind_label"], "kind": meta["kind"],
        "freq_label": meta["freq_label"], "description": meta["description"],
        "trigger": meta["trigger"], "enabled": enabled,
        "benchmark_note": group["benchmark_note"],
        "assets": group["assets"],
    }


def _build_vol_target(sid: str, p: dict) -> tuple[pd.DataFrame, dict]:
    meta = EXT_STRATEGIES[sid]
    sig_df, veh_df, _ = _fetch(meta["sig"], meta["veh"])
    if meta["sig"].endswith(".HK"):  # align HK calendars like engine C
        px_sig = sig_df["close"].reindex(veh_df.index).ffill()
        sig_df = sig_df.copy(); sig_df["close"] = px_sig
    else:
        px_sig = sig_df["close"].reindex(veh_df.index).ffill()
    w = vol_target_tqqq(
        sig_df, veh_df["close"], freq="D",
        vol_window=p["vol_window"], target_vol=p["target_vol"],
        trend_window=p["trend_window"], min_change=p["min_change"],
        stop_dd=(p["stop_dd"] if p.get("stop_dd") else None),
    )
    bt = run_weight_backtest(w, veh_df["close"])
    rv_now = float((px_sig.pct_change().rolling(p["vol_window"]).std() * np.sqrt(252)).iloc[-1])
    gate_now = bool(px_sig.iloc[-1] > px_sig.rolling(p["trend_window"]).mean().iloc[-1])
    current = {
        "exposure": round(float(w.iloc[-1]) * 100),
        "realized_vol": round(rv_now * 100, 1),
        "target_vol": p["target_vol"] * 100,
        "trend_gate_on": gate_now,
        "stop_dd": p.get("stop_dd") or None,
        "as_of": str(veh_df.index[-1].date()),
    }
    return bt, current


def _build_dual_ma(sid: str, p: dict) -> tuple[pd.DataFrame, dict]:
    meta = EXT_STRATEGIES[sid]
    sig_df, veh_df, _ = _fetch(meta["sig"], meta["veh"])
    close = veh_df["close"]
    w = dual_ma_timing(close, p["fast"], p["slow"])
    bt = run_weight_backtest(w, close)
    mf_now = float(close.rolling(p["fast"]).mean().iloc[-1])
    ms_now = float(close.rolling(p["slow"]).mean().iloc[-1])
    current = {
        "position": "持有" if w.iloc[-1] > 0 else "空仓",
        "ma_fast": round(mf_now, 3), "ma_slow": round(ms_now, 3),
        "fast": p["fast"], "slow": p["slow"],
        "as_of": str(close.index[-1].date()),
    }
    return bt, current


def _build_rotation(sid: str, p: dict) -> tuple[pd.DataFrame, dict, list[dict], pd.Series | None]:
    meta = EXT_STRATEGIES[sid]
    start = meta.get("start", "20220601")
    codes = list(meta["candidates"]) + [meta["safe"]]
    if meta.get("benchmark"):
        codes = codes + [meta["benchmark"]]
    frames: dict[str, pd.DataFrame] = {}
    failed: list[str] = []
    for c in dict.fromkeys(codes):  # dedupe, keep order
        try:
            frames[c] = dfd.get_a(c, start)
        except Exception:  # noqa: BLE001 — isolated per symbol
            failed.append(c)
    usable = [c for c in meta["candidates"] if c in frames]
    if len(usable) < 1 or meta["safe"] not in frames:
        raise RuntimeError("候选数据不足: 失败标的 " + (", ".join(failed) or "无"))
    decisions = rotation_generic(
        frames, candidates=usable, safe_code=meta["safe"],
        mom_window=p["mom_window"], vol_window=p["vol_window"],
        ma_window=p["ma_window"], weekly=p["weekly"], buffer_pct=p["buffer_pct"],
    )
    closes = pd.DataFrame({c: frames[c]["close"] for c in frames}).sort_index()
    # NOTE: every switch resets ALL columns before setting the new pick.
    # Setting only the new pick leaves earlier picks at 1.0 and silently creates
    # multi-times leverage — the known avg_exposure=400% anomaly of the core B
    # builder. Extended strategies use the corrected single-asset semantics.
    weights = pd.DataFrame(0.0, index=closes.index, columns=closes.columns)
    for t, row in decisions.iterrows():
        pos = closes.index.searchsorted(t)
        exec_idx = closes.index[min(pos + 1, len(closes) - 1)]
        weights.loc[exec_idx:, :] = 0.0
        weights.loc[exec_idx:, row["pick"]] = 1.0
    # Optional risk layer: scale the held position by a volatility target
    # (target_vol=0 disables it). Quantised to 5pp steps to limit churn.
    if p.get("target_vol"):
        rv = closes.pct_change().rolling(p["vol_window"]).std() * np.sqrt(252)
        scale = (p["target_vol"] / rv.replace(0, np.nan)).clip(upper=1.0).fillna(0.0)
        scale = (scale / 0.05).round() * 0.05
        weights = weights.mul(scale, axis=0)
    bt = run_multi_asset(weights, closes, cost_rate=0.0015)
    bench_override = None
    if meta.get("benchmark") and meta["benchmark"] in closes:
        b = closes[meta["benchmark"]].dropna()
        bench_override = b / b.iloc[0]
    last = decisions.iloc[-1]
    name_of = {c: EXT_GROUPS[meta["group"]]["assets"].get(c, c) for c in closes.columns}
    current = {
        "pick": str(last["pick"]), "pick_name": name_of.get(str(last["pick"]), str(last["pick"])),
        "reason": str(last["reason"]), "decision_date": str(decisions.index[-1].date()),
        "as_of": str(closes.index[-1].date()), "failed_symbols": failed,
    }
    recent = [
        {"date": str(t.date()), "pick": name_of.get(str(r["pick"]), str(r["pick"])), "reason": str(r["reason"])}
        for t, r in decisions.tail(12).iloc[::-1].iterrows()
    ]
    return bt, current, recent, bench_override


def build_one(sid: str, params: dict, enabled: bool = True) -> dict:
    """Build one extended strategy result; returns full payload (meta included)."""
    meta = EXT_STRATEGIES[sid]
    payload = {**_meta_payload(sid, enabled), "params": dict(params)}
    if not enabled:
        payload.update({"status": "disabled", "windows": {}, "current": None})
        return payload
    try:
        kind = meta["kind"]
        if kind == "vol_target":
            bt, current = _build_vol_target(sid, params)
            recent, bench_override = None, None
        elif kind == "dual_ma":
            bt, current = _build_dual_ma(sid, params)
            recent, bench_override = None, None
        elif kind == "rotation":
            bt, current, recent, bench_override = _build_rotation(sid, params)
        else:
            raise ValueError(f"未知策略类型 {kind}")
        windows = {w: _window_payload(bt, w, bench_override) for w in WINDOWS_EXT}
        payload.update({"status": "ok", "windows": windows, "current": current})
        if recent is not None:
            payload["recent"] = recent
    except Exception as e:  # noqa: BLE001 — one bad strategy must not break others
        payload.update({"status": "error", "error": str(e)[:300], "windows": {}, "current": None})
    return payload


def build_all_ext(ext_cfg: dict) -> dict:
    """Build every registered strategy with per-strategy isolation.

    ext_cfg: {"strategies": {sid: {"enabled": bool, "params": dict}}}
    Returns {"settings": merged-settings, "strategies": {sid: payload}}.
    """
    file_cfg = (ext_cfg or {}).get("strategies", {}) or {}
    settings: dict[str, dict] = {}
    results: dict[str, dict] = {}
    for sid, meta in EXT_STRATEGIES.items():
        cfg = file_cfg.get(sid, {}) or {}
        enabled = bool(cfg.get("enabled", True))
        raw_params = cfg.get("params") or {}
        try:
            params = validate_ext_params(sid, raw_params)
        except ValueError:
            params = dict(meta["default_params"])
        settings[sid] = {"enabled": enabled, "params": params}
        results[sid] = build_one(sid, params, enabled)
    return {"settings": {"strategies": settings}, "strategies": results}
