/* Quant Agent dashboard logic — daily-only, 3 strategies, plan section */
"use strict";

const DATA = { A: null, B: null, C: null };
let PLAN = null;
const state = {
  A: { window: "full", charts: {} },
  B: { window: "full", charts: {} },
  C: { window: "full", charts: {} },
};
const WINDOWS = [
  ["full", "全历史"], ["3y", "近3年"], ["1y", "近1年"], ["since_jun", "2026-06以来"],
];
const HOLDING_KEYS = {
  A: ["TQQQ", "现金"],
  B: ["159915 创业板ETF", "510300 沪深300ETF", "510880 红利ETF", "511010 国债ETF", "现金"],
  C: ["7200.HK", "现金"],
};

function toast(msg) {
  const t = document.getElementById("toast");
  t.textContent = msg; t.classList.add("show");
  setTimeout(() => t.classList.remove("show"), 2200);
}
function cls(v) { return v > 0 ? "pos" : v < 0 ? "neg" : "flat"; }
function fmt(v, suffix = "") {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  return `${v > 0 ? "+" : ""}${v}${suffix}`;
}

async function loadData(force = false) {
  try {
    const r = await fetch(force ? "/api/summary?force=true" : "/api/summary");
    const j = await r.json();
    if (!j.ok) throw new Error(j.error || "unknown");
    DATA.A = j.A; DATA.B = j.B; DATA.C = j.C;
    restoreSettings(j.settings);
    document.getElementById("updated").textContent =
      `数据截至 ${DATA.A.current.as_of} (美) / ${DATA.B.current.as_of} (A) / ${DATA.C.current.as_of} (港)`;
    renderAll();
    loadPlan();
  } catch (e) {
    toast("加载失败: " + e.message);
  }
}

/* ---------- strategy sections ---------- */
function toggleDoc(sid) {
  const doc = document.getElementById("doc" + sid);
  const btn = document.getElementById("docBtn" + sid);
  const open = doc.classList.toggle("open");
  btn.classList.toggle("on", open);
}

function renderAll() { ["A", "B", "C"].forEach(renderStrategy); }

function renderStrategy(sid) {
  const d = DATA[sid], st = state[sid];
  const chip = document.getElementById("chip" + sid);
  const c = d.current;
  if (sid === "B") {
    chip.textContent = `持有 ${c.pick_name} (${c.etf})`;
    chip.className = "chip up";
  } else {
    chip.textContent = `仓位 ${c.exposure}% · 波动 ${c.realized_vol}% · 闸门${c.trend_gate_on ? "开" : "关"}`;
    chip.className = "chip " + (c.exposure >= 60 ? "up" : c.exposure <= 5 ? "down" : "warn");
  }
  const tabs = document.getElementById("tabs" + sid);
  tabs.innerHTML = "";
  WINDOWS.forEach(([w, label]) => {
    const b = document.createElement("button");
    b.textContent = label;
    b.className = st.window === w ? "on" : "";
    b.onclick = () => { st.window = w; renderStrategy(sid); };
    tabs.appendChild(b);
  });

  const wp = d.windows[st.window] || Object.values(d.windows)[0];
  if (!wp) { document.getElementById("body" + sid).innerHTML = "<p class='sub'>该窗口暂无数据</p>"; return; }
  const S = wp.strategy, Bm = wp.benchmark;
  document.getElementById("body" + sid).innerHTML = `
    <div class="metrics">
      <div class="m"><div class="k">策略收益</div><div class="v ${cls(S.total_return)}">${fmt(S.total_return, "%")}</div><div class="s">基准 ${fmt(Bm.total_return, "%")} · ${wp.beats_benchmark ? "跑赢 ✓" : "未跑赢 ✗"}</div></div>
      <div class="m"><div class="k">策略年化</div><div class="v">${fmt(S.cagr, "%")}</div><div class="s">基准 ${fmt(Bm.cagr, "%")}</div></div>
      <div class="m"><div class="k">最大回撤</div><div class="v neg">${fmt(S.max_dd, "%")}</div><div class="s">基准 ${fmt(Bm.max_dd, "%")}</div></div>
      <div class="m"><div class="k">Sharpe</div><div class="v">${fmt(S.sharpe)}</div><div class="s">基准 ${fmt(Bm.sharpe)}</div></div>
      <div class="m"><div class="k">窗口</div><div class="v" style="font-size:14px;line-height:30px">${wp.start} → ${wp.end}</div></div>
    </div>
    <div class="charts">
      <div class="chartbox"><h3>净值曲线（起点=1，红=策略，灰=基准）</h3><canvas id="eq_${sid}"></canvas></div>
      <div class="chartbox"><h3>策略回撤 %</h3><canvas id="dd_${sid}"></canvas></div>
    </div>
    <div class="side">
      <div class="panelbox" id="extra_${sid}"></div>
      <div class="panelbox" id="cur_${sid}"></div>
    </div>`;
  drawCharts(sid, wp);
  if (sid === "B") renderExtraB(d); else renderExtraVol(d, sid);
  renderCurrent(sid, d);
}

const chartColors = () => ({ txt: "#9a9890", grid: "rgba(255,255,255,.06)", red: "#e05252", gray: "#77756e", green: "#35b881" });
let eqChart = {}, ddChart = {};
function drawCharts(sid, wp) {
  const C = chartColors();
  const labels = wp.dates;
  if (eqChart[sid]) eqChart[sid].destroy();
  if (ddChart[sid]) ddChart[sid].destroy();
  eqChart[sid] = new Chart(document.getElementById(`eq_${sid}`), {
    type: "line",
    data: { labels, datasets: [
      { data: wp.equity, borderColor: C.red, borderWidth: 1.6, pointRadius: 0, tension: .1 },
      { data: wp.bench_equity, borderColor: C.gray, borderWidth: 1.2, pointRadius: 0, tension: .1 },
    ]},
    options: {
      maintainAspectRatio: false,
      plugins: { legend: { display: false }, tooltip: { mode: "index", intersect: false } },
      scales: {
        x: { ticks: { color: C.txt, maxTicksLimit: 8, font: { size: 10 } }, grid: { display: false } },
        y: { ticks: { color: C.txt, font: { size: 10 } }, grid: { color: C.grid } },
      },
    },
  });
  ddChart[sid] = new Chart(document.getElementById(`dd_${sid}`), {
    type: "line",
    data: { labels, datasets: [{ data: wp.drawdown, borderColor: C.green, backgroundColor: "rgba(53,184,129,.12)", fill: true, borderWidth: 1.4, pointRadius: 0 }]},
    options: {
      maintainAspectRatio: false,
      plugins: { legend: { display: false } },
      scales: {
        x: { ticks: { color: C.txt, maxTicksLimit: 8, font: { size: 10 } }, grid: { display: false } },
        y: { ticks: { color: C.txt, font: { size: 10 }, callback: v => v + "%" }, grid: { color: C.grid }, max: 0 },
      },
    },
  });
}

function renderExtraVol(d, sid) {
  const el = document.getElementById("extra_" + sid);
  let rows = `<h3>${sid === "C" ? "港股" : "美股"}·波动率目标机制说明</h3><table>` +
    `<tr><th>项目</th><th>状态</th></tr>` +
    `<tr><td>SMA200 趋势闸门</td><td>${d.current.trend_gate_on ? "开启（允许持仓）" : "关闭（强制空仓）"}</td></tr>` +
    `<tr><td>信号源实现波动(20日)</td><td>${d.current.realized_vol}% / 目标 ${d.current.target_vol}%</td></tr>` +
    `<tr><td>目标仓位</td><td>${d.current.exposure}%</td></tr>` +
    `<tr><td>执行方式</td><td>收盘出信号，次日开盘附近执行；无需盯盘</td></tr></table>`;
  el.innerHTML = rows;
}
function renderExtraB(d) {
  const el = document.getElementById("extra_B");
  let rows = `<h3>近期轮动记录（每周决策）</h3><table><tr><th>日期</th><th>选中</th><th>原因</th></tr>`;
  for (const p of d.recent_picks) rows += `<tr><td>${p.date}</td><td>${p.pick}</td><td style="color:var(--muted)">${p.reason}</td></tr>`;
  el.innerHTML = rows + "</table>";
}
function renderCurrent(sid, d) {
  const el = document.getElementById("cur_" + sid);
  const c = d.current;
  if (sid === "B") {
    el.innerHTML = `<h3>本周持仓</h3>
      <div class="kv"><span class="k">选中资产</span><span>${c.pick_name}</span></div>
      <div class="kv"><span class="k">对应ETF</span><span>${c.etf}</span></div>
      <div class="kv"><span class="k">入选原因</span><span>${c.reason}</span></div>
      <div class="kv"><span class="k">决策日</span><span>${c.decision_date}</span></div>`;
  } else {
    el.innerHTML = `<h3>最新信号</h3>
      <div class="kv"><span class="k">目标仓位</span><span>${c.exposure}%</span></div>
      <div class="kv"><span class="k">实现波动</span><span>${c.realized_vol}%</span></div>
      <div class="kv"><span class="k">趋势闸门</span><span>${c.trend_gate_on ? "开启" : "关闭"}</span></div>
      <div class="kv"><span class="k">信号日</span><span>${c.as_of}</span></div>`;
  }
}

/* ---------- trading plan ---------- */
async function loadPlan() {
  try {
    const [pr, hr] = await Promise.all([fetch("/api/plan"), fetch("/api/holdings")]);
    const pj = await pr.json(), hj = await hr.json();
    if (!pj.ok) throw new Error(pj.error || "plan failed");
    PLAN = pj;
    PLAN.savedHoldings = hj.holdings;
    renderPlan();
  } catch (e) { toast("计划加载失败: " + e.message); }
}

function sessionChip(s) {
  const map = { intraday: ["盘中", "up"], pre_open: ["未开盘", "info"], closed: ["已闭盘", "down"] };
  const [txt, clsx] = map[s.state] || [s.state_cn, ""];
  return `<span class="chip ${clsx}">${s.state_cn || txt} · 当地 ${s.local_time.split(" ")[1]}</span>`;
}

/* ---------- trading plan: market tabs ---------- */
let PLAN_TAB = "A";   // active plan tab; panels stay mounted so inputs/scroll survive switching

function splitAssetLabel(label) {
  const i = label.search(/[\u4e00-\u9fff]/);
  if (i > 0) return { code: label.slice(0, i).trim(), name: label.slice(i).trim() };
  return { code: label.trim(), name: label.trim() };
}

function etfDetailTable(a) {
  if (!a.snap || !a.ref) return "";
  const s = a.snap;
  return `<div class="tblwrap"><table>
    <tr><th>代码</th><th>名称</th><th>现价</th><th>1日</th><th>5日</th><th>20日</th><th>波动20d</th><th>vs SMA50</th><th>vs SMA200</th><th>截至</th></tr>
    <tr>
      <td>${esc(a.ref.code)}</td>
      <td>${esc(splitAssetLabel(a.asset).name)}</td>
      <td>${esc(s.close)}</td>
      <td class="${cls(s.chg_1d_pct)}">${fmt(s.chg_1d_pct, "%")}</td>
      <td class="${cls(s.chg_5d_pct)}">${fmt(s.chg_5d_pct, "%")}</td>
      <td class="${cls(s.chg_20d_pct)}">${fmt(s.chg_20d_pct, "%")}</td>
      <td>${s.realized_vol_20d_pct ?? "—"}</td>
      <td class="${cls(s.vs_sma50_pct)}">${fmt(s.vs_sma50_pct, "%")}</td>
      <td class="${cls(s.vs_sma200_pct)}">${fmt(s.vs_sma200_pct, "%")}</td>
      <td style="color:var(--dim)">${esc(s.as_of)}</td>
    </tr></table></div>`;
}

function renderPlan() {
  const sids = ["A", "B", "C"];
  if (!sids.includes(PLAN_TAB)) PLAN_TAB = "A";
  const tabs = document.getElementById("planTabs");
  tabs.innerHTML = sids.map(sid => {
    const p = PLAN.plans[sid];
    return `<button class="ptab${PLAN_TAB === sid ? " on" : ""}" data-sid="${sid}"
      onclick="showPlanTab('${sid}')">${sid} · ${p.market}` +
      `${p.trade_needed ? '<span class="dot" title="该市场有调仓动作"></span>' : ""}</button>`;
  }).join("");
  // Panels are rebuilt ONLY when plan data changes; tab switches just toggle hidden,
  // so holdings inputs / scroll positions of every market survive switching.
  document.getElementById("planPanels").innerHTML =
    sids.map(sid => {
      const p = PLAN.plans[sid];
      return `<div class="plan-panel plancard${p.trade_needed ? " trade" : ""}" data-sid="${sid}"${PLAN_TAB === sid ? "" : " hidden"}>${planCardHtml(sid)}</div>`;
    }).join("");
}

function showPlanTab(sid) {
  PLAN_TAB = sid;
  document.querySelectorAll("#planTabs .ptab").forEach(b =>
    b.classList.toggle("on", b.dataset.sid === sid));
  document.querySelectorAll("#planPanels .plan-panel").forEach(pn =>
    pn.hidden = pn.dataset.sid !== sid);
}

function planCardHtml(sid) {
  const p = PLAN.plans[sid];
  const acts = p.actions.map(a => `
    ${etfDetailTable(a)}
    <div class="act ${a.action === "买入" ? "buy" : a.action === "卖出" ? "sell" : "hold"}">
      <span>${esc(a.asset)}</span>
      <span style="color:var(--muted);font-size:12px">${a.from}% → ${a.to}%</span>
      <span class="badge">${a.action}${a.action !== "持有" ? " " + Math.abs(a.delta) + "%" : ""}</span>
    </div>`).join("");
  const holdEdits = HOLDING_KEYS[sid].map(k =>
    `<div class="hrow"><label>${k}</label><input type="number" step="0.1" min="0" max="100"
      id="hold_${sid}_${k}" value="${(PLAN.savedHoldings?.[sid]?.[k] ?? 0).toFixed(1)}"></div>`).join("");
  return `
    <div class="pc-head"><b>${sid} · ${p.market}</b>${sessionChip(p.session)}</div>
    <div class="pc-title">${p.title}（${p.plan_date}）</div>
    <div style="font-size:13px">${p.headline}</div>
    <div class="pc-rationale">依据：${p.rationale}${p.session.next_open_cst ? "<br>下次开盘：" + p.session.next_open_cst : ""}</div>
    ${acts}
    <div class="holdings-edit">
      <div class="sub" style="font-size:11.5px">我的当前持仓 %（会自动归一化为100）</div>
      ${holdEdits}
      <button style="margin-top:6px" onclick="saveHoldings('${sid}')">保存${sid}持仓并刷新计划</button>
    </div>`;
}

async function saveHoldings(sid) {
  const holdings = {};
  try {
    for (const k of HOLDING_KEYS[sid]) {
      holdings[k] = readNumber(`hold_${sid}_${k}`, `${sid} ${k}持仓比例`, 0, 100);
    }
    if (Object.values(holdings).reduce((sum, value) => sum + value, 0) <= 0)
      throw new Error("持仓比例合计必须大于 0");
    const saved = { ...(PLAN?.savedHoldings || {}), [sid]: holdings };
    const r = await fetch("/api/holdings", { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ holdings: saved }) });
    const j = await r.json();
    if (!r.ok || !j.ok) throw new Error(j.error || `HTTP ${r.status}`);
    PLAN.savedHoldings = j.holdings;
    const pr = await fetch("/api/plan");
    const pj = await pr.json();
    if (!pr.ok || !pj.ok) throw new Error(pj.error || "计划刷新失败");
    PLAN = { ...pj, savedHoldings: j.holdings };
    renderPlan();
    toast(`${sid} 持仓比例已保存，刷新后仍会保留`);
  } catch (e) {
    toast((e.message || "").includes("持仓") ? "输入无效: " + e.message : "保存失败: " + e.message);
  }
}

/* ---------- params ---------- */
const PARAM_DEFAULTS = {
  params_a: { target_vol: 0.35, trend_window: 200 },
  params_b: { mom_window: 120, ma_window: 60 },
  params_c: { target_vol: 0.40, trend_window: 200 },
};

function restoreSettings(settings) {
  const s = settings || PARAM_DEFAULTS;
  const setValue = (id, value, fallback) => {
    const el = document.getElementById(id);
    if (el) el.value = value ?? fallback;
  };
  setValue("pA_tv", s.params_a?.target_vol, PARAM_DEFAULTS.params_a.target_vol);
  setValue("pA_tw", s.params_a?.trend_window, PARAM_DEFAULTS.params_a.trend_window);
  setValue("pC_tv", s.params_c?.target_vol, PARAM_DEFAULTS.params_c.target_vol);
  setValue("pC_tw", s.params_c?.trend_window, PARAM_DEFAULTS.params_c.trend_window);
}

function readNumber(id, label, min, max, integer = false) {
  const el = document.getElementById(id);
  const raw = el.value.trim();
  const value = Number(raw);
  if (!raw || !Number.isFinite(value)) throw new Error(`${label}必须是有效数值`);
  if (integer && !Number.isInteger(value)) throw new Error(`${label}必须是整数`);
  if (value < min || value > max) throw new Error(`${label}应在 ${min} 至 ${max} 之间`);
  return value;
}

function readParams(sid) {
  if (sid === "A") return { params_a: {
    target_vol: readNumber("pA_tv", "A 波动率目标", 0.05, 1.50),
    trend_window: readNumber("pA_tw", "A 趋势闸门 SMA", 20, 500, true),
  }};
  if (sid === "B") return { params_b: {
    mom_window: readNumber("pB_mom", "B 动量窗口", 20, 500, true),
    ma_window: readNumber("pB_ma", "B 均线闸门", 20, 500, true),
  }};
  return { params_c: {
    target_vol: readNumber("pC_tv", "C 波动率目标", 0.05, 1.50),
    trend_window: readNumber("pC_tw", "C 趋势闸门 SMA", 20, 500, true),
  }};
}

async function rerun(sid) {
  let body;
  try { body = readParams(sid); }
  catch (e) { toast("输入无效: " + e.message); return; }
  toast("正在保存并重跑回测…");
  try {
    const r = await fetch("/api/rerun", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    const j = await r.json();
    if (!r.ok || !j.ok) throw new Error(j.error || `HTTP ${r.status}`);
    DATA.A = j.A; DATA.B = j.B; DATA.C = j.C;
    restoreSettings(j.settings);
    renderAll(); loadPlan(); toast(`${sid} 参数已保存，刷新后仍会保留`);
  } catch (e) {
    toast("保存失败: " + e.message);
  }
}

function genReview() {
  const lines = [`生成时间：${new Date().toLocaleString("zh-CN")}`];
  if (PLAN) {
    lines.push("【交易计划】");
    for (const [sid, p] of Object.entries(PLAN.plans))
      lines.push(`${sid} ${p.market}｜${p.session.state_cn}｜${p.title}(${p.plan_date})｜${p.headline}`);
  }
  for (const sid of ["A", "B", "C"]) {
    const d = DATA[sid]; if (!d) continue;
    lines.push(`【策略${sid} ${d.name}】当前：${JSON.stringify(d.current)}`);
    for (const [w, x] of Object.entries(d.windows))
      lines.push(`${x.label}: 策略 ${x.strategy.total_return}% / 回撤 ${x.strategy.max_dd}% vs 基准 ${x.benchmark.total_return}% / 回撤 ${x.benchmark.max_dd}% -> ${x.beats_benchmark ? "跑赢" : "未跑赢"}`);
  }
  const text = `请基于以下量化面板数据做今日复盘（三地市场环境、各策略表现归因、明日关注点）：\n\n${lines.join("\n")}`;
  navigator.clipboard.writeText(text).then(() => toast("复盘提示词已复制，粘贴给 BB 即可"), () => {
    const blob = new Blob([text], { type: "text/plain" });
    const a = document.createElement("a"); a.href = URL.createObjectURL(blob); a.download = "review_context.txt"; a.click();
    toast("已导出 review_context.txt");
  });
}

/* ---------- AI decision layer ---------- */
let AICFG = null;
let SELECTED_GROUPS = new Set();   // 配置组 multi-select for one decide run
let EDITING_PROFILE = null;        // original name while editing a config row
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
async function initAi() {
  const chip = document.getElementById("aiChip");
  try {
    const j = await (await fetch("/api/ai/config")).json();
    AICFG = j;
    updateAiChip();
    if (!j.ready) document.getElementById("aiSettings").hidden = false;
    renderProfileManager();
    renderGroupChips();
    loadAiHistory();
  } catch (e) { chip.textContent = "配置接口异常"; }
}
function updateAiChip() {
  const chip = document.getElementById("aiChip");
  if (!chip || !AICFG) return;
  if (AICFG.ready) { chip.textContent = "已就绪 · " + AICFG.model; chip.className = "chip info"; }
  else { chip.textContent = "未配置"; chip.className = "chip warn"; }
}
/* ----- 配置组 chips（生成区） ----- */
function groupList() {
  const out = [];
  for (const p of (AICFG?.profiles || [])) {
    const g = p.group || "默认组";
    const hit = out.find(x => x.name === g);
    if (hit) hit.count += 1; else out.push({ name: g, count: 1 });
  }
  return out;
}
function renderGroupChips() {
  const el = document.getElementById("aiProfileChips");
  if (!el) return;
  const groups = groupList();
  [...SELECTED_GROUPS].forEach(g => { if (!groups.some(x => x.name === g)) SELECTED_GROUPS.delete(g); });
  if (!groups.length) {
    el.innerHTML = '<span class="sub">尚未保存模型配置（在「设置」里新增，可按配置组批量生成）</span>';
    updateGoBtnLabel(); return;
  }
  el.innerHTML = groups.map(g => {
    const names = (AICFG.profiles || []).filter(p => (p.group || "默认组") === g.name)
      .map(p => p.name).join("、");
    return `<span class="pchip${SELECTED_GROUPS.has(g.name) ? " on" : ""}" ` +
      `title="组内模型：${esc(names)}" onclick="toggleGroup('${esc(g.name)}')">` +
      `${esc(g.name)} (${g.count})</span>`;
  }).join("");
  updateGoBtnLabel();
}
function toggleGroup(name) {
  SELECTED_GROUPS.has(name) ? SELECTED_GROUPS.delete(name) : SELECTED_GROUPS.add(name);
  renderGroupChips();
}
function updateGoBtnLabel() {
  const btn = document.getElementById("aiGoBtn");
  if (!btn || btn.disabled) return;
  const n = SELECTED_GROUPS.size;
  btn.textContent = n > 0 ? `生成交易指令（${n} 组模型并行）` : "生成交易指令";
}
/* ----- 模型配置管理表（设置区） ----- */
function renderProfileManager() {
  const tb = document.getElementById("ai_pm_body");
  if (!tb) return;
  const ps = AICFG?.profiles || [];
  document.getElementById("ai_groups").innerHTML =
    [...new Set(ps.map(p => p.group || "默认组"))].map(g => `<option value="${esc(g)}">`).join("");
  if (!ps.length) {
    tb.innerHTML = '<tr><td colspan="5" class="sub">暂无配置，点上方「＋ 新增模型配置」创建第一个</td></tr>';
    return;
  }
  tb.innerHTML = ps.map(p => `<tr>
    <td>${esc(p.name)}</td>
    <td>${esc(p.group || "默认组")}</td>
    <td>${esc(p.model || "—")}</td>
    <td class="sub">${esc(p.base_url || "")}</td>
    <td style="white-space:nowrap"><button style="padding:2px 10px;font-size:12px" onclick="startEditProfile('${esc(p.name)}')">编辑</button>
      <button style="padding:2px 10px;font-size:12px" onclick="deleteProfile('${esc(p.name)}')">删除</button></td>
  </tr>`).join("");
}
function startEditProfile(name) {
  const p = (AICFG?.profiles || []).find(x => x.name === name);
  if (!p) return;
  EDITING_PROFILE = name;
  document.getElementById("ai_profile").value = p.name;
  document.getElementById("ai_group").value = p.group || "默认组";
  document.getElementById("ai_base").value = p.base_url || "";
  document.getElementById("ai_model").value = p.model || "";
  document.getElementById("ai_key").value = "";
  document.getElementById("aiKeyHint").textContent =
    `正在修改「${name}」；API Key 留空则沿用原 Key；改配置名=重命名`;
  document.getElementById("aiProfileForm").hidden = false;
}
function startAddProfile() {
  EDITING_PROFILE = null;
  ["ai_profile", "ai_group", "ai_base", "ai_model", "ai_key"].forEach(id =>
    document.getElementById(id).value = "");
  document.getElementById("aiKeyHint").textContent =
    "新增配置：填好后点「保存配置」。同一配置组的模型会在勾选该组时一起运行";
  document.getElementById("aiProfileForm").hidden = false;
  document.getElementById("ai_profile").focus();
}
function cancelEditProfile() {
  EDITING_PROFILE = null;
  document.getElementById("aiProfileForm").hidden = true;
}
async function deleteProfile(name) {
  if (!confirm(`确认删除模型配置「${name}」？此操作不可撤销。`)) return;
  try {
    const j = await (await fetch("/api/ai/config/delete", { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ profile: name }) })).json();
    if (!j.ok) throw new Error(j.error || "未知错误");
    AICFG = j;
    if (EDITING_PROFILE === name) cancelEditProfile();
    updateAiChip();
    renderProfileManager();
    renderGroupChips();
    toast(`已删除配置「${name}」`);
  } catch (e) { toast("删除失败：" + e.message); }
}
function toggleAiSettings() {
  const el = document.getElementById("aiSettings");
  el.hidden = !el.hidden;
}
async function saveAiConfig(silent = false) {
  const name = document.getElementById("ai_profile").value.trim();
  if (!name) { toast("请先填写配置名称"); return false; }
  const body = {
    base_url: document.getElementById("ai_base").value.trim(),
    model: document.getElementById("ai_model").value.trim(),
    profile: name,
    group: document.getElementById("ai_group").value.trim() || "默认组",
    api_key: document.getElementById("ai_key").value.trim(),
    original_name: (EDITING_PROFILE && EDITING_PROFILE !== name) ? EDITING_PROFILE : null,
  };
  const r = await fetch("/api/ai/config", { method: "POST",
    headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const j = await r.json();
  if (!j.ok) { toast("保存失败: " + j.error); return false; }
  let renamed = false;
  if (body.original_name) {   // rename = save new + remove old
    try {
      const d = await (await fetch("/api/ai/config/delete", { method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ profile: body.original_name }) })).json();
      if (d.ok) { AICFG = d; renamed = true; }
      else toast(`提示：旧配置「${body.original_name}」删除失败：${d.error}`);
    } catch (e) { toast("提示：旧配置删除请求失败"); }
  } else {
    AICFG = j;
  }
  EDITING_PROFILE = null;
  document.getElementById("ai_key").value = "";
  document.getElementById("aiProfileForm").hidden = true;
  updateAiChip();
  renderProfileManager();
  renderGroupChips();
  if (!silent) toast(renamed ? `已保存并重命名为「${name}」` : `AI 配置「${name}」已保存`);
  return true;
}

async function testAi() {
  const el = document.getElementById("aiTestResult");
  el.style.color = "";
  el.textContent = "测试中…（最长60秒）";
  // If the edit form is open with a filled name, persist it first so we test what user sees.
  const formOpen = !document.getElementById("aiProfileForm").hidden;
  const formName = document.getElementById("ai_profile").value.trim();
  if (formOpen && formName) {
    const okSaved = await saveAiConfig(true);
    if (!okSaved) { el.textContent = "✗ 配置未保存，无法测试"; el.style.color = "var(--red)"; return; }
  }
  try {
    const j = await (await fetch("/api/ai/test", { method: "POST" })).json();
    if (j.ok) {
      el.textContent = `✓ 连接正常 · ${j.model} · ${j.elapsed_s}s · 回复「${j.reply}」`;
      el.style.color = "var(--green)";
      toast("LLM 连接正常");
    } else {
      el.textContent = "✗ " + (j.error || "未知错误");
      el.style.color = "var(--red)";
      toast("连接失败，详见设置区提示");
    }
  } catch (e) {
    el.textContent = "✗ 网络错误：" + e.message;
    el.style.color = "var(--red)";
  }
}

function showAiError(msg) {
  const el = document.getElementById("aiError");
  el.textContent = msg; el.hidden = false;
}
const ACT_CN = { buy: "买入", sell: "卖出", hold: "持有" };
function money(v, currency) {
  const symbol = { CNY: "¥", USD: "$", HKD: "HK$" }[currency] || "";
  return v == null ? "—" : `${symbol}${v}`;
}
function priceText(d) {
  if (d.action === "hold") return "执行价：不适用（持有）";
  const ref = d.reference_close != null
    ? ` · 信号参考 ${money(d.reference_close, d.currency)}（${esc(d.price_as_of || "日期未知")} 收盘）`
    : " · 参考行情暂不可用（无需手动填写）";
  if (d.price_mode === "market_open") return `执行：下一交易日真实开盘价${ref}`;
  if (d.price_mode === "limit") {
    const p = d.limit_price != null ? money(d.limit_price, d.currency) : `${money(d.limit_low, d.currency)} ~ ${money(d.limit_high, d.currency)}`;
    return `执行：限价 ${p}${ref}`;
  }
  return `执行：下一交易日开盘价${ref}`;
}

function buildAiResultHtml(j) {
  const confCls = { high: "up", medium: "warn", low: "down" }[j.confidence] || "";
  let html = `
    <div class="ai-headline">
      <span>记录 ${esc(j.decision_id || "未落库")}</span>
      <span>市场 ${esc((j.selected_markets || []).join("、"))}</span>
      <span>${esc(j.generated_at)}</span>
      <span class="chip info">${esc(j.model)}</span>
      <span>模型配置 ${esc(j.model_profile || "默认模型")}</span>
      <span>数据源 ${esc(j.analysis_config?.data_source_label || "自动路由")}</span>
      <span>视角 ${(j.analysis_config?.view_labels || []).map(esc).join("、") || "默认"}</span>
      <span>耗时 ${j.elapsed_s}s</span>
      <span>新闻 ${j.news_count} 条</span>
      <span class="chip ${confCls}">置信度 ${esc(j.confidence)}</span>
      <span class="sub">指令为建议，需人工确认后手动执行</span>
    </div>`;
  const mv = j.market_view || {};
  const MK_LABEL = { US: "🇺🇸 美股观点", CN: "🇨🇳 A股观点", HK: "🇭🇰 港股观点" };
  const mks = (j.selected_markets || []).length ? j.selected_markets : ["US", "CN", "HK"];
  html += `<div class="ai-mv">` +
    mks.map(k =>
      `<div class="m"><div class="k">${MK_LABEL[k] || k}</div><div>${esc(mv[k] || "—")}</div></div>`
    ).join("") + `</div>`;

  const bySid = { A: [], B: [], C: [], WATCH: [] };
  for (const d of j.decisions) bySid[d.strategy]?.push(d);
  for (const sid of ["A", "B", "C", "WATCH"]) {
    const sess = j.sessions?.[sid] || (sid === "WATCH" ? { market: "自选观察", plan_for: "本次生成", plan_date: "" } : {});
    const list = bySid[sid];
    const trade = list.some(d => d.action === "buy" || d.action === "sell");
    html += `<div class="decwrap${trade ? " trade" : ""}">
      <div class="dw-title">${sid} · ${esc(sess.market || "")}
        <span class="chip">${esc(sess.plan_for || "")}（${esc(sess.plan_date || "")}）</span>
        <span class="sub">${esc(sess.state_cn || "")}</span></div>`;
    if (!list.length) {
      html += `<div class="sub">无指令 → 维持规则基线</div>`;
    }
    for (const d of list) {
      const bcls = d.action === "buy" ? "buy" : d.action === "sell" ? "sell" : "hold";
      html += `
        <div class="act ${bcls}">
          <span>${esc(d.asset)}</span>
          <span style="color:var(--muted);font-size:12px">基线 ${d.baseline_pct}% → 目标 ${d.target_pct}%</span>
          ${d.deviation_flag ? `<span class="dev-badge">⚠ ${esc(d.deviation_flag)}</span>` : ""}
          <span class="badge">${ACT_CN[d.action] || esc(d.action)} ${d.action !== "hold" ? Math.abs(Math.round(d.target_pct - d.baseline_pct)) + "%" : ""}</span>
        </div>
        <div class="dw-price">${priceText(d)}${d.price_condition ? "｜" + esc(d.price_condition) : ""}${d.price_reference ? "｜依据：" + esc(d.price_reference) : ""}</div>
        <div class="dw-reason">${esc(d.reason)}${d.deviation_note ? "｜偏离说明：" + esc(d.deviation_note) : ""}</div>`;
    }
    html += `</div>`;
  }
  if (j.risk_notes) html += `<div class="ai-risk"><b>风险提示：</b>${esc(j.risk_notes)}</div>`;
  if (j.warnings?.length)
    html += `<div class="ai-warns"><b>护栏修正：</b><br>` + j.warnings.map(w => "· " + esc(w)).join("<br>") + `</div>`;
  return html;
}

function renderAiResult(j) {
  document.getElementById("aiError").hidden = true;
  document.getElementById("aiResult").innerHTML = buildAiResultHtml(j);
}

async function runDecide() {
  const btn = document.getElementById("aiGoBtn");
  const markets = [...document.querySelectorAll(".ai-market:checked")].map(x => x.value);
  if (!markets.length) { showAiError("至少选择一个市场"); return; }
  const nGroups = SELECTED_GROUPS.size;
  btn.disabled = true;
  btn.textContent = nGroups > 0 ? `并行分析中…（${nGroups} 组模型同时运行，约等于最慢一个，10~60秒）` : "生成中…（约10~60秒）";
  document.getElementById("aiError").hidden = true;
  if (nGroups > 0) {
    // immediate progress placeholder: one spinner line per selected group
    const gnames = [...SELECTED_GROUPS];
    document.getElementById("aiResult").innerHTML =
      `<div class="sub" style="margin-bottom:6px">⏳ 已启动 ${gnames.length} 个配置组<b>并行</b>分析（共用同一份行情/新闻上下文），总耗时≈最慢的模型：</div>` +
      gnames.map(g => {
        const names = (AICFG?.profiles || []).filter(p => (p.group || "默认组") === g).map(p => p.name).join("、");
        return `<div style="font-size:13px;margin:4px 0">· <b>${esc(g)}</b>（${esc(names)}）<span class="sub">— 运行中…</span></div>`;
      }).join("");
  }
  try {
    const views = [...document.querySelectorAll(".ai-view:checked")].map(x => x.value);
    const payload = {
      markets, data_source: document.getElementById("ai_data_source").value,
      news_source: document.getElementById("ai_news_source").value, views
    };
    if (nGroups > 0) payload.model_groups = [...SELECTED_GROUPS];
    else payload.model_profile = (AICFG?.profile || "").trim() || null;
    const r = await fetch("/api/ai/decide", { method: "POST",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
    const j = await r.json();
    if (!j.ok) throw new Error(j.error || "未知错误");
    if (j.multi) {
      let html = j.results.map(res => buildAiResultHtml(res)).join(
        '<hr style="border:none;border-top:1px dashed var(--line,#444);margin:16px 0">');
      for (const err of (j.errors || []))
        html += `<div style="color:var(--red);font-size:13px;margin-top:10px">✗ 模型「${esc(err.profile)}」失败：${esc(err.error)}</div>`;
      document.getElementById("aiResult").innerHTML = html;
      toast(`已完成 ${j.results.length} 个模型分析${j.errors?.length ? `，${j.errors.length} 个失败` : ""}`);
    } else {
      renderAiResult(j);
      toast("AI 指令已生成，请人工确认后执行");
    }
    loadAiHistory();
  } catch (e) { showAiError("生成失败：" + e.message); }
  finally { btn.disabled = false; updateGoBtnLabel(); }
}

async function showHistory(id) {
  if (!id) return;
  try {
    const j = await (await fetch("/api/ai/history/" + encodeURIComponent(id))).json();
    if (!j.ok) throw new Error(j.error || "记录不存在");
    const r = j.record;
    const text = `决策 ${r.decision_id}\n生成时间: ${r.generated_at}\n市场: ${(r.selected_markets || []).join(",")}\n\n最终指令（含价格建议）:\n${JSON.stringify(r.result_json || {}, null, 2)}\n\n模型原文:\n${r.raw_response}\n\n上下文摘要:\n${JSON.stringify(r.request_context, null, 2)}`;
    const blob = new Blob([text], { type: "text/plain;charset=utf-8" });
    const a = document.createElement("a"); a.href = URL.createObjectURL(blob); a.download = `decision_${r.decision_id}.txt`; a.click();
    toast("已导出完整决策追溯记录");
  } catch (e) { toast("追溯失败：" + e.message); }
}

async function loadAiHistory() {
  const el = document.getElementById("aiHistory");
  try {
    const j = await (await fetch("/api/ai/history")).json();
    if (!j.history?.length) { el.innerHTML = '<span class="sub">暂无记录</span>'; return; }
    el.innerHTML = "<table><tr><th>时间</th><th>市场</th><th>模型配置</th><th>模型</th><th>置信度</th><th>指令摘要</th><th></th></tr>" +
      j.history.slice(0, 15).map(h => {
        const acts = (h.decisions || []).map(d => `${d.strategy}:${d.asset}${ACT_CN[d.action] || ""}${d.target_pct}%${d.price_mode === "market_open" ? "@开盘" : d.price_mode === "limit" ? `@${d.limit_price ?? `${d.limit_low ?? "?"}~${d.limit_high ?? "?"}`}` : ""}`).join("、") || "维持基线";
        return `<tr><td style="white-space:nowrap">${esc(h.generated_at)}</td><td>${esc((h.selected_markets || []).join("、"))}</td><td>${esc(h.model_profile || "默认")}</td><td>${esc(h.model)}</td><td>${esc(h.confidence)}</td><td>${esc(acts)}</td><td><button onclick="showHistory('${esc(h.decision_id)}')">追溯</button></td></tr>`;
      }).join("") + "</table>";
  } catch (e) { el.innerHTML = '<span class="sub">历史加载失败</span>'; }
}

loadData();
initAi();

/* ---------- user watchlist ---------- */
const WL_MK = { a: "A股", us: "美股", hk: "港股" };

async function loadWatch() {
  const el = document.getElementById("wlBody");
  try {
    const j = await (await fetch("/api/watchlist")).json();
    if (!j.ok) throw new Error(j.error || "加载失败");
    renderWatch(j.items);
  } catch (e) { el.innerHTML = `<span class="sub">加载失败：${esc(e.message)}</span>`; }
}

function renderWatch(items) {
  const el = document.getElementById("wlBody");
  if (!items.length) { el.innerHTML = '<span class="sub">暂无自选，用上方表单添加（会自动拉取行情并进入 AI 视野）</span>'; return; }
  el.innerHTML = "<table><tr><th>代码</th><th>名称</th><th>市场</th><th>持仓%</th><th>现价</th><th>1日</th><th>5日</th><th>20日</th><th>波动20d</th><th>vs SMA50</th><th>vs SMA200</th><th>截至</th><th></th></tr>" +
    items.map(it => {
      const s = it.snap;
      const cells = s ? `
        <td>${s.close}</td>
        <td class="${cls(s.chg_1d_pct)}">${fmt(s.chg_1d_pct, "%")}</td>
        <td class="${cls(s.chg_5d_pct)}">${fmt(s.chg_5d_pct, "%")}</td>
        <td class="${cls(s.chg_20d_pct)}">${fmt(s.chg_20d_pct, "%")}</td>
        <td>${s.realized_vol_20d_pct ?? "—"}</td>
        <td class="${cls(s.vs_sma50_pct)}">${fmt(s.vs_sma50_pct, "%")}</td>
        <td class="${cls(s.vs_sma200_pct)}">${fmt(s.vs_sma200_pct, "%")}</td>
        <td style="color:var(--dim)">${s.as_of}</td>`
        : `<td colspan="7" class="sub">数据不可用：${esc(it.error || "")}</td>`;
      return `<tr>
        <td>${esc(it.code)}</td>
        <td>${esc(it.name || "—")}</td>
        <td>${WL_MK[it.market] || it.market}</td>
        <td><input class="wl-holding" data-code="${esc(it.code)}" type="number" min="0" max="100" step="0.1" value="${Number(it.holding_pct || 0)}" style="width:72px" onchange="saveWatchHolding('${esc(it.code)}', this.value)"></td>
        ${cells}
        <td><button style="padding:2px 10px;font-size:12px" onclick="rmWatch('${esc(it.code)}')">删除</button></td>
      </tr>`;
    }).join("") + "</table>";
}

async function saveWatchHolding(code, value) {
  const r = await fetch("/api/holdings"); const j = await r.json();
  const h = j.holdings || {}; h.WATCH = h.WATCH || {}; h.WATCH[code] = Math.max(0, Math.min(100, Number(value) || 0));
  const saved = await (await fetch("/api/holdings", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ holdings: h }) })).json();
  if (saved.ok) toast(`${code} 持仓已保存`); else toast("持仓保存失败");
}

async function addWatch() {
  const code = document.getElementById("wl_code").value.trim();
  if (!code) { toast("请输入代码"); return; }
  const market = document.getElementById("wl_market").value;
  const r = await fetch("/api/watchlist/add", { method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ code, market }) });
  const j = await r.json();
  if (!j.ok) { toast("添加失败: " + j.error); return; }
  document.getElementById("wl_code").value = "";
  toast(`已添加 ${code}，正在刷新行情…`);
  loadWatch();
}

async function rmWatch(code) {
  const r = await fetch("/api/watchlist/remove", { method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ code }) });
  const j = await r.json();
  if (!j.ok) { toast("删除失败"); return; }
  toast("已删除 " + code);
  loadWatch();
}

loadWatch();
