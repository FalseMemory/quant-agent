"""
AI decision layer: LLM generates structured trading instructions from
holdings + live market snapshot + rule-strategy baselines + latest news.

Honest framing (by design):
- Rule strategies remain the backtestable risk baseline.
- The LLM output is an OVERLAY: clamped by hard guardrails, flagged when it
  deviates from the rule baseline, and every run is logged for事后复盘.
- Nothing is auto-executed; the user trades manually.
"""
from __future__ import annotations

import json
import math
import os
import re
import time
import uuid
import sqlite3
import threading
import datetime as dt
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd
import requests

from . import data_feed as dfd
from . import advisor
from .strategies import ASSETS_B

ROOT = Path(__file__).resolve().parent.parent
CFG_FILE = ROOT / "data_cache" / "ai_config.json"
HIST_FILE = ROOT / "data_cache" / "ai_decisions.json"  # legacy fallback/read compatibility
DB_FILE = ROOT / "data_cache" / "quant_agent.db"

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "Mozilla/5.0"})


class AIError(Exception):
    """Friendly, user-facing AI configuration/runtime error."""


# ---------------------------------------------------------------------------
# Config store
# ---------------------------------------------------------------------------
DEFAULT_VIEWS = ["trend", "risk", "news", "valuation", "contrarian"]
VIEW_LABELS = {"trend": "趋势与技术", "risk": "持仓与风控", "news": "新闻与事件", "valuation": "估值与基本面", "contrarian": "反向审查"}
SOURCE_OPTIONS = {
    "auto": "自动路由（推荐）", "yahoo": "Yahoo Finance", "sina": "新浪财经"
}


def load_config(profile: str | None = None) -> dict:
    if CFG_FILE.exists():
        try:
            raw = json.loads(CFG_FILE.read_text(encoding="utf-8"))
            profiles = raw.get("profiles") if isinstance(raw, dict) else None
            if isinstance(profiles, list) and profiles:
                name = profile or raw.get("active_profile") or profiles[0].get("name")
                selected = next((x for x in profiles if x.get("name") == name), profiles[0])
                return {"base_url": str(selected.get("base_url", "")).strip(),
                        "api_key": str(selected.get("api_key", "")).strip(),
                        "model": str(selected.get("model", "")).strip(),
                        "profile": str(selected.get("name", "默认模型")),
                        "group": str(selected.get("group", "默认组")),
                        "profiles": profiles}
            return {"base_url": str(raw.get("base_url", "")).strip(),
                    "api_key": str(raw.get("api_key", "")).strip(),
                    "model": str(raw.get("model", "")).strip(),
                    "profile": "默认模型", "group": "默认组", "profiles": []}
        except Exception:  # noqa: BLE001
            pass
    return {"base_url": "", "api_key": "", "model": "", "profile": "默认模型",
            "group": "默认组", "profiles": []}


def save_config(base_url: str, api_key: str, model: str,
                profile: str = "默认模型", group: str = "默认组") -> dict:
    clean = {"base_url": base_url.strip().rstrip("/"), "api_key": api_key.strip(), "model": model.strip()}
    profile = profile.strip() or "默认模型"
    group = group.strip() or "默认组"
    if clean["base_url"] and not clean["base_url"].startswith(("http://", "https://")):
        raise AIError("base_url 必须以 http:// 或 https:// 开头")
    existing = load_config()
    profiles = [x for x in existing.get("profiles", []) if x.get("name") != profile]
    profiles.insert(0, {"name": profile, "group": group, **clean})
    CFG_FILE.parent.mkdir(exist_ok=True)
    CFG_FILE.write_text(json.dumps({"active_profile": profile, "profiles": profiles}, ensure_ascii=False, indent=2), encoding="utf-8")
    return {**clean, "profile": profile, "group": group, "profiles": profiles}


def delete_config(profile: str) -> dict:
    raw: dict = {}
    if CFG_FILE.exists():
        try:
            raw = json.loads(CFG_FILE.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            raw = {}
    old = [x for x in (raw.get("profiles") or []) if isinstance(x, dict)]
    profiles = [x for x in old if x.get("name") != profile]
    if len(profiles) == len(old):
        raise AIError(f"配置「{profile}」不存在")
    active = raw.get("active_profile")
    data = {"active_profile": active if (active != profile and profiles) else (profiles[0].get("name", "") if profiles else ""),
            "profiles": profiles}
    CFG_FILE.parent.mkdir(exist_ok=True)
    CFG_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return masked_config()


def _profile_groups(profiles: list[dict]) -> list[str]:
    """Distinct group names in first-appearance order."""
    out: list[str] = []
    for p in profiles:
        g = str(p.get("group") or "默认组")
        if g not in out:
            out.append(g)
    return out


def masked_config() -> dict:
    cfg = load_config()
    key = cfg["api_key"]
    masked = (key[:6] + "*" * 8 + key[-4:]) if len(key) > 12 else ("*" * len(key) if key else "")
    return {"base_url": cfg["base_url"], "model": cfg["model"],
            "profile": cfg.get("profile", "默认模型"), "group": cfg.get("group", "默认组"),
            "profiles": [{"name": x.get("name", ""), "group": str(x.get("group") or "默认组"),
                          "model": x.get("model", ""), "base_url": x.get("base_url", "")}
                         for x in cfg.get("profiles", [])],
            "groups": _profile_groups(cfg.get("profiles", [])),
            "has_key": bool(key), "api_key_masked": masked,
            "ready": bool(cfg["base_url"] and key and cfg["model"]),
            "source_options": SOURCE_OPTIONS, "view_options": VIEW_LABELS}


# ---------------------------------------------------------------------------
# News (Sina rolling feed — free, no auth)
# Channel -> market scope so unselected markets never leak into the context.
# ---------------------------------------------------------------------------
NEWS_CHANNELS = [
    (2516, "财经", {"CN", "HK"}),
    (2517, "股市", {"CN"}),
    (2518, "美股", {"US"}),
]

# Keyword-based second pass: drop items that are clearly about a market the
# user did NOT select, even when they arrive via a generic channel.
MKT_KEYWORDS = {
    "US": ("美股", "美联储", "纳指", "纳斯达克", "标普500", "道指", "美债",
           "英伟达", "特斯拉", "微软", "Meta", "亚马逊", "美元指数", "美国股市"),
    "HK": ("港股", "恒指", "恒生指数", "港交所", "南向资金", "港元"),
    "CN": ("A股", "沪指", "深成指", "创业板", "沪深两市", "北交所",
           "央行", "证监会", "人民币汇率"),
}


def _news_market_tags(text: str) -> set[str]:
    return {mk for mk, kws in MKT_KEYWORDS.items() if any(kw in text for kw in kws)}


def filter_news_by_markets(items: list[dict], selected: set[str]) -> list[dict]:
    """Keep items with no clear market tag (generic macro) or tagged ⊆ selected."""
    out = []
    for it in items:
        tags = _news_market_tags(f"{it.get('title', '')} {it.get('intro', '')}")
        if tags and not tags.issubset(selected):
            continue
        out.append(it)
    return out


def fetch_news(hours: float = 48, per_channel: int = 18, max_total: int = 24,
               source: str = "sina", markets: list[str] | None = None) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    now = time.time()
    if source not in {"sina", "auto"}:
        return []  # 未配置新闻 API 时不伪造其他来源；由上下文记录缺失

    selected = {str(x).upper() for x in (markets or ["US", "CN", "HK"])} & {"US", "CN", "HK"}
    channels = [ch for ch in NEWS_CHANNELS if ch[2] & selected] or NEWS_CHANNELS[:1]

    for lid, label, _scope in channels:
        try:
            r = SESSION.get("https://feed.mix.sina.com.cn/api/roll/get",
                            params={"pageid": 153, "lid": lid, "num": per_channel, "page": 1},
                            timeout=15)
            r.raise_for_status()
            items = ((r.json().get("result") or {}).get("data")) or []
        except Exception:  # noqa: BLE001
            continue
        for it in items:
            title = str(it.get("title", "")).strip()
            try:
                ts = int(float(it.get("ctime", 0)))
            except Exception:  # noqa: BLE001
                continue
            if not title or title in seen or (now - ts) > hours * 3600:
                continue
            seen.add(title)
            out.append({
                "channel": label, "title": title,
                "intro": re.sub(r"\s+", " ", str(it.get("intro", "")))[:90],
                "media": str(it.get("media_name", "")),
                "time": time.strftime("%m-%d %H:%M", time.localtime(ts)),
                "_ts": ts,
            })
    out.sort(key=lambda x: -x["_ts"])
    out = filter_news_by_markets(out, selected)[:max_total]
    for x in out:
        x.pop("_ts", None)
    return out


# ---------------------------------------------------------------------------
# Market snapshot (technical state of core assets)
# ---------------------------------------------------------------------------
SNAPSHOT_ASSETS = [
    ("QQQ", "us", "纳指100(QQQ)", "美股风向标/策略A信号源"),
    ("TQQQ", "us", "TQQQ 纳指3x", "策略A交易标的"),
    ("2800.HK", "hk", "盈富基金(2800.HK)", "恒指代理/策略C信号源"),
    ("7200.HK", "hk", "恒指2x(7200.HK)", "策略C交易标的"),
    ("sz399006", "a", "创业板指", "策略B候选信号源"),
    ("sh000300", "a", "沪深300", "策略B基准信号源"),
    ("sh000015", "a", "上证红利", "策略B候选信号源"),
    ("sz159915", "a", "159915 创业板ETF", "策略B交易标的"),
    ("sh510300", "a", "510300 沪深300ETF", "策略B交易标的"),
    ("sh510880", "a", "510880 红利ETF", "策略B交易标的"),
    ("sh511010", "a", "511010 国债ETF", "策略B交易标的"),
]


def _snap(px: pd.Series) -> dict | None:
    px = px.dropna()
    if len(px) < 30:
        return None
    last = float(px.iloc[-1])

    def chg(n: int):
        return round((last / float(px.iloc[-1 - n]) - 1) * 100, 2) if len(px) > n else None

    sma50 = float(px.rolling(50).mean().iloc[-1]) if len(px) >= 50 else None
    sma200 = float(px.rolling(200).mean().iloc[-1]) if len(px) >= 200 else None
    rv20 = None
    if len(px) >= 21:
        rv20 = round(float(px.pct_change().rolling(20).std().iloc[-1] * math.sqrt(252) * 100), 1)
    return {
        "close": round(last, 2),
        "chg_1d_pct": chg(1), "chg_5d_pct": chg(5), "chg_20d_pct": chg(20),
        "vs_sma50_pct": round((last / sma50 - 1) * 100, 2) if sma50 else None,
        "vs_sma200_pct": round((last / sma200 - 1) * 100, 2) if sma200 else None,
        "realized_vol_20d_pct": rv20,
        "as_of": str(px.index[-1].date()),
    }


def market_snapshot(data_source: str = "auto") -> list[dict]:
    out = []
    for code, mkt, name, note in SNAPSHOT_ASSETS:
        try:
            # Current adapters: Yahoo covers US/HK; Sina covers CN. auto chooses by market.
            # Explicit source is recorded; unsupported market/source combinations fall back safely.
            # Provider coverage is market-aware: Yahoo for US/HK, Sina for CN.
            # Explicit incompatible choices fall back to the compatible provider rather than corrupting codes.
            use_yahoo = mkt in {"us", "hk"}
            actual_source = "yahoo" if use_yahoo else "sina"
            px = dfd.get_us(code, "1d", "2y") if use_yahoo else dfd.get_a(code, "20240101")
            s = _snap(px["close"])
        except Exception:  # noqa: BLE001
            s = None
            actual_source = "unavailable"
        if s:
            s.update({"code": code, "name": name,
                      "market": {"a": "CN", "us": "US", "hk": "HK"}.get(mkt, mkt.upper()),
                      "currency": {"a": "CNY", "us": "USD", "hk": "HKD"}.get(mkt, ""),
                      "data_source": actual_source, "note": note})
            out.append(s)
    return out


# ---------------------------------------------------------------------------
# Context assembly
# ---------------------------------------------------------------------------
KNOWN_ASSETS = {
    "A": ["TQQQ"],
    "B": [v["etf"] for v in ASSETS_B.values()],
    "C": ["7200.HK"],
}


def _market_key(market: str) -> str:
    return {"A股": "CN", "美股": "US", "港股": "HK"}.get(market, market)


def _watchlist_assets() -> list[str]:
    try:
        from . import watchlist as wl
        return [str(row["code"]) for row in wl.load_items()]
    except Exception:  # noqa: BLE001
        return []


def build_context(summary: dict, plan_payload: dict, holdings: dict | None = None,
                  markets: list[str] | None = None, data_source: str = "auto",
                  news_source: str = "sina", views: list[str] | None = None) -> dict:
    holdings = holdings or advisor.load_holdings()
    selected = [str(x).upper() for x in (markets or ["US", "CN", "HK"])]
    selected = [x for x in selected if x in {"US", "CN", "HK"}]
    if not selected:
        raise AIError("至少选择一个市场：US / CN / HK")
    sessions, rule_targets, rule_rationale = {}, {}, {}
    for sid in ("A", "B", "C"):
        p = plan_payload["plans"][sid]
        sessions[sid] = {
            "market": p["market"], "state_cn": p["session"]["state_cn"],
            "plan_for": "今日" if p["session"]["plan_for"] == "today" else "下一交易日",
            "plan_date": p["plan_date"],
            "next_open_cst": p["session"].get("next_open_cst"),
        }
        rule_targets[sid] = {k: v for k, v in p["target"].items() if k != "现金" and v > 0}
        rule_rationale[sid] = p["rationale"]

    perf = {}
    for sid in ("A", "B", "C"):
        perf[sid] = {}
        for win, x in summary[sid]["windows"].items():
            perf[sid][x["label"]] = {
                "strategy_ret_pct": x["strategy"]["total_return"],
                "strategy_maxdd_pct": x["strategy"]["max_dd"],
                "bench_ret_pct": x["benchmark"]["total_return"],
                "beats_bench": x["beats_benchmark"],
            }

    effective_source = data_source if data_source in SOURCE_OPTIONS else "auto"
    snaps = market_snapshot(effective_source)
    watch_assets: list[str] = []
    watch_holdings = holdings.get("WATCH", {}) if isinstance(holdings, dict) else {}
    try:  # user watchlist becomes an explicit, tradeable analysis pool
        from . import watchlist as wl
        for row in wl.snapshots():
            market = {"a": "CN", "us": "US", "hk": "HK"}[row["market"]]
            if market not in selected:
                continue
            code = row["code"]
            watch_assets.append(code)
            entry = {"name": row.get("name") or code, "code": code,
                     "market": market, "strategy": "WATCH",
                     "currency": {"CN": "CNY", "US": "USD", "HK": "HKD"}[market],
                     "holding_pct": float(watch_holdings.get(code, 0.0)),
                     "note": "用户自选·可分析持仓卖出/减仓/持有"}
            s = row.get("snap")
            if s:
                entry.update(s)
            else:
                entry["unavailable"] = row.get("error", "无数据")
            snaps.append(entry)
    except Exception:  # noqa: BLE001
        pass

    # Restrict strategy pools and sessions to the user's selected markets.
    selected_sids = {"US": "A", "CN": "B", "HK": "C"}
    sessions = {sid: value for sid, value in sessions.items()
                if _market_key(value["market"]) in selected}
    rule_targets = {sid: value for sid, value in rule_targets.items() if sid in sessions}
    rule_rationale = {sid: value for sid, value in rule_rationale.items() if sid in sessions}
    perf = {sid: value for sid, value in perf.items() if sid in sessions}
    known = {sid: value for sid, value in KNOWN_ASSETS.items() if sid in sessions}
    known["WATCH"] = watch_assets
    selected_views = [v for v in (views or DEFAULT_VIEWS) if v in VIEW_LABELS]
    news = fetch_news(source=news_source, markets=selected)
    return {
        "now_cst": dt.datetime.now(dt.timezone.utc).astimezone(
            dt.timezone(dt.timedelta(hours=8))).strftime("%Y-%m-%d %H:%M %A"),
        "selected_markets": selected,
        "analysis_config": {"data_source": effective_source,
                            "data_source_label": SOURCE_OPTIONS[effective_source],
                            "news_source": news_source if news_source in {"sina", "auto"} else "sina",
                            "views": selected_views, "view_labels": [VIEW_LABELS[v] for v in selected_views]},
        "sessions": sessions,
        "holdings_pct": holdings,
        "rule_baseline_targets_pct": rule_targets,
        "rule_baseline_rationale": rule_rationale,
        "rule_backtest_performance": perf,
        "known_assets": known,
        "market_snapshot": [s for s in snaps if s.get("market") in selected],
        "news_recent": news,
    }


# ---------------------------------------------------------------------------
# Prompt + LLM call (OpenAI-compatible endpoint)
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """你是一名严谨的中低频量化交易决策助手，服务一位上班族个人投资者。
框架：三个规则策略构成风险基线——A 美股TQQQ波动率目标+SMA200闸门、B A股周度动量轮动、C 港股恒指2x波动率目标。全部日级、收盘出信号次日开盘附近执行，不盯盘。

你的任务：只分析 selected_markets 中的市场，综合【用户持仓】【市场技术快照】【规则基线目标与回测表现】【最新新闻】，输出本次执行日的具体仓位指令。analysis_config 中的 views 是本次用户要求的分析视角，必须分别覆盖；data_source/news_source 是本次数据来源口径，禁止声称使用未提供的数据。

市场隔离（最高优先级）：selected_markets 之外的市场一律视为不存在。输出中的任何字段——包括 market_view 的键、decisions、reason、deviation_note、risk_notes——都不得出现未选市场的名称、指数或行情解读；也不要输出"XX 市场未纳入本次范围"之类的解释性文字。若新闻里出现未选市场相关的宏观消息，忽略之；只有当其影响已经体现在所选市场的数据中时，才可基于所选市场自身数据做判断。

硬性约束：
1. asset 必须从 known_assets 对应列表中选取（现金不用输出，自动=100-合计）。WATCH 是用户自选池，允许对自选股票/ETF 给出 buy|sell|hold，并优先结合 holdings_pct 判断是否应卖出或减仓。
2. 每个策略的 target_pct 合计不得超过 100；单个资产 0~100。
3. 相对规则基线偏离尽量不超过 ±30 个百分点；确有重大风险事件理由时可突破，但须在 deviation_note 说明。
4. 新闻仅作辅助判断：区分噪音与实质影响（如利率决议、地缘冲突、行业监管），个股新闻对指数级ETF影响有限。
5. 局势不明朗时倾向维持现状(action=hold)。禁止臆造数据中不存在的标的或事件。
6. 买入或卖出默认使用 price_mode="market_open"，表示下一交易日开盘价附近执行；不要预测或伪造尚未产生的未来开盘价。只有输入行情足以支持明确限价逻辑时才使用 price_mode="limit"，并填写 limit_price 或 limit_low/limit_high。hold 使用 not_set。
7. 可填写 price_condition 说明触发/失效条件。最终参考收盘价、行情日期和币种由系统从 market_snapshot 自动补齐，不需要用户或模型重复填写。
8. 只输出一个 JSON 对象，不要任何多余文本。

输出 schema：
{"market_view": {"<仅填所选市场的键，如 US/CN/HK>": "一句话"},
 "decisions": [{"strategy": "A|B|C|WATCH", "asset": "名称或代码", "target_pct": 数字,
                "action": "buy|sell|hold", "reason": "一句话",
                "deviation_note": "与基线差异说明",
                "price_mode": "market_open|limit|not_set",
                "limit_price": 数字或null, "limit_low": 数字或null, "limit_high": 数字或null,
                "price_reference": "价格依据", "price_as_of": "YYYY-MM-DD或空",
                "price_condition": "价格触发/失效条件"}],
 "risk_notes": "主要风险提示",
 "confidence": "low|medium|high"}"""


USER_TMPL = """今天是 {now}。各市场执行安排：{sess_desc}

请基于以下 JSON 数据输出本次交易指令 JSON：

{ctx_json}"""


def _build_messages(ctx: dict) -> list[dict]:
    sess_parts = []
    for sid, s in ctx["sessions"].items():
        sess_parts.append(f"{sid}{s['market']}[{s['state_cn']}]→{s['plan_for']}({s['plan_date']})")
    user = USER_TMPL.format(now=ctx["now_cst"], sess_desc="；".join(sess_parts),
                            ctx_json=json.dumps(ctx, ensure_ascii=False))
    return [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}]


def call_llm(cfg: dict, messages: list[dict], timeout: int = 180) -> str:
    url = cfg["base_url"].rstrip("/") + "/chat/completions"
    # NOTE: deliberately NO max_tokens here. Thinking/reasoning models burn the
    # budget on hidden reasoning and return EMPTY content when exhausted —
    # that was the original "未返回 JSON：" empty-text bug.
    payload = {"model": cfg["model"], "messages": messages, "temperature": 0.2}
    try:
        r = requests.post(url, headers={"Authorization": f"Bearer {cfg['api_key']}"},
                          json=payload, timeout=timeout)
    except requests.RequestException as e:
        raise AIError(f"无法连接 LLM 服务：{e}") from e
    if r.status_code == 401:
        raise AIError("API Key 无效或已过期（401）")
    if r.status_code == 404:
        raise AIError("接口不存在（404）：检查 base_url 是否需以 /v1 结尾、模型名是否正确")
    if r.status_code == 429:
        raise AIError("LLM 服务限流或余额不足（429），请稍后再试")
    if r.status_code >= 400:
        raise AIError(f"LLM 服务返回 {r.status_code}：{r.text[:200]}")

    try:
        data = r.json()
    except Exception as e:  # noqa: BLE001
        raise AIError(f"LLM 响应不是 JSON：{r.text[:200]}") from e
    if isinstance(data, dict) and data.get("error"):
        err = data["error"]
        msg_txt = err.get("message") if isinstance(err, dict) else str(err)
        raise AIError(f"LLM 返回错误：{str(msg_txt)[:250]}")

    choices = data.get("choices") or []
    if not choices:
        raise AIError(f"LLM 响应缺少 choices：{json.dumps(data, ensure_ascii=False)[:200]}")
    choice0 = choices[0]
    msg = choice0.get("message") or {}
    content = str(msg.get("content") or "").strip()
    if content:
        return content

    # ---- empty content: diagnose + salvage ----
    finish = choice0.get("finish_reason")
    reasoning = str(msg.get("reasoning_content") or msg.get("reasoning") or "").strip()
    if reasoning:
        try:
            obj = extract_json(reasoning)  # thinking trace may still hold the JSON
        except Exception:  # noqa: BLE001
            obj = None
        if obj is not None:
            return json.dumps(obj, ensure_ascii=False)
        hint = "该模型是思考型(reasoning)：思考耗尽了输出预算但没给出最终答案"
    else:
        hint = "模型返回了空内容"
    if finish == "length":
        hint += "；finish_reason=length（输出被截断）"
    hint += "。建议换非思考型模型（如 deepseek-chat / qwen-turbo / gpt-4o-mini），或检查服务商用量限制"
    raise AIError(hint)


def extract_json(text: str) -> dict:
    text = text.strip()
    text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    i, j = text.find("{"), text.rfind("}")
    if i < 0 or j <= i:
        raise AIError(f"LLM 未返回 JSON：{text[:150]}")
    try:
        return json.loads(text[i:j + 1])
    except json.JSONDecodeError as e:
        raise AIError(f"JSON 解析失败：{e}｜原文片段：{text[i:i + 150]}") from e


# ---------------------------------------------------------------------------
# Guardrails + decide pipeline
# ---------------------------------------------------------------------------
MAX_TOTAL_PCT = 100.0
SOFT_DEVIATION_PCT = 30.0


def apply_guardrails(parsed: dict, rule_targets: dict, known_assets: dict | None = None) -> tuple[list[dict], list[str]]:
    warnings: list[str] = []
    decisions: list[dict] = []
    known_assets = known_assets or KNOWN_ASSETS
    raw_list = parsed.get("decisions") or []
    if not isinstance(raw_list, list):
        raise AIError("decisions 字段不是数组")

    for d in raw_list:
        sid = str(d.get("strategy", "")).strip().upper()
        asset = str(d.get("asset", "")).strip()
        if sid not in ("A", "B", "C", "WATCH"):
            warnings.append(f"丢弃未知策略条目: {d}")
            continue
        if asset and asset not in known_assets.get(sid, []):
            warnings.append(f"{sid}: 未知资产「{asset}」已丢弃（必须是 {known_assets.get(sid, [])} 之一）")
            continue
        try:
            pct = max(0.0, min(MAX_TOTAL_PCT, float(d.get("target_pct", 0))))
        except Exception:  # noqa: BLE001
            pct = 0.0
        dev_note = str(d.get("deviation_note", "")).strip()
        base = float(rule_targets.get(sid, {}).get(asset, 0.0))
        flag = ""
        if abs(pct - base) > SOFT_DEVIATION_PCT:
            flag = f"偏离基线 {round(pct - base):+d}pct"
        action = str(d.get("action", "hold")).lower().strip()[:6]
        if action not in {"buy", "sell", "hold"}:
            warnings.append(f"{sid}/{asset}: 未知 action「{action}」已按 hold 处理")
            action = "hold"
        mode = str(d.get("price_mode", "not_set")).lower().strip()
        if mode not in {"market_open", "limit", "not_set"}:
            warnings.append(f"{sid}/{asset}: 未知 price_mode「{mode}」已按 not_set 处理")
            mode = "not_set"
        if action == "hold":
            mode = "not_set"
        def _price(key: str):
            try:
                value = d.get(key)
                return round(float(value), 4) if value is not None and str(value).strip() else None
            except Exception:  # noqa: BLE001
                return None
        limit_price = _price("limit_price")
        limit_low = _price("limit_low")
        limit_high = _price("limit_high")
        if mode == "limit" and not any(x is not None for x in (limit_price, limit_low, limit_high)):
            warnings.append(f"{sid}/{asset}: 限价模式缺少价格，已自动回退为下一交易日开盘执行")
            mode = "market_open"
        if action in {"buy", "sell"} and mode == "not_set":
            mode = "market_open"
        decisions.append({
            "strategy": sid, "asset": asset, "target_pct": round(pct, 1),
            "action": action, "reason": str(d.get("reason", "")).strip(),
            "baseline_pct": base, "deviation_flag": flag, "deviation_note": dev_note,
            "price_mode": mode, "limit_price": limit_price,
            "limit_low": limit_low, "limit_high": limit_high,
            "price_reference": str(d.get("price_reference", "")).strip(),
            "price_as_of": str(d.get("price_as_of", "")).strip(),
            "price_condition": str(d.get("price_condition", "")).strip(),
        })

    # per-strategy total cap
    by_sid: dict[str, float] = {}
    for dec in decisions:
        by_sid[dec["strategy"]] = by_sid.get(dec["strategy"], 0.0) + dec["target_pct"]
    for sid, total in by_sid.items():
        if total > MAX_TOTAL_PCT + 1e-6:
            factor = MAX_TOTAL_PCT / total
            for dec in decisions:
                if dec["strategy"] == sid:
                    dec["target_pct"] = round(dec["target_pct"] * factor, 1)
            warnings.append(f"策略{sid} 目标合计 {total:.0f}% 超100%，已等比压缩至 100%")

    # Missing selected strategy entries are intentionally allowed. The caller
    # records the selected markets so an omitted market is distinguishable from
    # a user intentionally not requesting it.
    return decisions, warnings


def enrich_execution_prices(decisions: list[dict], ctx: dict) -> list[dict]:
    """Attach authoritative snapshot references; future open remains intentionally unknown."""
    lookup: dict[str, dict] = {}
    for snap in ctx.get("market_snapshot", []):
        for key in (snap.get("code"), snap.get("name")):
            if key:
                lookup[str(key)] = snap
    for dec in decisions:
        if dec.get("action") == "hold":
            dec.update({"price_mode": "not_set", "reference_close": None,
                        "price_reference": "持有，无需执行价格"})
            continue
        snap = lookup.get(str(dec.get("asset", "")))
        if snap and snap.get("close") is not None:
            dec["reference_close"] = float(snap["close"])
            dec["price_as_of"] = str(snap.get("as_of", ""))
            dec["currency"] = str(snap.get("currency", ""))
            dec["price_reference"] = "项目行情API最近已完成交易日收盘价，仅作信号参考"
            dec["reference_status"] = "available"
        else:
            dec["reference_close"] = None
            dec["currency"] = ""
            dec["price_reference"] = "项目行情API未取得该标的参考价；无需手动填写"
            dec["reference_status"] = "unavailable"
        if dec.get("price_mode") == "market_open":
            dec["execution_price"] = None
            dec["execution_note"] = "下一交易日真实开盘价执行，开盘前无法预知具体成交价"
        elif dec.get("price_mode") == "limit":
            dec["execution_price"] = dec.get("limit_price")
            dec["execution_note"] = "按建议限价或限价区间委托，是否成交取决于实际行情"
    return decisions


def _resolve_profile_cfg(profile_name: str | None) -> dict:
    cfg = load_config(profile_name)
    if not (cfg["base_url"] and cfg["api_key"] and cfg["model"]):
        raise AIError(f"模型配置「{profile_name or '默认'}」不完整：请在「AI 交易指令 → 设置」补全 base_url / API Key / 模型名")
    return cfg


def _run_llm_pipeline(ctx: dict, cfg: dict) -> dict:
    """One LLM decision against a prepared context; persists its own audit row."""
    messages = _build_messages(ctx)
    t0 = time.time()
    raw = call_llm(cfg, messages)
    parsed = extract_json(raw)

    mv = parsed.get("market_view") if isinstance(parsed.get("market_view"), dict) else {}
    decisions, warnings = apply_guardrails(
        parsed, ctx["rule_baseline_targets_pct"], ctx["known_assets"])
    decisions = enrich_execution_prices(decisions, ctx)

    result = {
        "ok": True,
        "decision_id": uuid.uuid4().hex,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "selected_markets": ctx["selected_markets"],
        "analysis_config": ctx.get("analysis_config", {}),
        "model_profile": cfg.get("profile", "默认模型"),
        "elapsed_s": round(time.time() - t0, 1),
        "model": cfg["model"],
        "confidence": str(parsed.get("confidence", "n/a")),
        "market_view": {k: str(v)[:300] for k, v in mv.items()},
        "decisions": decisions,
        "risk_notes": str(parsed.get("risk_notes", "")).strip(),
        "warnings": warnings,
        "sessions": ctx["sessions"],
        "news_count": len(ctx["news_recent"]),
    }
    append_history(result, context=ctx, raw_response=raw, parsed=parsed)
    return result


def decide(summary: dict, plan_payload: dict, holdings: dict | None = None,
           markets: list[str] | None = None, model_profile: str | None = None,
           data_source: str = "auto", news_source: str = "sina",
           views: list[str] | None = None) -> dict:
    cfg = _resolve_profile_cfg(model_profile)
    ctx = build_context(summary, plan_payload, holdings, markets=markets,
                        data_source=data_source, news_source=news_source, views=views)
    return _run_llm_pipeline(ctx, cfg)


def _expand_groups(model_groups: list[str] | None) -> list[str]:
    """Map selected group names to profile names (first-appearance order)."""
    wanted = {str(g).strip() for g in (model_groups or []) if str(g).strip()}
    if not wanted:
        return []
    out: list[str] = []
    for p in load_config().get("profiles", []):
        if str(p.get("group") or "默认组") in wanted and p.get("name") not in out:
            out.append(str(p.get("name", "")))
    return out


def decide_multi(summary: dict, plan_payload: dict, holdings: dict | None = None,
                 markets: list[str] | None = None, profiles: list[str] | None = None,
                 model_groups: list[str] | None = None,
                 data_source: str = "auto", news_source: str = "sina",
                 views: list[str] | None = None) -> dict:
    """Run the SAME context through several saved model profiles.

    Profiles may be picked explicitly (`profiles`) and/or by 配置组
    (`model_groups` — every saved profile in those groups runs). Context
    (market data / news / baselines) is fetched once and shared, so output
    differences come from the models, not data drift. Each run gets its own
    decision_id and audit row; one failing profile does not abort the others.
    """
    names: list[str] = _expand_groups(model_groups)
    for x in (profiles or []):
        n = str(x).strip()
        if n and n not in names:
            names.append(n)
    if not names:
        raise AIError("未选择任何模型或配置组")

    ctx = build_context(summary, plan_payload, holdings, markets=markets,
                        data_source=data_source, news_source=news_source, views=views)

    # Parallel execution: the shared context is read-only after build and
    # call_llm issues an independent HTTP request per profile, so profiles
    # have no data dependency. Workers are capped (default 4) to stay friendly
    # to upstream rate limits when one API key serves many models.
    try:
        workers = max(1, min(len(names), int(os.environ.get("QUANT_MULTI_WORKERS", "4"))))
    except ValueError:
        workers = min(len(names), 4)

    slots_results: list[dict | None] = [None] * len(names)
    slots_errors: list[dict | None] = [None] * len(names)

    def _run_slot(i: int, name: str) -> None:
        try:
            cfg = _resolve_profile_cfg(name)
            slots_results[i] = _run_llm_pipeline(ctx, cfg)
        except AIError as e:
            slots_errors[i] = {"profile": name, "error": str(e)}
        except Exception as e:  # noqa: BLE001
            slots_errors[i] = {"profile": name, "error": f"生成失败：{str(e)[:200]}"}

    if workers <= 1:
        for i, name in enumerate(names):
            _run_slot(i, name)
    else:
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="llm") as ex:
            futures = [ex.submit(_run_slot, i, n) for i, n in enumerate(names)]
            for f in futures:
                f.result()  # exceptions are captured inside _run_slot

    results = [r for r in slots_results if r is not None]
    errors = [e for e in slots_errors if e is not None]
    # preserve selection order for display
    order = {n: i for i, n in enumerate(names)}
    results.sort(key=lambda r: order.get(r.get("model_profile", ""), 0))

    if not results:
        raise AIError(errors[0]["error"] if errors else "全部模型配置均失败")
    return {"ok": True, "multi": True,
            "selected_markets": ctx["selected_markets"],
            "analysis_config": ctx.get("analysis_config", {}),
            "parallel": workers > 1, "workers": workers,
            "results": results, "errors": errors}


# ---------------------------------------------------------------------------
# Durable decision audit store
# ---------------------------------------------------------------------------
def _db_conn() -> sqlite3.Connection:
    DB_FILE.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ai_decisions (
            decision_id TEXT PRIMARY KEY,
            generated_at TEXT NOT NULL,
            model TEXT,
            selected_markets TEXT NOT NULL,
            confidence TEXT,
            request_context TEXT NOT NULL,
            raw_response TEXT NOT NULL,
            parsed_response TEXT NOT NULL,
            result_json TEXT NOT NULL,
            warnings_json TEXT NOT NULL,
            created_ts REAL NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ai_decisions_time ON ai_decisions(created_ts DESC)")
    conn.commit()
    return conn


_AUDIT_WRITE_LOCK = threading.Lock()   # parallel model runs share one SQLite file


def append_history(result: dict, context: dict | None = None,
                   raw_response: str = "", parsed: dict | None = None) -> None:
    """Persist a complete audit bundle; JSON history remains a legacy fallback."""
    try:
        with _AUDIT_WRITE_LOCK:
            conn = _db_conn()
            conn.execute(
                """INSERT OR REPLACE INTO ai_decisions
                (decision_id, generated_at, model, selected_markets, confidence,
                 request_context, raw_response, parsed_response, result_json,
                 warnings_json, created_ts)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (result["decision_id"], result["generated_at"], result.get("model", ""),
                 json.dumps(result.get("selected_markets", []), ensure_ascii=False),
                 result.get("confidence", ""), json.dumps(context or {}, ensure_ascii=False),
                 raw_response, json.dumps(parsed or {}, ensure_ascii=False),
                 json.dumps(result, ensure_ascii=False),
                 json.dumps(result.get("warnings", []), ensure_ascii=False), time.time()),
            )
            conn.commit()
            conn.close()
    except Exception:  # noqa: BLE001
        pass


def load_history(limit: int = 50) -> list[dict]:
    try:
        conn = _db_conn()
        rows = conn.execute(
            "SELECT result_json FROM ai_decisions ORDER BY created_ts DESC LIMIT ?",
            (max(1, min(int(limit), 500)),)).fetchall()
        conn.close()
        return [json.loads(row["result_json"]) for row in rows]
    except Exception:  # noqa: BLE001
        pass
    if HIST_FILE.exists():
        try:
            return json.loads(HIST_FILE.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass
    return []


def get_history_detail(decision_id: str) -> dict | None:
    conn = _db_conn()
    row = conn.execute("SELECT * FROM ai_decisions WHERE decision_id = ?", (decision_id,)).fetchone()
    conn.close()
    if not row:
        return None
    item = dict(row)
    for key in ("selected_markets", "request_context", "parsed_response", "result_json", "warnings_json"):
        try:
            item[key] = json.loads(item[key])
        except Exception:  # noqa: BLE001
            pass
    return item
