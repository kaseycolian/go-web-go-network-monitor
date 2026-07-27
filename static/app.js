/* Go, Web, Go! dashboard.
   Single page, loaded once: live data over SSE, history over REST.
   No page reloads; charts mutate in place. */
"use strict";

const APP_NAME = "Go, Web, Go!";
const MACHINE_FALLBACK = "This machine";  // never the app name
const $ = (id) => document.getElementById(id);
const state = {
  range: 21600,          // seconds; "all" = null
  live: true,            // auto-follow now
  csrf: localStorage.getItem("wtb_csrf") || null,
  auth: { enabled: false, superuser: true, authed: true, configured: false },
  unit: localStorage.getItem("wtb_unit") || "Mbps",
  status: null,
  lastSSE: 0,
  charts: {},
  sparkDl: [], sparkUl: [],
  timelineData: null,
  metricsTimer: null,
  esBackoff: 1000,
  targetsSeen: [],
};

/* ---------------------------------------------------------------- theme */

/* theme-service themes (mirrors static/theme/themes.index.json). "" = Auto
   (default Rink Classic, follows OS light/dark). */
const THEMES = [
  { id: "", label: "Auto (Rink Classic)" },
  { id: "rink-classic-dark", label: "Rink Classic · Dark" },
  { id: "rink-classic-dark-no-background", label: "Rink Classic (No BG) · Dark" },
  { id: "midnight-arcade-dark", label: "Midnight Arcade · Dark" },
  { id: "midnight-arcade-dark-no-background", label: "Midnight Arcade (No BG) · Dark" },
  { id: "hot-neon-dark", label: "Hot Neon · Dark" },
  { id: "hot-neon-dark-no-background", label: "Hot Neon (No BG) · Dark" },
  { id: "synthwave-sunset-dark", label: "Synthwave Sunset · Dark" },
  { id: "acid-arcade-dark", label: "Acid Arcade · Dark" },
  { id: "rink-classic-light", label: "Rink Classic · Light" },
  { id: "rink-classic-light-no-background", label: "Rink Classic (No BG) · Light" },
  { id: "midnight-arcade-light", label: "Midnight Arcade · Light" },
  { id: "midnight-arcade-light-no-background", label: "Midnight Arcade (No BG) · Light" },
  { id: "acid-arcade-light", label: "Acid Arcade · Light" },
  { id: "acid-arcade-light-no-background", label: "Acid Arcade (No BG) · Light" },
  { id: "hot-neon-light", label: "Hot Neon · Light" },
  { id: "synthwave-sunset-light", label: "Synthwave Sunset · Light" },
];

/* apply a theme id (or "" for Auto), persist it, and re-render the charts
   (Chart.js reads its colors from CSS vars, so they must be redrawn) */
function setTheme(id) {
  const root = document.documentElement;
  if (id) { root.setAttribute("data-theme", id); localStorage.setItem("theme", id); }
  else { root.removeAttribute("data-theme"); localStorage.removeItem("theme"); }
  const sel = $("themeSelect");
  if (sel) sel.value = id;
  restyleCharts();
  drawTimeline();
  drawSparks();
}

function cssVar(name) {
  return getComputedStyle(document.documentElement)
    .getPropertyValue(name).trim();
}

/* series color follows the entity, not its slot order in the chart */
function seriesColor(target) {
  const st = state.status || {};
  if (target === st.gateway) return cssVar("--series-1");
  if (target === "1.1.1.1") return cssVar("--series-2");
  if (target === "8.8.8.8") return cssVar("--series-3");
  if (target === st.isp_hop) return cssVar("--series-4");
  return cssVar("--series-6");
}
function seriesName(target) {
  const st = state.status || {};
  if (target === st.gateway) return `router (${target})`;
  if (target === st.isp_hop) return `ISP hop (${target})`;
  return target;
}

const LAYER_COLORS = { link: "--lay-link", lan: "--lay-lan",
                       internet: "--lay-inet",
                       degraded: "--warn", dns: "--warn" };
const LAYER_NAMES = { link: "link (cable/dock/WiFi)", lan: "router/LAN",
                      internet: "internet (ISP)", degraded: "degraded",
                      dns: "DNS" };

/* ---------------------------------------------------------------- utils */

/* One fixed speed unit for the whole dashboard, so the numbers are directly
   comparable and never switch units mid-glance. */
const SPEED_UNITS = {
  Kbps: { factor: 1e3, label: "Kb/s", dec: 0 },
  Mbps: { factor: 1e6, label: "Mb/s", dec: 1 },
  Gbps: { factor: 1e9, label: "Gb/s", dec: 2 },
};
function unitDef() { return SPEED_UNITS[state.unit] || SPEED_UNITS.Mbps; }
function unitLabel() { return unitDef().label; }
function bpsToUnit(bps) { return (bps || 0) / unitDef().factor; }
function mbpsToUnit(mbps) { return (mbps || 0) * 1e6 / unitDef().factor; }

/* format a bits/sec value in the chosen unit, e.g. "94.2 Mb/s" */
function fmtSpeed(bps) {
  if (bps == null) return "–";
  const u = unitDef();
  return (bps / u.factor).toFixed(u.dec) + " " + u.label;
}
/* format a value already in Mbps (speed-test results) in the chosen unit */
function fmtSpeedMbps(mbps) {
  if (mbps == null) return "–";
  const u = unitDef();
  return (mbps * 1e6 / u.factor).toFixed(u.dec) + " " + u.label;
}

function setUnit(u) {
  if (!SPEED_UNITS[u]) u = "Mbps";
  state.unit = u;
  localStorage.setItem("wtb_unit", u);
  const sel = $("unitSelect");
  if (sel) { sel.value = u; if (sel._drop) sel._drop.sync(); }
  if (state.status) onStatus(state.status);  // refresh live tiles
  refreshMetrics().catch(() => {});           // rebuild charts in new unit
}
function fmtDur(s) {
  s = Math.round(s);
  if (s < 60) return s + "s";
  if (s < 3600) return `${(s / 60) | 0}m ${s % 60}s`;
  return `${(s / 3600) | 0}h ${((s % 3600) / 60) | 0}m`;
}
function fmtLog(ts) {
  const d = new Date(ts * 1000);
  const p = (n) => String(n).padStart(2, "0");
  return `${p(d.getMonth() + 1)}/${p(d.getDate())} ` +
         `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}
function fmtTime(ts) {
  const d = new Date(ts * 1000);
  const span = state.range || 1e9;
  return span > 172800
    ? d.toLocaleString([], { month: "short", day: "numeric",
                             hour: "2-digit", minute: "2-digit" })
    : d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}
function rangeWindow() {
  const now = Date.now() / 1000;
  return state.range == null
    ? { from: 0, to: now } : { from: now - state.range, to: now };
}

async function api(path, opts = {}) {
  if (opts.body) opts.headers = Object.assign(
    { "Content-Type": "application/json" }, opts.headers || {});
  const r = await fetch(path, opts);
  if (!r.ok) throw Object.assign(new Error("http " + r.status),
                                 { status: r.status });
  const ct = r.headers.get("content-type") || "";
  return ct.includes("json") ? r.json() : r.text();
}

/* authenticated call: send CSRF, and on 401 prompt for the passcode then
   retry once. Used for every state-changing action. */
async function apiAuth(path, opts = {}) {
  opts.headers = Object.assign({ "X-CSRF": state.csrf || "" },
                               opts.headers || {});
  try {
    return await api(path, opts);
  } catch (e) {
    if (e.status !== 401) throw e;
    if (!await showLogin()) throw e;
    opts.headers["X-CSRF"] = state.csrf || "";
    return api(path, opts);
  }
}

/* The passcode gate. `blocking` hides the cancel path (used at boot when the
   whole dashboard is locked). Resolves true once logged in. */
function showLogin(blocking = false) {
  return new Promise((resolve) => {
    const dlg = $("loginDlg");
    $("loginErr").textContent = "";
    $("pin").value = "";
    dlg.returnValue = "";
    if (dlg.open) return resolve(false);
    dlg.showModal();
    dlg.onkeydown = (ev) => {  // don't let Esc dismiss a blocking gate
      if (blocking && ev.key === "Escape") ev.preventDefault();
    };
    $("loginForm").onsubmit = async (ev) => {
      ev.preventDefault();
      await withBusy($("loginGo"), async () => {
        try {
          const res = await api("/api/auth/login", {
            method: "POST",
            body: JSON.stringify({ passcode: $("pin").value }),
          });
          state.csrf = res.csrf;
          localStorage.setItem("wtb_csrf", res.csrf);
          state.auth.authed = true;
          dlg.close("ok");
        } catch (e) {
          $("loginErr").textContent = e.status === 429
            ? "Too many attempts — wait 5 minutes."
            : e.status === 503 ? "No passcode is set on the host."
              : "Wrong passcode.";
        }
      });
    };
    dlg.onclose = () => resolve(dlg.returnValue === "ok");
  });
}

/* ---------------------------------------------------------------- charts */

function chartDefaults() {
  Chart.defaults.color = cssVar("--muted");
  Chart.defaults.borderColor = cssVar("--grid");
  Chart.defaults.font.family =
    'system-ui, -apple-system, "Segoe UI", sans-serif';
  Chart.defaults.animation = false;
  Chart.defaults.maintainAspectRatio = false;
  Chart.defaults.elements.line.borderWidth = 2;
  Chart.defaults.elements.line.tension = 0.25;
  Chart.defaults.elements.point.radius = 0;
  Chart.defaults.elements.point.hoverRadius = 4;
  Chart.defaults.interaction = { mode: "nearest", axis: "x",
                                 intersect: false };
  Chart.defaults.plugins.legend.labels.boxWidth = 12;
  Chart.defaults.plugins.legend.labels.boxHeight = 12;
}

function baseScales(yTitle, extra = {}) {
  return Object.assign({
    x: { type: "linear", ticks: { maxTicksLimit: 8,
          callback: (v) => fmtTime(v) }, grid: { display: false } },
    y: { beginAtZero: true, title: { display: true, text: yTitle },
         grid: { color: cssVar("--grid") } },
  }, extra);
}

const tooltipTitleTime = {
  callbacks: { title: (items) => items.length
      ? new Date(items[0].parsed.x * 1000).toLocaleString() : "" },
};

function makeCharts() {
  chartDefaults();
  state.charts.lat = new Chart($("latChart"), {
    type: "line",
    data: { datasets: [] },
    options: { parsing: false, scales: baseScales("latency ms"),
               plugins: { tooltip: tooltipTitleTime } },
  });
  state.charts.loss = new Chart($("lossChart"), {
    type: "line",
    data: { datasets: [] },
    options: { parsing: false,
      scales: baseScales("%  /  ms", {
        y: { beginAtZero: true, suggestedMax: 20,
             title: { display: true, text: "loss %  ·  jitter ms" },
             grid: { color: cssVar("--grid") } } }),
      plugins: { tooltip: tooltipTitleTime } },
  });
  state.charts.tp = new Chart($("tpChart"), {
    type: "line",
    data: { datasets: [] },
    options: { parsing: false,
      scales: baseScales("Mb/s"),
      plugins: { tooltip: tooltipTitleTime } },
  });
  state.charts.st = new Chart($("stChart"), {
    type: "line",
    data: { datasets: [] },
    options: { parsing: false,
      scales: baseScales("Mb/s"),
      plugins: { tooltip: Object.assign({}, tooltipTitleTime) } },
  });
}

function restyleCharts() {
  if (!state.charts.lat) return;
  chartDefaults();
  for (const c of Object.values(state.charts)) {
    c.options.scales.y.grid.color = cssVar("--grid");
    for (const ds of c.data.datasets) {
      if (ds._entity) {
        ds.borderColor = seriesColor(ds._entity);
        ds.backgroundColor = ds.borderColor;
      } else if (ds._var) {
        ds.borderColor = cssVar(ds._var);
        ds.backgroundColor = ds.borderColor;
      }
    }
    c.update("none");
  }
}

function applyMetrics(m) {
  const lat = state.charts.lat;
  const targets = Object.keys(m.ping);
  state.targetsSeen = targets;
  const order = (t) => {
    const st = state.status || {};
    if (t === st.gateway) return 0;
    if (t === "1.1.1.1") return 1;
    if (t === "8.8.8.8") return 2;
    return 3;
  };
  targets.sort((a, b) => order(a) - order(b));
  lat.data.datasets = targets.map((t) => ({
    label: seriesName(t), _entity: t,
    borderColor: seriesColor(t), backgroundColor: seriesColor(t),
    data: m.ping[t].map((p) => ({ x: p.t, y: p.avg })),
    spanGaps: false,
  }));
  lat.update("none");

  const loss = state.charts.loss;
  const internet = targets.filter(
    (t) => t === "1.1.1.1" || t === "8.8.8.8");
  const lossPts = {};
  for (const t of internet) {
    for (const p of m.ping[t]) {
      const a = (lossPts[p.t] ||= { sent: 0, lost: 0, jit: [], t: p.t });
      // reconstruct counts from loss% is lossy; use fields directly
      if (p.loss != null) { a.sent += 100; a.lost += p.loss; }
      if (p.jitter != null) a.jit.push(p.jitter);
    }
  }
  const lossSeries = Object.values(lossPts).sort((a, b) => a.t - b.t);
  loss.data.datasets = [
    { label: "packet loss %", _var: "--crit",
      borderColor: cssVar("--crit"), backgroundColor: cssVar("--crit"),
      data: lossSeries.map((p) => ({
        x: p.t, y: p.sent ? +(p.lost / (p.sent / 100)).toFixed(1) : null })) },
    { label: "jitter ms", _var: "--series-6",
      borderColor: cssVar("--series-6"),
      backgroundColor: cssVar("--series-6"),
      data: lossSeries.map((p) => ({
        x: p.t, y: p.jit.length
          ? +(p.jit.reduce((s, v) => s + v, 0) / p.jit.length).toFixed(1)
          : null })) },
  ];
  loss.update("none");

  const tp = state.charts.tp;
  tp.options.scales.y.title.text = unitLabel();
  tp.data.datasets = [
    { label: "download", _var: "--series-5",
      borderColor: cssVar("--series-5"),
      backgroundColor: cssVar("--series-5"), fill: false,
      data: m.throughput.map((p) => ({ x: p.t, y: +bpsToUnit(p.rx).toFixed(2) })) },
    { label: "upload", _var: "--series-6",
      borderColor: cssVar("--series-6"),
      backgroundColor: cssVar("--series-6"),
      data: m.throughput.map((p) => ({ x: p.t, y: +bpsToUnit(p.tx).toFixed(2) })) },
  ];
  tp.update("none");
}

function applySpeedtests(rows) {
  const st = state.charts.st;
  const ok = rows.filter((r) => r.ok);
  st.options.scales.y.title.text = unitLabel();
  st.data.datasets = [
    { label: "download", _var: "--series-5",
      borderColor: cssVar("--series-5"),
      backgroundColor: cssVar("--series-5"), pointRadius: 4,
      data: ok.map((r) => ({ x: r.ts, y: +mbpsToUnit(r.down_mbps).toFixed(2),
                             _g: r.bufferbloat_grade })) },
    { label: "upload", _var: "--series-6",
      borderColor: cssVar("--series-6"),
      backgroundColor: cssVar("--series-6"), pointRadius: 4,
      data: ok.map((r) => ({ x: r.ts, y: +mbpsToUnit(r.up_mbps).toFixed(2) })) },
  ];
  st.options.plugins.tooltip.callbacks = {
    title: (items) => items.length
      ? new Date(items[0].parsed.x * 1000).toLocaleString() : "",
    afterLabel: (item) => item.raw._g ? `bufferbloat: ${item.raw._g}` : "",
  };
  st.update("none");
}

/* ---------------------------------------------------------------- timeline */

function drawTimeline() {
  const tl = state.timelineData;
  const cv = $("timeline");
  const ctx = cv.getContext("2d");
  const W = cv.width = cv.clientWidth * devicePixelRatio;
  const H = cv.height = 64 * devicePixelRatio;
  ctx.clearRect(0, 0, W, H);
  if (!tl || !tl.cells.length) return;
  const t0 = tl.cells[0].t, t1 = tl.cells[tl.cells.length - 1].t + tl.bucket;
  const px = (t) => ((t - t0) / (t1 - t0)) * W;
  for (const c of tl.cells) {
    let color = cssVar("--idle");
    if (c.state === "ok") color = cssVar("--ok");
    else if (c.state === "degraded") color = cssVar("--warn");
    else if (c.state && c.state.startsWith("outage:")) {
      color = cssVar(LAYER_COLORS[c.state.slice(7)] || "--lay-link");
    }
    ctx.fillStyle = color;
    ctx.fillRect(px(c.t), H * 0.15,
                 Math.max(1, px(c.t + tl.bucket) - px(c.t) - 1), H * 0.7);
  }
  // label bands
  const bands = $("labelBands");
  bands.innerHTML = "";
  for (const lb of tl.labels) {
    const s = Math.max(lb.start_ts, t0);
    const e = Math.min(lb.end_ts || t1, t1);
    if (e <= s) continue;
    const div = document.createElement("div");
    div.className = "label-band";
    div.style.left = (100 * (s - t0) / (t1 - t0)) + "%";
    div.style.width = (100 * (e - s) / (t1 - t0)) + "%";
    div.textContent = lb.label;
    div.title = lb.label;
    bands.appendChild(div);
  }
  cv._t0 = t0; cv._t1 = t1;
}

function timelineClick(ev) {
  const cv = $("timeline");
  const tl = state.timelineData;
  if (!tl || cv._t0 == null) return;
  const frac = ev.offsetX / cv.clientWidth;
  const t = cv._t0 + frac * (cv._t1 - cv._t0);
  const hit = tl.outages.find((o) =>
    o.start_ts <= t && (o.end_ts == null || o.end_ts >= t));
  showDrillin(hit || null, t);
}

function showDrillin(outage, t) {
  const d = $("drillin");
  if (!outage) { d.hidden = true; return; }
  const dur = (outage.end_ts || Date.now() / 1000) - outage.start_ts;
  let html = `<h4>Outage — ${LAYER_NAMES[outage.layer] || outage.layer}</h4>
    <div>${new Date(outage.start_ts * 1000).toLocaleString()} ·
      lasted ${outage.end_ts ? fmtDur(dur) : "ongoing (" + fmtDur(dur) + ")"} ·
      worst loss ${outage.worst_loss_pct != null
        ? outage.worst_loss_pct.toFixed(0) + "%" : "?"} ·
      location: ${outage.label_at_time || "unlabeled"}
      ${outage.self_saturated
        ? " · <strong>this PC was saturating the link</strong>" : ""}</div>`;
  if (outage.traceroute) {
    html += `<span class="hops">${outage.traceroute.map((h) =>
      `${String(h.hop).padStart(2)}  ${h.ip || "*"}  ${
        h.ms != null ? h.ms + " ms" : "timeout"}`).join("\n")}</span>`;
  }
  html += ` <button class="btn btn-small" onclick="this.closest('.drillin').hidden=true">close</button>`;
  d.innerHTML = html;
  d.hidden = false;
}

/* ---------------------------------------------------------------- sparks */

function drawSpark(cv, data, varName) {
  const ctx = cv.getContext("2d");
  const W = cv.width = cv.clientWidth * devicePixelRatio;
  const H = cv.height = 36 * devicePixelRatio;
  ctx.clearRect(0, 0, W, H);
  if (data.length < 2) return;
  const max = Math.max(...data, 1);
  ctx.strokeStyle = cssVar(varName);
  ctx.lineWidth = 2 * devicePixelRatio;
  ctx.beginPath();
  data.forEach((v, i) => {
    const x = (i / (data.length - 1)) * W;
    const y = H - (v / max) * (H - 4) - 2;
    i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
  });
  ctx.stroke();
}
function drawSparks() {
  drawSpark($("sparkDl"), state.sparkDl, "--series-5");
  drawSpark($("sparkUl"), state.sparkUl, "--series-6");
}

/* ---------------------------------------------------------------- live */

function onStatus(s) {
  state.status = s;
  state.lastSSE = Date.now();
  $("reconnectBanner").hidden = true;

  $("machineName").textContent = s.machine_name || MACHINE_FALLBACK;
  const mon = $("monState");
  // dot + word; the word (.mon-txt) is hidden on small screens to save room
  mon.innerHTML = (s.monitoring ? "●" : "⏸")
    + ' <span class="mon-txt">' + (s.monitoring ? "recording" : "paused")
    + "</span>";
  mon.className = "mon-state " + (s.monitoring ? "rec" : "paused");
  $("pausedBanner").hidden = !!s.monitoring;
  if ($("monToggle").getAttribute("aria-busy") !== "true")
    updateMonLabel();  // guarded so it doesn't wipe the spinner

  $("connIcon").textContent = s.conn_type === "wifi" ? "📶" : "🔌";
  let name = s.conn_type === "wifi" ? (s.ssid || "WiFi") : (s.adapter || "…");
  if (s.link_mbps) name += ` · ${s.link_mbps >= 1000
    ? (s.link_mbps / 1000) + " Gbps" : s.link_mbps + " Mbps"}`;
  if (s.conn_type === "wifi" && s.wifi_signal_pct != null)
    name += ` · ${s.wifi_signal_pct | 0}%`;
  $("connName").textContent = name;
  $("connBadge").classList.toggle("down", s.is_up === false || !!s.outage);
  $("labelText").textContent = s.label || "no label";
  $("labelChip").title = s.label
    ? `Location label — tags all recordings since ${
        s.label_since ? new Date(s.label_since * 1000).toLocaleString() : "?"}`
    : "No location label set — tap ✎ to tag recordings from now on";

  $("dlNow").innerHTML = fmtSpeed(s.rx_bps).replace(/ (\S+)$/, " <small>$1</small>");
  $("ulNow").innerHTML = fmtSpeed(s.tx_bps).replace(/ (\S+)$/, " <small>$1</small>");
  state.sparkDl.push(s.rx_bps || 0); if (state.sparkDl.length > 60) state.sparkDl.shift();
  state.sparkUl.push(s.tx_bps || 0); if (state.sparkUl.length > 60) state.sparkUl.shift();
  drawSparks();

  const q = $("quality");
  q.textContent = s.quality != null ? s.quality : "–";
  q.className = "big " + (s.quality == null ? ""
    : s.quality >= 80 ? "good" : s.quality >= 50 ? "meh" : "bad");
  const lat = s.latency && Object.entries(s.latency)
    .filter(([t]) => t === "1.1.1.1" || t === "8.8.8.8")
    .map(([, v]) => v).filter((v) => v != null);
  $("qualitySub").textContent =
    `${lat && lat.length ? Math.min(...lat).toFixed(0) + " ms" : "–"} · ` +
    `jitter ${s.jitter_ms != null ? s.jitter_ms.toFixed(0) + " ms" : "–"} · ` +
    `loss ${s.loss_pct != null ? s.loss_pct.toFixed(0) + "%" : "–"}`;

  if (s.last_speedtest) {
    const t = s.last_speedtest;
    $("lastSpeed").textContent =
      `↓ ${fmtSpeedMbps(t.down_mbps)} / ↑ ${fmtSpeedMbps(t.up_mbps)}`;
    $("lastSpeedSub").textContent =
      `${new Date(t.ts * 1000).toLocaleString()}` +
      (t.bufferbloat_grade ? ` · bufferbloat ${t.bufferbloat_grade}` : "");
  }
  $("runSpeed").disabled = !!s.speedtest_running;
  $("runSpeed").textContent = s.speedtest_running ? "Running…" : "Run test";

  const badge = $("alertBadge");
  if (badge) {
    const n = s.active_alerts || 0;
    badge.textContent = n;
    badge.hidden = n === 0;
  }

  // live favicon + title: paused / hard outage / degraded / quality tiers
  const fs = wtbFavicon.state(s);
  document.title = fs.key === "paused"
    ? `⏸ paused — ${APP_NAME}`
    : `${fs.emoji} ↓${fmtSpeed(s.rx_bps)} ↑${fmtSpeed(s.tx_bps)} — ${APP_NAME}`;
  wtbFavicon.apply(fs);

  // peers
  const peers = s.peers || [];
  const sw = $("peerSwitch");
  if (peers.length && sw.options.length !== peers.length + 1) {
    sw.innerHTML = "<option value=''>this machine</option>" + peers.map(
      (p) => `<option value="${p}">${p.replace(/^https?:\/\//, "")}</option>`)
      .join("");
    sw.hidden = false;
    if (sw._drop) sw._drop.rebuild();
  }
}

/* favicon state/drawing lives in favicon.js (shared with logs.html) */

function connectSSE() {
  const es = new EventSource("/api/stream");
  es.onmessage = (ev) => {
    state.esBackoff = 1000;
    onStatus(JSON.parse(ev.data));
  };
  es.onerror = () => {
    es.close();
    $("reconnectBanner").hidden = false;
    setTimeout(() => {
      state.esBackoff = Math.min(state.esBackoff * 1.7, 30000);
      connectSSE();
    }, state.esBackoff);
  };
  es.onopen = () => {
    // backfill whatever we missed while disconnected
    if (state.lastSSE && Date.now() - state.lastSSE > 10000) refreshAll();
  };
}

/* ---------------------------------------------------------------- history */

async function refreshMetrics() {
  const { from, to } = rangeWindow();
  const qs = `?from=${from}&to=${to}`;
  const [m, tl, sts] = await Promise.all([
    api("/api/metrics" + qs), api("/api/timeline" + qs),
    api("/api/speedtests" + qs)]);
  applyMetrics(m);
  state.timelineData = tl;
  drawTimeline();
  applySpeedtests(sts);
  renderOutages(tl.outages);
  const o = tl.outages.length;
  $("timelineSummary").textContent = o
    ? `${o} outage${o > 1 ? "s" : ""} in range — longest ${
        fmtDur(Math.max(...tl.outages.map((x) =>
          (x.end_ts || Date.now() / 1000) - x.start_ts)))}. Tap the ribbon to inspect.`
    : "No outages in the selected range.";
}

async function refreshInsights() {
  const { from, to } = rangeWindow();
  const ins = await api(`/api/insights?from=${from}&to=${to}`);
  $("statTiles").innerHTML = [
    [ins.uptime_pct != null ? ins.uptime_pct + "%" : "–", "uptime"],
    [ins.outage_count, "outages"],
    [ins.longest_outage || "–", "longest"],
    [ins.avg_outage || "–", "average"],
    [ins.monitored_hours + "h", "monitored"],
    [ins.self_saturated_count, "self-inflicted?"],
  ].map(([v, k]) =>
    `<div class="stat"><div class="v">${v}</div><div class="k">${k}</div></div>`)
    .join("");
  $("verdict").textContent = ins.verdict;

  $("labelTable").querySelector("tbody").innerHTML =
    ins.label_comparison.map((l) =>
      `<tr><td>${esc(l.label)}</td><td>${l.hours_monitored}</td>
       <td>${l.drops_per_day ?? "–"}</td><td>${l.uptime_pct}</td></tr>`)
      .join("") || "<tr><td colspan=4>no labels yet — set one from the header</td></tr>";

  renderHeatmap(ins.heatmap);
}

function renderHeatmap(heat) {
  const days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
  const max = Math.max(1, ...heat.flat());
  let html = "<div></div>";
  for (let h = 0; h < 24; h++)
    html += `<div class="hm-hour">${h % 6 === 0 ? h : ""}</div>`;
  heat.forEach((row, d) => {
    html += `<div class="hm-lab">${days[d]}</div>`;
    row.forEach((v, h) => {
      const alpha = v ? 0.15 + 0.85 * (v / max) : 0;
      html += `<div class="hm-cell" title="${days[d]} ${h}:00 — ${
        v.toFixed(1)} outage min" style="${
        v ? `background:color-mix(in srgb, var(--crit) ${
          Math.round(alpha * 100)}%, var(--surface-2))` : ""}"></div>`;
    });
  });
  $("heatmap").innerHTML = html;
}

function esc(s) {
  return String(s).replace(/[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;",
              '"': "&quot;", "'": "&#39;" }[c]));
}

function renderOutages(outs) {
  $("outageTable").querySelector("tbody").innerHTML = outs.map((o, i) => {
    const dur = (o.end_ts || Date.now() / 1000) - o.start_ts;
    return `<tr class="clickable" data-i="${i}">
      <td>${new Date(o.start_ts * 1000).toLocaleString()}</td>
      <td>${o.end_ts ? fmtDur(dur) : "ongoing"}</td>
      <td><span class="layer-chip"><i class="sw" style="background:var(${
        LAYER_COLORS[o.layer] || "--crit"})"></i>${
        LAYER_NAMES[o.layer] || o.layer}</span></td>
      <td>${o.worst_loss_pct != null ? o.worst_loss_pct.toFixed(0) + "%" : "–"}</td>
      <td>${esc(o.label_at_time || "–")}</td></tr>`;
  }).join("") || "<tr><td colspan=5>none 🎉</td></tr>";
  $("outageTable").querySelectorAll("tr.clickable").forEach((tr) => {
    tr.onclick = () => {
      showDrillin(outs[+tr.dataset.i]);
      $("drillin").scrollIntoView({ behavior: "smooth", block: "center" });
    };
  });
}

/* ------------------------------------------------------------ live toggle */

function updateMonLabel() {
  $("monToggle").textContent =
    state.status && state.status.monitoring === false
      ? "▶ Start monitoring" : "⏸ Pause monitoring";
}

function setLive(on) {
  state.live = on;
  const b = $("liveToggle");
  b.textContent = on ? "⏸ pause live" : "▶ resume live";
  b.setAttribute("aria-pressed", String(on));
  b.classList.toggle("paused", !on);
  if (on) refreshAll();
}

/* complete drops (no degraded), always the latest 50 regardless of the
   selected chart range */
async function refreshDropLog() {
  const outs = await api(`/api/outages?from=0&to=${Date.now() / 1000}`);
  const drops = outs.filter((o) => o.layer !== "degraded").slice(0, 50);
  $("dropTable").querySelector("tbody").innerHTML = drops.map((o) => {
    const end = o.end_ts || Date.now() / 1000;
    return `<tr><td>${fmtLog(o.start_ts)}</td>
      <td>${o.end_ts ? fmtLog(o.end_ts) : "ongoing"}</td>
      <td>${fmtDur(end - o.start_ts)}</td></tr>`;
  }).join("") || "<tr><td colspan=3>no complete drops recorded 🎉</td></tr>";
}

/* ---------------------------------------------------------------- alerts */

// metric metadata: label, unit kind, direction, whether it has a duration
const ALERT_METRICS = {
  latency_ms:     { label: "Latency above", unit: "ms", dir: "above",
                    live: true },
  loss_pct:       { label: "Packet loss above", unit: "%", dir: "above",
                    live: true },
  quality:        { label: "Quality below", unit: "score", dir: "below",
                    live: true },
  speedtest_down: { label: "Speed-test download below", unit: "speed",
                    dir: "below", live: false },
  speedtest_up:   { label: "Speed-test upload below", unit: "speed",
                    dir: "below", live: false },
};

function alertUnitLabel(metric) {
  const m = ALERT_METRICS[metric];
  return m.unit === "speed" ? unitLabel() : m.unit;
}

function ruleText(r) {
  const m = ALERT_METRICS[r.metric] || { label: r.metric, unit: "" };
  const thr = m.unit === "speed"
    ? mbpsToUnit(r.threshold).toFixed(unitDef().dec) + " " + unitLabel()
    : `${r.threshold} ${m.unit === "score" ? "" : m.unit}`.trim();
  return m.live && r.duration_s
    ? `${m.label} ${thr} for ${r.duration_s}s`
    : `${m.label} ${thr}`;
}

async function refreshAlerts() {
  const [events, rules] = await Promise.all([
    api("/api/alerts/events?limit=50"), api("/api/alerts/rules")]);

  // events table on the dashboard
  const now = Date.now() / 1000;
  $("alertTable").querySelector("tbody").innerHTML = events.map((e) => {
    const m = ALERT_METRICS[e.metric] || {};
    const val = m.unit === "speed"
      ? fmtSpeedMbps(e.value)
      : `${e.value != null ? (+e.value).toFixed(m.unit === "ms"
          || m.unit === "score" ? 0 : 1) : "–"}${
          m.unit && m.unit !== "score" && m.unit !== "speed" ? " " + m.unit : ""}`;
    const dur = e.end_ts ? fmtDur(e.end_ts - e.start_ts)
      : (e.metric.startsWith("speedtest") ? "—" : "ongoing");
    return `<tr><td>${fmtLog(e.start_ts)}</td>
      <td>${esc(e.message || e.metric)}</td>
      <td>${val}</td><td>${dur}</td>
      <td><button class="row-x" data-ev="${e.id}"
        aria-label="Delete alert">✕</button></td></tr>`;
  }).join("") || "<tr><td colspan=5 class='muted'>No alerts logged. Add a "
    + "threshold in Settings → Alerts.</td></tr>";
  $("clearAlerts").hidden = events.length === 0;
  $("alertTable").querySelectorAll(".row-x").forEach((b) => {
    b.onclick = () => apiAuth(`/api/alerts/events/${b.dataset.ev}`,
      { method: "DELETE" }).then(refreshAlerts).catch(alertErr);
  });

  // rules list in the settings drawer
  $("alertRules").innerHTML = rules.map((r) =>
    `<div class="alert-rule"><span>${esc(ruleText(r))}</span>
      <button class="x" data-rule="${r.id}"
        aria-label="Delete rule">✕</button></div>`).join("")
    || "<p class='hint'>No alert rules yet.</p>";
  $("alertRules").querySelectorAll(".x").forEach((b) => {
    b.onclick = () => apiAuth(`/api/alerts/rules/${b.dataset.rule}`,
      { method: "DELETE" }).then(refreshAlerts).catch(alertErr);
  });
}

function syncAlertForm() {
  const metric = $("alertMetric").value;
  const m = ALERT_METRICS[metric] || {};
  $("alertUnit").textContent = alertUnitLabel(metric);
  $("alertForWrap").style.display = m.live ? "" : "none";
}

function wireAlerts() {
  $("alertMetric").onchange = syncAlertForm;
  syncAlertForm();

  $("addAlert").onclick = () => {
    const metric = $("alertMetric").value;
    const m = ALERT_METRICS[metric];
    const raw = parseFloat($("alertThreshold").value);
    if (!isFinite(raw) || raw < 0) {
      $("alertAddErr").textContent = "Enter a threshold value."; return;
    }
    // speed thresholds are entered in the display unit; store canonical Mbps
    const threshold = m.unit === "speed"
      ? raw * unitDef().factor / 1e6 : raw;
    const body = { metric, threshold };
    if (m.live) body.duration_s = Math.max(0,
      parseInt($("alertDuration").value, 10) || 0);
    withBusy($("addAlert"), () => apiAuth("/api/alerts/rules", {
      method: "POST", body: JSON.stringify(body),
    })).then(() => {
      $("alertThreshold").value = "";
      $("alertAddErr").textContent = "";
      refreshAlerts();
    }).catch((e) => { $("alertAddErr").textContent = "Couldn't add rule."; });
  };

  $("clearAlerts").onclick = () => {
    if (!confirm("Delete all logged alerts?")) return;
    withBusy($("clearAlerts"), () => apiAuth("/api/alerts/events",
      { method: "DELETE" })).then(refreshAlerts).catch(alertErr);
  };

  api("/api/alerts/config").then((c) => {
    const r = $("alertRetention");
    r.value = String(c.retention_days);
    if (r._drop) r._drop.sync();
  }).catch(() => {});
  $("alertRetention").onchange = (ev) =>
    apiAuth("/api/alerts/config", { method: "POST",
      body: JSON.stringify({ retention_days: +ev.target.value }) })
      .catch(alertErr);
}

function refreshAll() {
  refreshMetrics().catch(() => {});
  refreshInsights().catch(() => {});
  refreshDropLog().catch(() => {});
  refreshAlerts().catch(() => {});
}

/* ---------------------------------------------------------------- actions */

function wireActions() {
  document.querySelectorAll(".range-buttons button").forEach((b) => {
    b.onclick = () => {
      document.querySelector(".range-buttons .active")
        ?.classList.remove("active");
      b.classList.add("active");
      state.range = b.dataset.range === "all" ? null : +b.dataset.range;
      refreshAll();
    };
  });

  $("timeline").onclick = timelineClick;
  $("liveToggle").onclick = () => setLive(!state.live);

  // legend tooltips: tap/click or focus+Enter — works on mobile where
  // hover doesn't exist
  const tip = $("legendTip");
  document.querySelectorAll(".tl-legend button").forEach((b) => {
    b.setAttribute("aria-expanded", "false");
    b.onclick = () => {
      const open = !tip.hidden && tip.dataset.for === b.textContent;
      document.querySelectorAll(".tl-legend button").forEach(
        (x) => x.setAttribute("aria-expanded", "false"));
      if (open) { tip.hidden = true; return; }
      tip.textContent = b.dataset.tip;
      tip.dataset.for = b.textContent;
      tip.hidden = false;
      b.setAttribute("aria-expanded", "true");
    };
  });
  document.addEventListener("click", (ev) => {
    if (!ev.target.closest(".tl-legend")) tip.hidden = true;
  });

  // Cancel buttons in dialogs are type=button (so Enter submits the primary,
  // not Cancel); close the dialog with a cancel result on click.
  document.addEventListener("click", (ev) => {
    const b = ev.target.closest("[data-dismiss]");
    if (b) { const d = b.closest("dialog"); if (d) d.close("cancel"); }
  });

  const openDrawer = () => {
    $("drawer").hidden = false;
    $("drawerBackdrop").hidden = false;
    document.body.classList.add("drawer-open");  // lock page scroll
  };
  const closeDrawer = () => {
    $("drawer").hidden = true;
    $("drawerBackdrop").hidden = true;
    document.body.classList.remove("drawer-open");
  };
  $("settingsBtn").onclick = openDrawer;
  $("closeDrawer").onclick = closeDrawer;
  $("drawerBackdrop").onclick = closeDrawer;
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !$("drawer").hidden) closeDrawer();
  });

  $("latInfo").onclick = () => {
    const open = $("latTip").hidden;
    $("latTip").hidden = !open;
    $("latInfo").setAttribute("aria-expanded", String(open));
  };
  const themeSel = $("themeSelect");
  if (themeSel) {
    if (!themeSel.options.length)
      THEMES.forEach((t) => themeSel.add(new Option(t.label, t.id)));
    themeSel.value = document.documentElement.getAttribute("data-theme") || "";
    themeSel.onchange = () => setTheme(themeSel.value);
  }

  if ($("unitSelect")) {
    $("unitSelect").value = state.unit;
    $("unitSelect").onchange = (ev) => setUnit(ev.target.value);
  }

  wireAlerts();

  document.querySelectorAll("[data-act]").forEach((b) => {
    b.onclick = () => withBusy(b, async () => {
      const act = b.dataset.act === "resume" ? "start" : b.dataset.act;
      await apiAuth(`/api/monitor/${act}`, { method: "POST" });
    }).catch(alertErr);
  });

  // single toggle in the settings drawer: acts on the current state and
  // stays busy until the SSE stream confirms the state actually flipped
  const waitFor = (pred, timeout = 6000) => new Promise((res) => {
    const t0 = Date.now();
    const iv = setInterval(() => {
      if (pred() || Date.now() - t0 > timeout) { clearInterval(iv); res(); }
    }, 150);
  });
  $("monToggle").onclick = () => {
    const target = !(state.status && state.status.monitoring);
    withBusy($("monToggle"), async () => {
      await apiAuth(`/api/monitor/${target ? "start" : "pause"}`,
                    { method: "POST" });
      await waitFor(() =>
        !!(state.status && state.status.monitoring) === target);
    }).catch(alertErr)
      // spinner's gone; put the right caption back right now instead of
      // waiting up to 2s for the next SSE tick
      .finally(updateMonLabel);
  };

  $("runSpeed").onclick = () => withBusy($("runSpeed"), () =>
    apiAuth("/api/speedtest/run", { method: "POST" })).catch(alertErr);

  $("editLabel").onclick = () => {
    const dlg = $("labelDlg");
    const st = state.status || {};
    $("labelCurrent").textContent = st.label
      ? `Current: “${st.label}” — in use since ${
          st.label_since
            ? new Date(st.label_since * 1000).toLocaleString() : "?"}.`
      : "No label yet — data recorded so far is unlabeled.";
    $("labelInput").value = state.status?.label || "";
    $("labelQuick").innerHTML = (state.status?.known_labels || []).map(
      (l) => `<button type="button">${esc(l)}</button>`).join("");
    $("labelQuick").querySelectorAll("button").forEach((qb) => {
      qb.onclick = () => { $("labelInput").value = qb.textContent; };
    });
    dlg.showModal();
    $("labelForm").onsubmit = (ev) => {
      if (ev.submitter && ev.submitter.value === "cancel") return;
      ev.preventDefault();
      withBusy(ev.submitter || dlg.querySelector(".btn-primary"), () =>
        apiAuth("/api/label", { method: "POST",
          body: JSON.stringify({ label: $("labelInput").value }) })
          .then(() => { dlg.close(); refreshAll(); })).catch(alertErr);
    };
  };

  $("addNote").onclick = () => {
    const text = $("noteText").value.trim();
    if (!text) return;
    withBusy($("addNote"), () =>
      apiAuth("/api/note", { method: "POST",
        body: JSON.stringify({ text }) })
        .then(() => { $("noteText").value = ""; })).catch(alertErr);
  };

  $("clearData").onclick = async () => {
    const { from, to } = rangeWindow();
    const span = state.range ? fmtDur(state.range) : "ALL TIME";
    if (!confirm(`Delete all recorded data in the current range (${span})?`))
      return;
    if (!confirm("Really delete? This cannot be undone.")) return;
    await withBusy($("clearData"), () =>
      apiAuth(`/api/data?from=${from}&to=${to}`, { method: "DELETE" }))
      .catch(alertErr);
    refreshAll();
  };

  $("peerSwitch").onchange = (ev) => {
    if (ev.target.value) location.href = ev.target.value;
  };

  // CSV exports: fetch with a spinner, then hand the file to the browser
  [["expOutages", "outages"], ["expSpeed", "speedtests"],
   ["expPings", "pings"]].forEach(([id, table]) => {
    $(id).onclick = (ev) => {
      ev.preventDefault();
      withBusy($(id), async () => {
        const { from, to } = rangeWindow();
        const r = await fetch(
          `/api/export?table=${table}&from=${from}&to=${to}`);
        if (!r.ok) throw new Error("export failed (" + r.status + ")");
        const url = URL.createObjectURL(await r.blob());
        const a = document.createElement("a");
        a.href = url;
        a.download = `gowebgo_${table}.csv`;
        a.click();
        URL.revokeObjectURL(url);
      }).catch(alertErr);
    };
  });

  api("/api/diagnostics").then((d) => {
    $("diagPanel").innerHTML = d.power_saving
      ? `<span class="warn">⚠ ${esc(d.hint)}</span>`
      : d.power_saving === false
        ? "✓ Adapter power-saving is off — good."
        : "Adapter power-saving state unknown on this platform.";
    if (d.driver_version)
      $("diagPanel").innerHTML +=
        `<br>driver ${esc(d.driver_version)} (${esc(d.driver_date || "?")})`;
  }).catch(() => { $("diagPanel").textContent = "diagnostics unavailable"; });

  $("drawerFoot").textContent =
    APP_NAME + " — a personal LAN tool. Data stays on this machine. " +
    "The host machine itself (localhost) is always trusted and can reset " +
    "the passcode.";

  wireSecurity();

  // upgrade every native <select data-drop> to the accessible custom dropdown
  // (options are all populated by now: theme list, unit, alert selects)
  if (window.WtbDrop) window.WtbDrop.enhanceAll();
}

/* ---------------------------------------------------------- security UI */

async function refreshAuthState() {
  try {
    state.auth = await api("/api/auth/state");
  } catch { /* keep last known */ }
  renderSecurity();
}

function renderSecurity() {
  const a = state.auth;
  const panel = $("securityPanel");
  if (!panel) return;
  const su = a.superuser
    ? " You're on the host machine (localhost): you don't need the passcode to"
      + " view or control things, and only you can reset it."
    : "";
  const main = a.enabled
    ? "🔒 <strong>Protected</strong> — other devices on your network must"
      + " enter the passcode to view or change anything."
    : "🔓 <strong>Open</strong> — anyone on your network can view and change"
      + " everything. Turn on protection to require a passcode.";
  panel.innerHTML = main + su;
  const toggle = $("toggleAuth");
  toggle.textContent = a.enabled ? "Disable protection" : "Enable protection";
  toggle.classList.toggle("btn-danger", a.enabled);
  // superuser: Reset (or Set, first time). Remote: Change (needs current PIN).
  $("changePin").textContent = a.superuser
    ? (a.configured ? "Reset passcode" : "Set passcode") : "Change passcode";
  // remote devices can only manage the passcode while logged in (protected)
  $("changePin").hidden = !a.superuser && !a.enabled;
}

/* Passcode dialog. opts: {title, hint, needCurrent, needNew, currentLabel,
   newLabel, errText, validateNew}. Resolves {current, value} or null. */
function pinDialog(opts) {
  return new Promise((resolve) => {
    const dlg = $("pinDlg");
    if (dlg.open) return resolve(null);
    $("pinTitle").textContent = opts.title;
    $("pinHint").textContent = opts.hint || "";
    $("pinErr").textContent = opts.errText || "";
    $("pinCurrent").hidden = !opts.needCurrent;
    $("pinCurrent").placeholder = opts.currentLabel || "current passcode";
    $("pinNew").hidden = !opts.needNew;
    $("pinNew").placeholder = opts.newLabel || "passcode";
    $("pinCurrent").value = $("pinNew").value = "";
    dlg.returnValue = "";
    dlg.showModal();
    setTimeout(() => {
      (opts.needCurrent ? $("pinCurrent") : $("pinNew")).focus();
    }, 30);
    $("pinForm").onsubmit = (ev) => {
      ev.preventDefault();
      const cur = $("pinCurrent").value, val = $("pinNew").value;
      if (opts.needCurrent && !cur) {
        $("pinErr").textContent = "Enter your current passcode."; return;
      }
      if (opts.validateNew && !/^\d{4,}$/.test(val)) {
        $("pinErr").textContent = "Passcode must be at least 4 digits."; return;
      }
      if (!val) { $("pinErr").textContent = "Enter your passcode."; return; }
      dlg.returnValue = "ok";
      dlg._vals = { current: cur, value: val };
      dlg.close("ok");
    };
    dlg.onclose = () =>
      resolve(dlg.returnValue === "ok" ? dlg._vals : null);
  });
}

/* Prompt for a passcode and submit it, re-prompting on a wrong passcode (403)
   or too-short PIN (400). Returns true on success, false on cancel. */
async function confirmPasscode(opts, submit) {
  let errText = "";
  while (true) {
    const v = await pinDialog(Object.assign({ errText }, opts));
    if (!v) return false;
    try {
      await submit(v);
      return true;
    } catch (e) {
      if (e.status === 403) { errText = "Wrong passcode. Try again."; continue; }
      if (e.status === 400) {
        errText = "Passcode must be at least 4 digits."; continue;
      }
      throw e;
    }
  }
}

function securityMsg(text) {
  const el = $("securityMsg");
  if (!el) return;
  el.textContent = text;
  clearTimeout(securityMsg._t);
  securityMsg._t = setTimeout(() => { el.textContent = ""; }, 8000);
}

function wireSecurity() {
  // Set / Reset (superuser) or Change (remote) the passcode.
  $("changePin").onclick = async () => {
    const a = state.auth;
    const remote = !a.superuser;
    const ok = await confirmPasscode({
      title: a.superuser ? (a.configured ? "Reset passcode" : "Set passcode")
        : "Change passcode",
      hint: remote
        ? "Enter your current passcode and a new one (4+ digits)."
        : "Choose a new passcode (4+ digits). This becomes the passcode other "
          + "devices must enter.",
      needCurrent: remote, needNew: true, validateNew: true,
      newLabel: "new passcode (4+ digits)",
    }, (v) => apiAuth("/api/auth/change", {
      method: "POST",
      body: JSON.stringify({ current: v.current, new: v.value }),
    }));
    if (!ok) return;
    await refreshAuthState();
    securityMsg(a.configured ? "✓ Passcode reset." : "✓ Passcode set.");
  };

  // Enable / disable protection — always confirm with the passcode.
  $("toggleAuth").onclick = async () => {
    const a = state.auth;
    if (a.enabled) {
      const ok = await confirmPasscode({
        title: "Disable protection",
        hint: "Enter your passcode to turn protection off. Other devices will "
          + "then have full access.",
        needNew: true, newLabel: "passcode",
      }, (v) => apiAuth("/api/auth/disable", {
        method: "POST", body: JSON.stringify({ passcode: v.value }),
      }));
      if (!ok) return;
      await refreshAuthState();
      securityMsg("✓ Protection turned off.");
    } else {
      const firstTime = !a.configured;
      const ok = await confirmPasscode({
        title: "Enable protection",
        hint: firstTime
          ? "Set a passcode (4+ digits). Other devices will need it to view or "
            + "change anything."
          : "Enter your passcode to turn protection on.",
        needNew: true, validateNew: firstTime,
        newLabel: firstTime ? "new passcode (4+ digits)" : "passcode",
      }, (v) => apiAuth("/api/auth/enable", {
        method: "POST", body: JSON.stringify({ passcode: v.value }),
      }));
      if (!ok) return;
      await refreshAuthState();
      securityMsg("✓ Protection is on — other devices need the passcode.");
    }
  };
}

/* show a spinner inside a control while its action runs */
async function withBusy(el, fn) {
  if (el.getAttribute("aria-busy") === "true") return;
  const sp = document.createElement("span");
  sp.className = "spin";
  el.setAttribute("aria-busy", "true");
  el.prepend(sp);
  try {
    return await fn();
  } finally {
    sp.remove();
    el.removeAttribute("aria-busy");
  }
}

function alertErr(e) {
  console.error(e);
  alert("Action failed: " + (e.message || e));
}

/* ---------------------------------------------------------------- boot */

window.addEventListener("resize", () => { drawTimeline(); drawSparks(); });

function startDashboard() {
  makeCharts();
  wireActions();
  connectSSE();
  refreshAll();
  refreshAuthState();
  setInterval(() => { if (state.live) refreshMetrics().catch(() => {}); }, 30000);
  setInterval(() => {
    refreshInsights().catch(() => {});
    refreshDropLog().catch(() => {});
    refreshAlerts().catch(() => {});
  }, 60000);
}

async function boot() {
  // theme is already applied pre-paint by theme/theme-init.js; the selector is
  // populated + synced in wireActions()
  try {
    state.auth = await api("/api/auth/state");
  } catch { /* if this fails we'll surface it once the dashboard loads */ }
  // when locked and not the host, block on the passcode before showing data
  if (state.auth.enabled && !state.auth.authed && !state.auth.superuser) {
    await showLogin(true);
    try { state.auth = await api("/api/auth/state"); } catch { /* noop */ }
  }
  startDashboard();
}

boot();
setInterval(() => {
  if (Date.now() - state.lastSSE > 8000 && state.lastSSE)
    $("reconnectBanner").hidden = false;
}, 4000);
