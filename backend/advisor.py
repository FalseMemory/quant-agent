"""
Trading advisor: session-aware, holdings-aware action plans.

Rules (per user requirement):
- 未开盘 / 盘中  -> 给出「今日」交易计划
- 已闭盘        -> 给出「下一交易日」计划
Sessions are evaluated in each exchange's own timezone (DST-safe via zoneinfo).
Weekends are non-trading days; exchange holidays are NOT modelled (noted in UI).
"""
from __future__ import annotations

import json
import re
import datetime as dt
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
HOLDINGS_FILE = ROOT / "holdings.json"

# minutes-from-midnight sessions, exchange-local time
MARKETS = {
    "A": {"label": "美股", "tz": ZoneInfo("America/New_York"),
          "sessions": [(9 * 60 + 25, 16 * 60 + 5)],
          "assets": ["TQQQ"]},
    "B": {"label": "A股", "tz": ZoneInfo("Asia/Shanghai"),
          "sessions": [(9 * 60 + 25, 11 * 60 + 35), (12 * 60 + 55, 15 * 60 + 5)],
          "assets": ["创业板ETF(159915)", "沪深300ETF(510300)", "红利ETF(510880)", "国债ETF(511010)"]},
    "C": {"label": "港股", "tz": ZoneInfo("Asia/Hong_Kong"),
          "sessions": [(9 * 60 + 25, 12 * 60 + 5), (12 * 60 + 55, 16 * 60 + 10)],
          "assets": ["7200.HK"]},
}

THRESHOLD = 3.0  # pct points; smaller diffs -> hold


def _is_weekend(d: dt.date) -> bool:
    return d.weekday() >= 5


def _next_trading_day(d: dt.date) -> dt.date:
    nd = d + dt.timedelta(days=1)
    while _is_weekend(nd):
        nd += dt.timedelta(days=1)
    return nd


def session_state(sid: str, now_utc: dt.datetime | None = None) -> dict:
    m = MARKETS[sid]
    now_local = (now_utc or dt.datetime.now(dt.timezone.utc)).astimezone(m["tz"])
    today = now_local.date()
    minutes = now_local.hour * 60 + now_local.minute

    if _is_weekend(today):
        nxt = _next_trading_day(today)
        return {"state": "closed", "state_cn": "休市(周末)", "plan_for": "next",
                "local_time": now_local.strftime("%m-%d %H:%M"), "today": str(today),
                "plan_date": str(nxt),
                "next_open_cst": _fmt_next_open(nxt, m)}
    first_open, last_close = m["sessions"][0][0], m["sessions"][-1][1]
    if minutes < first_open:
        return {"state": "pre_open", "state_cn": "未开盘", "plan_for": "today",
                "local_time": now_local.strftime("%m-%d %H:%M"), "today": str(today),
                "plan_date": str(today),
                "next_open_cst": _fmt_next_open(today, m)}
    if minutes <= last_close:
        return {"state": "intraday", "state_cn": "盘中", "plan_for": "today",
                "local_time": now_local.strftime("%m-%d %H:%M"), "today": str(today),
                "plan_date": str(today),
                "next_open_cst": None}
    nxt = _next_trading_day(today)
    return {"state": "closed", "state_cn": "已闭盘", "plan_for": "next",
            "local_time": now_local.strftime("%m-%d %H:%M"), "today": str(today),
            "plan_date": str(nxt),
            "next_open_cst": _fmt_next_open(nxt, m)}


def _fmt_next_open(day: dt.date, m: dict) -> str:
    """Next session open converted to Beijing time, for display."""
    open_min = m["sessions"][0][0]
    local_open = dt.datetime.combine(day, dt.time(open_min // 60, open_min % 60), tzinfo=m["tz"])
    cst = local_open.astimezone(ZoneInfo("Asia/Shanghai"))
    return f"{cst.strftime('%m-%d %H:%M')} 北京时间"


# ---------------------------------------------------------------------------
# Holdings store
# ---------------------------------------------------------------------------
# canonical ETF names MUST match backend/strategies.py ASSETS_B[*]["etf"]
DEFAULT_HOLDINGS = {
    "A": {"TQQQ": 60.0, "现金": 40.0},
    "B": {"510880 红利ETF": 70.0, "现金": 30.0},
    "C": {"7200.HK": 50.0, "现金": 50.0},
}


def load_holdings() -> dict:
    if HOLDINGS_FILE.exists():
        try:
            return json.loads(HOLDINGS_FILE.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass
    return json.loads(json.dumps(DEFAULT_HOLDINGS))  # deep copy


def save_holdings(h: dict) -> dict:
    clean = {}
    for sid in ("A", "B", "C", "WATCH"):
        raw = h.get(sid, {})
        vals = {k: max(0.0, min(100.0, float(v))) for k, v in raw.items() if k}
        total = sum(vals.values())
        if total > 0:  # normalize to 100 so inputs stay consistent
            vals = {k: round(v * 100.0 / total, 1) for k, v in vals.items()}
        clean[sid] = vals
    HOLDINGS_FILE.write_text(json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8")
    return clean


# ---------------------------------------------------------------------------
# Plan builder
# ---------------------------------------------------------------------------
def _target_a_c(exposure_pct: int, asset: str) -> dict:
    return {asset: float(exposure_pct), "现金": round(100.0 - exposure_pct, 1)}


def _diff_actions(current: dict, target: dict) -> list[dict]:
    keys = sorted(set(current) | set(target))
    actions = []
    for k in keys:
        cur = float(current.get(k, 0.0))
        tgt = float(target.get(k, 0.0))
        delta = round(tgt - cur, 1)
        if abs(delta) < THRESHOLD:
            actions.append({"action": "持有", "asset": k, "from": cur, "to": tgt,
                            "delta": delta})
        elif delta > 0:
            actions.append({"action": "买入", "asset": k, "from": cur, "to": tgt,
                            "delta": delta})
        else:
            actions.append({"action": "卖出", "asset": k, "from": cur, "to": tgt,
                            "delta": delta})
    return actions


def _needs_trade(actions: list[dict]) -> bool:
    return any(a["action"] != "持有" for a in actions)


def _asset_ref(label: str) -> dict | None:
    """Map a plan asset label (TQQQ / 159915 创业板ETF / 7200.HK / 现金)
    to a watchlist-style {code, market} ref; None for cash or unknown."""
    if "现金" in label:
        return None
    from . import watchlist as wl_mod
    try:
        m = re.search(r"\b(\d{6})\b", label)      # 159915 创业板ETF
        if m:
            return wl_mod.normalize(m.group(1), "a")
        if label.upper().endswith(".HK"):          # 7200.HK
            return wl_mod.normalize(label, "hk")
        return wl_mod.normalize(label, "us")       # TQQQ
    except wl_mod.WatchError:
        return None


def _attach_snapshots(actions: list[dict]) -> None:
    """Attach the SAME snapshot fields the watchlist table shows
    (close / chg_1d/5d/20d / vol20d / vs sma50/200 / as_of) to ETF rows."""
    from . import watchlist as wl_mod
    for a in actions:
        ref = _asset_ref(a["asset"])
        if not ref:
            continue
        try:
            snap = wl_mod.snapshot_one(ref)
            if snap:
                a["ref"] = ref
                a["snap"] = snap
        except Exception:  # noqa: BLE001  — plan must not break on quote hiccups
            pass


def build_plan(data: dict, holdings: dict | None = None) -> dict:
    """data = engine build_all() output; returns per-strategy plan."""
    holdings = holdings or load_holdings()
    plans = {}
    now_utc = dt.datetime.now(dt.timezone.utc)

    for sid in ("A", "B", "C"):
        st = data[sid]["current"]
        sess = session_state(sid, now_utc)
        m = MARKETS[sid]
        plan_title = ("今日" if sess["plan_for"] == "today" else "下一交易日") + "交易计划"
        plan_date = sess["plan_date"]

        if sid == "B":
            target = {st["etf"]: 100.0}
            rationale = f"周度轮动选中 {st['pick_name']}：{st['reason']}（决策日 {st['decision_date']}）"
        else:
            asset = "TQQQ" if sid == "A" else "7200.HK"
            target = _target_a_c(st["exposure"], asset)
            gate = "开启" if st["trend_gate_on"] else "关闭"
            rationale = (f"SMA200 闸门{gate}；实现波动 {st['realized_vol']}% / 目标 {st['target_vol']}%"
                         f"，信号日 {st['as_of']}")

        cur = {k: float(v) for k, v in holdings.get(sid, {}).items()}
        actions = _diff_actions(cur, target)
        _attach_snapshots(actions)
        trade = _needs_trade(actions)

        headline = (
            f"{'按计划执行以下调仓' if trade else '持仓与目标一致，无需操作'}"
            f"（目标：{'、'.join(f'{k} {v:.0f}%' for k, v in target.items() if v > 0)}）"
        )
        plans[sid] = {
            "market": m["label"],
            "session": sess,
            "title": plan_title,
            "plan_date": plan_date,
            "rationale": rationale,
            "target": target,
            "actions": actions,
            "headline": headline,
            "trade_needed": trade,
        }
    return {"generated_at": now_utc.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M"),
            "note": "周末自动顺延；交易所节假日未建模，节假日请勿机械执行",
            "plans": plans}
