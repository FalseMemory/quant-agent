/* Quant Agent dashboard logic — daily-only, 3 strategies, plan section */
"use strict";

const DATA = { A: null, B: null, C: null };
let PLAN = null;
let LOCAL_SETTINGS = null;
let LOCAL_HOLDINGS = {};
const LOCAL_DEFAULT_HOLDINGS = {
  A: { TQQQ: 100 },
  B: { "510880 红利ETF": 100 },
  C: { "7200.HK": 100 },
};
const state = {
  A: { window: "full", charts: {} },
  B: { window: "full", charts: {} },
  C: { window: "full", charts: {} },
};
const WINDOWS = [
  ["full", "全历史"], ["3y", "近3年"], ["1y", "近1年"], ["since_jun", "2026-06以来"],
];
function holdingKeys(sid) {
  const plan = PLAN?.plans?.[sid];
  const active = (plan?.pool || []).map(x => x.asset).filter(Boolean);
  const legacy = (plan?.legacy_holdings || []).map(x => x.asset).filter(Boolean);
  return [...new Set([...active, ...legacy, "现金"])];
}

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
  const btn = document.getElementById("refreshBtn");
  if (force && btn) { btn.disabled = true; btn.textContent = "刷新中…"; }
  try {
    LOCAL_SETTINGS = await BrowserStore.get("strategy-settings", null);
    LOCAL_HOLDINGS = await BrowserStore.get("holdings", LOCAL_DEFAULT_HOLDINGS);
    const endpoint = LOCAL_SETTINGS ? "/api/rerun" : (force ? "/api/summary?force=true" : "/api/summary");
    const options = LOCAL_SETTINGS ? {
      method: "POST", headers: { "Content-Type": "application/json" },
      // `refresh` must ride along with the stored settings — the rerun branch
      // used to drop it, so 刷新数据 replayed the cached series instead of
      // re-downloading them and the "数据截至" date never moved.
      body: JSON.stringify({ ...LOCAL_SETTINGS, refresh: force }),
    } : undefined;
    const r = await fetch(endpoint, options);
    const j = await r.json();
    if (!j.ok) throw new Error(j.error || "unknown");
    DATA.A = j.A; DATA.B = j.B; DATA.C = j.C;
    LOCAL_SETTINGS = j.settings;
    if (!await BrowserStore.get("strategy-settings", null)) await BrowserStore.set("strategy-settings", j.settings);
    restoreSettings(LOCAL_SETTINGS);
    document.getElementById("updated").textContent =
      `数据截至 ${DATA.A.current.as_of} (美) / ${DATA.B.current.as_of} (A) / ${DATA.C.current.as_of} (港)`;
    renderAll();
    loadPlan();
    if (force) toast("数据已刷新");
  } catch (e) {
    toast("加载失败: " + e.message);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = "刷新数据"; }
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
    chip.textContent = `${c.etf || "现金"} · 仓位 ${c.exposure}% · 波动 ${c.realized_vol ?? "—"}% · 闸门${c.trend_gate_on ? "开" : "关"}`;
    chip.className = "chip " + (c.exposure >= 60 ? "up" : c.exposure <= 5 ? "down" : "warn");
  }
  const wins = WINDOWS.concat(d.windows.custom ? [["custom", "自定义"]] : []);
  if (!wins.some(([w]) => st.window === w)) st.window = wins[0][0];
  const tabs = document.getElementById("tabs" + sid);
  tabs.innerHTML = "";
  wins.forEach(([w, label]) => {
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
      <div class="m"><div class="k">策略收益</div><div class="v ${cls(S.total_return)}">${fmt(S.total_return, "%")}</div><div class="s">${esc(wp.bench_name || "基准")} ${fmt(Bm.total_return, "%")} · ${wp.beats_benchmark ? "跑赢 ✓" : "未跑赢 ✗"}</div></div>
      <div class="m"><div class="k">策略年化</div><div class="v">${fmt(S.cagr, "%")}</div><div class="s">基准 ${fmt(Bm.cagr, "%")}</div></div>
      <div class="m"><div class="k">最大回撤</div><div class="v neg">${fmt(S.max_dd, "%")}</div><div class="s">基准 ${fmt(Bm.max_dd, "%")}</div></div>
      <div class="m"><div class="k">Sharpe</div><div class="v">${fmt(S.sharpe)}</div><div class="s">基准 ${fmt(Bm.sharpe)}</div></div>
      <div class="m"><div class="k">窗口</div><div class="v" style="font-size:14px;line-height:30px">${wp.start} → ${wp.end}</div></div>
    </div>
    <div class="charts">
      <div class="chartbox"><h3>净值曲线（实线=策略，虚线=${esc(wp.bench_name || "基准")}）</h3><canvas id="eq_${sid}"></canvas></div>
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
const B_BENCH_COLORS = ["#4e91e6", "#f0a43a", "#b06ce0", "#e6c84e", "#42b8a5", "#e06f8b", "#7bbf5e", "#d97b45", "#6f8ee6", "#c6a45a"];
let eqChart = {}, ddChart = {};
function drawCharts(sid, wp) {
  const C = chartColors();
  const labels = wp.dates;
  const benchName = wp.bench_name || "基准";
  const positionLines = index => {
    const p = wp.positions?.[index];
    if (!p) return ["持仓：暂无数据"];
    const items = Array.isArray(p.items) && p.items.length
      ? p.items : [{ asset: p.asset || "现金", weight: p.weight ?? 0 }];
    return ["当日目标持仓：", ...items.map(x => `  ${x.asset}  ${x.weight}%`)];
  };
  const extraBenches = Array.isArray(wp.benchmark_curves)
    ? wp.benchmark_curves.map((curve, i) => ({
        label: curve.name,
        data: curve.values,
        borderColor: B_BENCH_COLORS[i % B_BENCH_COLORS.length],
        backgroundColor: B_BENCH_COLORS[i % B_BENCH_COLORS.length],
        borderWidth: 1.25, borderDash: [5, 4], pointRadius: 0, pointHoverRadius: 3,
        tension: .1, hidden: true,
      })) : [];
  const datasets = [
    { label: `策略 ${sid}`, data: wp.equity, borderColor: C.red, backgroundColor: C.red,
      borderWidth: 1.9, pointRadius: 0, pointHoverRadius: 3, tension: .1 },
    { label: benchName, data: wp.bench_equity, borderColor: C.gray, backgroundColor: C.gray,
      borderWidth: 1.4, borderDash: [7, 5], pointRadius: 0, pointHoverRadius: 3, tension: .1 },
    ...extraBenches,
  ];
  if (eqChart[sid]) eqChart[sid].destroy();
  if (ddChart[sid]) ddChart[sid].destroy();
  eqChart[sid] = new Chart(document.getElementById(`eq_${sid}`), {
    type: "line",
    data: { labels, datasets },
    options: {
      maintainAspectRatio: false,
      interaction: { mode: "index", intersect: false, axis: "x" },
      plugins: {
        legend: {
          display: true,
          position: "top",
          align: "end",
          onClick: (event, item, legend) => {
            const chart = legend.chart;
            chart.setDatasetVisibility(item.datasetIndex, !chart.isDatasetVisible(item.datasetIndex));
            chart.update();
          },
          labels: {
            color: C.txt, boxWidth: 24, boxHeight: 2, padding: 14, font: { size: 11 },
            generateLabels: chart => Chart.defaults.plugins.legend.labels.generateLabels(chart)
              .map(item => ({ ...item, text: `${item.hidden ? "○" : "●"} ${item.text}` })),
          },
        },
        tooltip: {
          mode: "index", intersect: false, position: "nearest", caretPadding: 10,
          displayColors: true, boxWidth: 10, boxHeight: 2, padding: 11,
          backgroundColor: "rgba(24,24,22,.94)", titleColor: "#f2f0e9", bodyColor: "#dedbd2",
          borderColor: "rgba(255,255,255,.16)", borderWidth: 1,
          titleFont: { size: 12, weight: "600" }, bodyFont: { size: 11, lineHeight: 1.45 },
          callbacks: {
            title: items => items.length ? `交易日  ${items[0].label}` : "",
            label: ctx => `${ctx.dataset.label}：${Number(ctx.parsed.y).toFixed(4)}`,
            afterBody: items => positionLines(items[0]?.dataIndex),
          },
        },
      },
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
  let rows = `<h3>${sid === "C" ? "港股" : "美股"}·动态池波动率目标</h3><table>` +
    `<tr><th>项目</th><th>状态</th></tr>` +
    `<tr><td>当前选中</td><td>${esc(d.current.etf || "现金")}</td></tr>` +
    `<tr><td>SMA${d.current.trend_window || 200} 趋势资格</td><td>${d.current.trend_gate_on ? "合格（允许持仓）" : "无合格候选（现金）"}</td></tr>` +
    `<tr><td>选中资产实现波动</td><td>20日 ${d.current.realized_vol_20 ?? "—"}% / 40日 ${d.current.realized_vol ?? "—"}% / 目标 ${d.current.target_vol}%</td></tr>` +
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
      <div class="kv"><span class="k">当前选中</span><span>${esc(c.etf || "现金")}</span></div>
      <div class="kv"><span class="k">目标仓位</span><span>${c.exposure}%</span></div>
      <div class="kv"><span class="k">实现波动</span><span>${c.realized_vol ?? "—"}%</span></div>
      <div class="kv"><span class="k">趋势闸门</span><span>${c.trend_gate_on ? "开启" : "关闭"}</span></div>
      <div class="kv"><span class="k">信号日</span><span>${c.as_of}</span></div>`;
  }
}

/* ---------- trading plan ---------- */
async function loadPlan() {
  try {
    LOCAL_HOLDINGS = await BrowserStore.get("holdings", LOCAL_HOLDINGS || LOCAL_DEFAULT_HOLDINGS);
    const pr = await fetch("/api/plan", { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ holdings: LOCAL_HOLDINGS }) });
    const pj = await pr.json();
    if (!pr.ok || !pj.ok) throw new Error(pj.error || "plan failed");
    PLAN = { ...pj, savedHoldings: LOCAL_HOLDINGS };
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

function poolTableHtml(sid) {
  const pool = PLAN.plans[sid]?.pool || [];
  const rows = pool.map(x => {
    const s = x.snap;
    const status = x.above_ma !== undefined && x.above_ma !== null
      ? `<span class="chip ${x.above_ma ? "up" : "down"}">${x.above_ma ? "SMA合格" : "SMA未过"}</span>` : "—";
    const marketCells = s ? `
      <td>${esc(s.close)}</td>
      <td class="${cls(s.chg_1d_pct)}">${fmt(s.chg_1d_pct, "%")}</td>
      <td class="${cls(s.chg_20d_pct)}">${fmt(s.chg_20d_pct, "%")}</td>
      <td>${s.realized_vol_20d_pct ?? "—"}</td>
      <td style="color:var(--dim)" title="${esc(s.source || x.quote_source || "")}">${esc(s.as_of)}${s.quote_level === "light" ? " · 快照" : ""}</td>`
      : `<td colspan="5" class="sub" title="${esc(x.quote_error || "数据源暂时不可用")}">行情暂不可用：${esc(x.quote_error || "请确认代码或稍后重试")}</td>`;
    return `<tr>
      <td>${esc(x.ref?.code || "—")}</td><td>${esc(x.asset)}</td>
      <td>${status}</td><td>${x.momentum_score == null ? "—" : Number(x.momentum_score).toFixed(3)}</td>
      ${marketCells}
      ${x.above_ma !== undefined && x.above_ma !== null ? `<td><button onclick="removePoolAsset('${sid}', '${esc(x.ref?.code || "")}')">移除</button></td>` : ""}
    </tr>`;
  }).join("");
  if (!rows) return "";
  return `<div class="tblwrap" style="margin-top:10px"><table>
    <tr><th>代码</th><th>标的</th><th>趋势资格</th><th>动量分数</th><th>现价</th><th>1日</th><th>20日</th><th>波动20d</th><th>截至</th><th></th></tr>
    ${rows}</table></div>`;
}

function poolManagerHtml(sid) {
  const d = DATA[sid] || {};
  const count = (d.pool || []).length;
  const failed = (d.failed_assets || []).map(x => `${x.code || "?"} ${x.error || "行情失败"}`);
  const hint = { A: "输入美股代码，如 SPY / QQQ", B: "输入6位ETF代码，如 510500", C: "输入港股代码，如 2800 / 2800.HK" }[sid];
  const safe = sid === "B" ? `；${esc(d.safe_asset?.etf || "国债ETF")}为系统防御资产，不占额度` : "";
  return `<div class="pool-manager">
    <div class="pm-head"><b>我的策略 ${sid} ETF 候选池</b><span class="chip info">${count}/${d.pool_limit || 10}</span>
      <span class="sub">用户池 1～10 只${safe}</span></div>
    <div class="pool-add"><input id="${sid}_pool_code" placeholder="${hint}">
      <input id="${sid}_pool_name" placeholder="名称可留空，系统自动识别">
      <button class="primary" onclick="addPoolAsset('${sid}')" ${count >= (d.pool_limit || 10) ? "disabled" : ""}>添加并重跑推荐</button></div>
    ${failed.length ? `<div class="pool-warn">行情失败：${failed.map(esc).join("；")}</div>` : ""}
  </div>`;
}

function legacyHoldingsHtml(p) {
  if (!p.legacy_holdings?.length) return "";
  return `<div class="pool-warn"><b>池外遗留持仓</b>：${p.legacy_holdings.map(x => `${esc(x.asset)} ${x.weight}%`).join("；")}。移出候选池不会自动清仓，请在持仓区保留并手动处理。</div>`;
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
  const holdEdits = holdingKeys(sid).map(k =>
    `<div class="hrow"><label>${k}</label><input type="number" step="0.1" min="0" max="100"
      id="hold_${sid}_${k}" value="${(PLAN.savedHoldings?.[sid]?.[k] ?? 0).toFixed(1)}"></div>`).join("");
  return `
    <div class="pc-head"><b>${sid} · ${p.market}</b>${sessionChip(p.session)}</div>
    <div class="pc-title">${p.title}（${p.plan_date}）</div>
    <div style="font-size:13px">${p.headline}</div>
    <div class="pc-rationale">依据：${p.rationale}${p.session.next_open_cst ? "<br>下次开盘：" + p.session.next_open_cst : ""}</div>
    ${acts}
    ${poolManagerHtml(sid)}
    ${poolTableHtml(sid)}
    ${legacyHoldingsHtml(p)}
    <div class="holdings-edit">
      <div class="sub" style="font-size:11.5px">我的当前持仓 %（会自动归一化为100）</div>
      ${holdEdits}
      <button style="margin-top:6px" onclick="saveHoldings('${sid}')">保存${sid}持仓并刷新计划</button>
    </div>`;
}

async function saveHoldings(sid) {
  const holdings = {};
  try {
    for (const k of holdingKeys(sid)) {
      holdings[k] = readNumber(`hold_${sid}_${k}`, `${sid} ${k}持仓比例`, 0, 100);
    }
    if (Object.values(holdings).reduce((sum, value) => sum + value, 0) <= 0)
      throw new Error("持仓比例合计必须大于 0");
    const saved = { ...(PLAN?.savedHoldings || {}), [sid]: holdings };
    const r = await fetch("/api/holdings/validate", { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ holdings: saved }) });
    const j = await r.json();
    if (!r.ok || !j.ok) throw new Error(j.error || `HTTP ${r.status}`);
    LOCAL_HOLDINGS = j.holdings;
    await BrowserStore.set("holdings", LOCAL_HOLDINGS);
    PLAN.savedHoldings = LOCAL_HOLDINGS;
    const pr = await fetch("/api/plan", { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ holdings: LOCAL_HOLDINGS }) });
    const pj = await pr.json();
    if (!pr.ok || !pj.ok) throw new Error(pj.error || "计划刷新失败");
    PLAN = { ...pj, savedHoldings: LOCAL_HOLDINGS };
    renderPlan();
    toast(`${sid} 持仓比例已保存，刷新后仍会保留`);
  } catch (e) {
    toast((e.message || "").includes("持仓") ? "输入无效: " + e.message : "保存失败: " + e.message);
  }
}

/* ---------- params ---------- */
const PARAM_DEFAULTS = {
  params_a: { target_vol: 0.35, trend_window: 200 },
  params_b: { mom_window: 180, ma_window: 60 },
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
  setValue("pB_mom", s.params_b?.mom_window, PARAM_DEFAULTS.params_b.mom_window);
  setValue("pB_ma", s.params_b?.ma_window, PARAM_DEFAULTS.params_b.ma_window);
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
    assets: poolAssets("A"),
  }};
  if (sid === "B") return { params_b: {
    mom_window: readNumber("pB_mom", "B 动量窗口", 20, 500, true),
    ma_window: readNumber("pB_ma", "B 均线闸门", 20, 500, true),
    assets: (DATA.B?.pool || []).map(x => ({ code: x.code, name: x.name || splitAssetLabel(x.etf || "").name, class: x.class || "用户候选" })),
    safe_asset: DATA.B?.safe_asset ? { code: DATA.B.safe_asset.code, name: DATA.B.safe_asset.name, class: DATA.B.safe_asset.class || "系统防御" } : undefined,
  }};
  return { params_c: {
    target_vol: readNumber("pC_tv", "C 波动率目标", 0.05, 1.50),
    trend_window: readNumber("pC_tw", "C 趋势闸门 SMA", 20, 500, true),
    assets: poolAssets("C"),
  }};
}

function poolAssets(sid) {
  return (DATA[sid]?.pool || []).map(x => ({ code: x.code, name: x.name, class: x.class || "用户候选" }));
}

async function savePool(sid, assets) {
  if (!assets.length) { toast("候选池至少保留1只ETF"); return; }
  let body;
  try {
    body = readParams(sid);
    body[`params_${sid.toLowerCase()}`].assets = assets;
  } catch (e) { toast("输入无效: " + e.message); return; }
  toast(`正在校验ETF池并重跑策略${sid}…`);
  try {
    const r = await fetch("/api/rerun", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    const j = await r.json();
    if (!r.ok || !j.ok) throw new Error(j.error || `HTTP ${r.status}`);
    DATA.A = j.A; DATA.B = j.B; DATA.C = j.C;
    LOCAL_SETTINGS = j.settings;
    await BrowserStore.set("strategy-settings", LOCAL_SETTINGS);
    restoreSettings(LOCAL_SETTINGS); renderAll(); await loadPlan();
    toast(`策略${sid}候选池已保存在本浏览器（${DATA[sid].pool.length}/10），推荐已更新`);
  } catch (e) { toast("候选池保存失败: " + e.message); }
}

function addPoolAsset(sid) {
  let code = document.getElementById(`${sid}_pool_code`)?.value.trim() || "";
  const name = document.getElementById(`${sid}_pool_name`)?.value.trim();
  if (sid === "A") {
    code = code.toUpperCase();
    if (!/^[A-Z0-9.-]+$/.test(code) || code.endsWith(".HK")) { toast("请输入有效美股代码，如 SPY / QQQ"); return; }
  } else if (sid === "B") {
    if (!/^\d{6}$/.test(code)) { toast("请输入6位A股ETF代码"); return; }
  } else {
    code = code.toUpperCase();
    if (!/^\d{4,5}(\.HK)?$/.test(code)) { toast("请输入港股代码，如 2800 / 2800.HK"); return; }
    code = `${code.replace(/\.HK$/, "").padStart(4, "0")}.HK`;
  }
  const assets = poolAssets(sid);
  if (assets.length >= (DATA[sid]?.pool_limit || 10)) { toast("候选池最多10只ETF"); return; }
  if (assets.some(x => sid === "B" ? String(x.code).endsWith(code) : x.code === code)) { toast("该ETF已在候选池中"); return; }
  assets.push({ code, name: name || "", class: "用户候选" });
  savePool(sid, assets);
}

function removePoolAsset(sid, code) {
  savePool(sid, poolAssets(sid).filter(x => x.code !== code && !(sid === "B" && String(x.code).endsWith(code))));
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
    LOCAL_SETTINGS = j.settings;
    await BrowserStore.set("strategy-settings", LOCAL_SETTINGS);
    restoreSettings(LOCAL_SETTINGS);
    renderAll(); loadPlan(); toast(`${sid} 参数已保存在本浏览器，刷新后仍会保留`);
    if (state[sid].customStart) applyCustom(sid);   // 重跑后自动重算自定义区间
  } catch (e) {
    toast("保存失败: " + e.message);
  }
}

/* ---------- 自定义起点收益对比 ---------- */
async function applyCustom(sid) {
  const el = document.getElementById("cust_" + sid);
  const start = el && el.value;
  if (!start) { toast("请先选择起始日期"); return; }
  toast("计算自定义区间…");
  try {
    const r = await fetch(`/api/custom?sid=${sid}&start=${encodeURIComponent(start)}`);
    const j = await r.json();
    if (!r.ok || !j.ok) throw new Error(j.error || `HTTP ${r.status}`);
    DATA[sid].windows.custom = j.payload;
    state[sid].customStart = start;
    state[sid].window = "custom";
    renderStrategy(sid);
    toast(`${sid} 已按 ${j.payload.start} 起算（假设当日收盘按目标建仓，含 15bp 成本口径）`);
  } catch (e) {
    toast("自定义区间失败: " + e.message);
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
function refreshAiConfigView(activeProfile = "") {
  const profiles = AICFG?.profiles || [];
  const active = profiles.find(p => p.name === activeProfile) || profiles[0] || {};
  AICFG = {
    profile: active.name || "", group: active.group || "默认组",
    base_url: active.base_url || "", model: active.model || "",
    ready: Boolean(active.base_url && active.model && active.api_key), profiles,
  };
}

async function persistAiProfiles(activeProfile = "") {
  await BrowserStore.saveProfiles(AICFG?.profiles || [], activeProfile || AICFG?.profile || "");
}

async function initAi() {
  const chip = document.getElementById("aiChip");
  try {
    const saved = await BrowserStore.loadProfiles();
    AICFG = { profiles: saved.profiles };
    refreshAiConfigView(saved.active_profile);
    updateAiChip();
    if (!AICFG.ready) document.getElementById("aiSettings").hidden = false;
    renderProfileManager();
    renderGroupChips();
    loadAiHistory();
  } catch (e) { chip.textContent = "浏览器本地配置异常"; }
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
    AICFG.profiles = (AICFG?.profiles || []).filter(p => p.name !== name);
    refreshAiConfigView();
    await persistAiProfiles(AICFG.profile);
    if (EDITING_PROFILE === name) cancelEditProfile();
    updateAiChip();
    renderProfileManager();
    renderGroupChips();
    toast(`已从本浏览器删除配置「${name}」`);
  } catch (e) { toast("删除失败：" + e.message); }
}
function toggleAiSettings() {
  const el = document.getElementById("aiSettings");
  el.hidden = !el.hidden;
}
async function saveAiConfig(silent = false) {
  const name = document.getElementById("ai_profile").value.trim();
  if (!name) { toast("请先填写配置名称"); return false; }
  const existing = (AICFG?.profiles || []).find(p => p.name === EDITING_PROFILE || p.name === name);
  const apiKey = document.getElementById("ai_key").value.trim() || existing?.api_key || "";
  const profile = {
    base_url: document.getElementById("ai_base").value.trim().replace(/\/$/, ""),
    model: document.getElementById("ai_model").value.trim(),
    name,
    group: document.getElementById("ai_group").value.trim() || "默认组",
    api_key: apiKey,
  };
  if (!profile.base_url.startsWith("http://") && !profile.base_url.startsWith("https://")) {
    toast("保存失败: Base URL 必须以 http:// 或 https:// 开头"); return false;
  }
  if (!profile.model || !profile.api_key) {
    toast("保存失败: 模型名和 API Key 不能为空"); return false;
  }
  const renamed = Boolean(EDITING_PROFILE && EDITING_PROFILE !== name);
  const profiles = (AICFG?.profiles || []).filter(p => p.name !== name && p.name !== EDITING_PROFILE);
  profiles.unshift(profile);
  AICFG = { profiles };
  refreshAiConfigView(name);
  await persistAiProfiles(name);
  EDITING_PROFILE = null;
  document.getElementById("ai_key").value = "";
  document.getElementById("aiProfileForm").hidden = true;
  updateAiChip();
  renderProfileManager();
  renderGroupChips();
  if (!silent) toast(renamed ? `已加密保存并重命名为「${name}」` : `AI 配置「${name}」已加密保存在本浏览器`);
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
    const cfg = (AICFG?.profiles || []).find(p => p.name === AICFG.profile);
    if (!cfg) throw new Error("找不到当前模型配置");
    const j = await (await fetch("/api/ai/test", { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ config: cfg }) })).json();
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

  const bySid = { A: [], B: [], C: [] };
  for (const d of j.decisions) bySid[d.strategy]?.push(d);
  for (const sid of ["A", "B", "C"]) {
    const sess = j.sessions?.[sid] || {};
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
    const profiles = (AICFG?.profiles || []).filter(p => p.ready);
    if (nGroups > 0) payload.model_configs = profiles.filter(p => SELECTED_GROUPS.has(p.group || "默认组"));
    else {
      const active = profiles.find(p => p.name === AICFG.profile) || profiles[0];
      if (!active) throw new Error("请先在设置中保存可用的模型配置");
      payload.model_configs = [active];
    }
    payload.holdings = LOCAL_HOLDINGS;
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
    await saveAiHistoryResult(j);
    loadAiHistory();
  } catch (e) { showAiError("生成失败：" + e.message); }
  finally { btn.disabled = false; updateGoBtnLabel(); }
}

async function saveAiHistoryResult(result) {
  const history = await BrowserStore.get("ai-history", []);
  const items = result.multi ? result.results || [] : [result];
  for (const item of items) history.unshift(item);
  await BrowserStore.set("ai-history", history.slice(0, 50));
}

async function showHistory(id) {
  if (!id) return;
  try {
    const history = await BrowserStore.get("ai-history", []);
    const r = history.find(item => item.decision_id === id);
    if (!r) throw new Error("记录不存在");
    const text = `决策 ${r.decision_id}\n生成时间: ${r.generated_at}\n市场: ${(r.selected_markets || []).join(",")}\n\n最终指令（含价格建议）:\n${JSON.stringify(r, null, 2)}`;
    const blob = new Blob([text], { type: "text/plain;charset=utf-8" });
    const a = document.createElement("a"); a.href = URL.createObjectURL(blob); a.download = `decision_${r.decision_id}.txt`; a.click();
    toast("已导出本地决策记录");
  } catch (e) { toast("追溯失败：" + e.message); }
}

async function loadAiHistory() {
  const el = document.getElementById("aiHistory");
  try {
    const history = await BrowserStore.get("ai-history", []);
    if (!history.length) { el.innerHTML = '<span class="sub">暂无本地记录</span>'; return; }
    el.innerHTML = "<table><tr><th>时间</th><th>市场</th><th>模型配置</th><th>模型</th><th>置信度</th><th>指令摘要</th><th></th></tr>" +
      history.slice(0, 15).map(h => {
        const acts = (h.decisions || []).map(d => `${d.strategy}:${d.asset}${ACT_CN[d.action] || ""}${d.target_pct}%${d.price_mode === "market_open" ? "@开盘" : d.price_mode === "limit" ? `@${d.limit_price ?? `${d.limit_low ?? "?"}~${d.limit_high ?? "?"}`}` : ""}`).join("、") || "维持基线";
        return `<tr><td style="white-space:nowrap">${esc(h.generated_at)}</td><td>${esc((h.selected_markets || []).join("、"))}</td><td>${esc(h.model_profile || "默认")}</td><td>${esc(h.model)}</td><td>${esc(h.confidence)}</td><td>${esc(acts)}</td><td><button onclick="showHistory('${esc(h.decision_id)}')">追溯</button></td></tr>`;
      }).join("") + "</table>";
  } catch (e) { el.innerHTML = '<span class="sub">本地历史加载失败</span>'; }
}

loadData();
initAi();
