// Avalon Monitor dashboard. Zero-build ES module: fleet view, host detail, live WebSocket.
import { TimeChart, drawSparkline } from "./charts.js?v=1.2.0";

// ------------------------------------------------------------------ utils
const $ = (sel, root = document) => root.querySelector(sel);
const h = (tag, attrs = {}, ...children) => {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") el.className = v;
    else if (k === "html") el.innerHTML = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (v != null) el.setAttribute(k, v);
  }
  for (const c of children.flat()) if (c != null) el.append(c.nodeType ? c : document.createTextNode(String(c)));
  return el;
};
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const cssVar = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();

export const fmt = {
  pct: (v, d = 0) => (v == null ? "—" : v.toFixed(d) + "%"),
  bytes: (b, d = 1) => {
    if (b == null) return "—";
    const u = ["B", "KB", "MB", "GB", "TB", "PB"]; let i = 0; let v = b;
    while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
    return (i === 0 ? v.toFixed(0) : v.toFixed(v >= 100 ? 0 : i >= 4 ? Math.max(d, 1) : d)) + " " + u[i];
  },
  bps: (b) => {
    if (b == null) return "—";
    const u = ["B/s", "KB/s", "MB/s", "GB/s"]; let i = 0; let v = b;
    while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
    return (i === 0 ? v.toFixed(0) : v.toFixed(v >= 100 ? 0 : 1)) + " " + u[i];
  },
  temp: (t) => (t == null ? "—" : Math.round(t) + "°C"),
  watts: (w) => (w == null ? "—" : Math.round(w) + " W"),
  dur: (s) => {
    if (s == null) return "—";
    s = Math.floor(s);
    const d = Math.floor(s / 86400), hh = Math.floor((s % 86400) / 3600), mm = Math.floor((s % 3600) / 60);
    if (d > 0) return `${d}d ${hh}h`;
    if (hh > 0) return `${hh}h ${mm}m`;
    return `${mm}m ${s % 60}s`;
  },
  ago: (age) => {
    if (age == null) return "never";
    if (age < 5) return "just now";
    if (age < 90) return `${Math.round(age)}s ago`;
    if (age < 5400) return `${Math.round(age / 60)}m ago`;
    if (age < 172800) return `${Math.round(age / 3600)}h ago`;
    return `${Math.round(age / 86400)}d ago`;
  },
  load: (l) => (l ? l.map((x) => x.toFixed(2)).join(" · ") : "—"),
};

const ICON = {
  cpu: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="6" y="6" width="12" height="12" rx="2"/><path d="M9 2v3M15 2v3M9 19v3M15 19v3M2 9h3M2 15h3M19 9h3M19 15h3"/></svg>',
  down: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M12 5v14M5 12l7 7 7-7"/></svg>',
  up: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M12 19V5M5 12l7-7 7 7"/></svg>',
  disk: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><ellipse cx="12" cy="6" rx="8" ry="3"/><path d="M4 6v12c0 1.7 3.6 3 8 3s8-1.3 8-3V6"/></svg>',
  temp: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M14 14.8V5a2 2 0 0 0-4 0v9.8a4 4 0 1 0 4 0z"/></svg>',
  bolt: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"><path d="M13 2 3 14h8l-1 8 10-12h-8l1-8z"/></svg>',
  ok: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><path d="M5 13l4 4L19 7"/></svg>',
  bad: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg>',
  back: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M15 6l-6 6 6 6"/></svg>',
};

function levelFor(p, warn = 80, crit = 92) { return p == null ? "" : p >= crit ? "crit" : p >= warn ? "warn" : ""; }
function osLabel(host) { return host?.os_version || host?.os || "unknown"; }

// ------------------------------------------------------------------ state
const state = {
  config: null, hosts: new Map(), range: "1h", view: null, ws: null, live: false,
  spark: new Map(),  // host name -> [[ts, cpu]]
  detail: null,      // { name, charts: {}, ... }
};
const RANGE_ORDER = ["15m", "1h", "6h", "24h", "7d", "30d"];

async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) {
    let msg = r.statusText;
    try { msg = (await r.json()).detail || msg; } catch (_) { /* ignore */ }
    throw new Error(`${r.status} ${msg}`);
  }
  return r.json();
}

let toastTimer;
function toast(msg, err = false) {
  const t = $("#toast"); t.textContent = msg; t.className = "toast show" + (err ? " err" : "");
  clearTimeout(toastTimer); toastTimer = setTimeout(() => (t.className = "toast"), 4000);
}

// ---------------------------------------------------------------- topbar
function renderTopbar() {
  const seg = $("#range-seg"); seg.innerHTML = "";
  for (const r of RANGE_ORDER) {
    seg.append(h("button", { "aria-pressed": String(r === state.range), onclick: () => setRange(r) }, r));
  }
  $("#theme-btn").onclick = () => {
    const cur = document.documentElement.dataset.theme === "light" ? "dark" : "light";
    document.documentElement.dataset.theme = cur;
    try { localStorage.setItem("avm-theme", cur); } catch (_) { /* ignore */ }
    redrawAll();
  };
  if (state.config) {
    const u = state.config.user;
    $("#user").textContent = u.method === "access" ? u.subject : "tailnet · direct";
  }
}

function renderFleetStats() {
  const hosts = [...state.hosts.values()];
  const online = hosts.filter((x) => x.status === "online");
  const cpu = online.map((x) => x.summary?.cpu?.percent).filter((v) => v != null);
  const avgCpu = cpu.length ? cpu.reduce((a, b) => a + b, 0) / cpu.length : null;
  const memUsed = online.reduce((a, x) => a + (x.summary?.memory?.used || 0), 0);
  const memTot = online.reduce((a, x) => a + (x.summary?.memory?.total || 0), 0);
  const gpus = online.flatMap((x) => x.summary?.gpus || []);
  const gpuUtil = gpus.map((g) => g.util_percent).filter((v) => v != null);
  const failing = hosts.reduce((a, x) => a + (x.summary?.checks_failing || 0), 0) + hosts.filter((x) => x.status === "offline").length;
  const stat = (b, s) => h("div", { class: "stat" }, h("b", { class: "num" }, b), h("span", {}, s));
  const el = $("#fleet-stats"); el.innerHTML = "";
  el.append(
    stat(`${online.length} / ${hosts.length}`, "online"),
    stat(fmt.pct(avgCpu), "avg cpu"),
    stat(memTot ? `${fmt.bytes(memUsed, 0)} / ${fmt.bytes(memTot, 0)}` : "—", "memory"),
    stat(gpuUtil.length ? fmt.pct(gpuUtil.reduce((a, b) => a + b, 0) / gpuUtil.length) : "—", `${gpus.length} gpu${gpus.length === 1 ? "" : "s"}`),
    stat(String(failing), failing ? "alerts" : "alerts"),
  );
  el.lastChild.querySelector("b").style.color = failing ? cssVar("--s-crit") : "";
}

// ------------------------------------------------------------------ fleet
function meter(label, cls, value, sub, pct, na = false) {
  const lvl = levelFor(pct);
  return h("div", { class: "meter" + (na ? " na" : ""), "data-m": cls },
    h("div", { class: "lbl" }, h("span", {}, label)),
    h("div", { class: "val num", "data-f": "val" }, value),
    h("div", { class: "sub num", "data-f": "sub" }, sub ?? ""),
    h("div", { class: "meter-track" }, h("div", { class: "meter-fill " + lvl, "data-f": "fill", style: `--c: var(--m-${cls}); width:${pct ?? 0}%` })),
  );
}

function hostCard(host) {
  const s = host.summary || {};
  const card = h("div", { class: `card host-card ${host.status}`, "data-host": host.name, onclick: () => (location.hash = "#/host/" + encodeURIComponent(host.name)) });
  const gpu = s.gpus?.[0];
  card.append(
    h("div", { class: "host-head" },
      h("span", { class: "status-dot " + host.status, "data-f": "dot" }),
      h("div", {},
        h("div", { class: "host-name" }, host.display_name),
        h("div", { class: "host-sub" }, h("span", { "data-f": "os" }, osLabel(s.host)), h("span", { "data-f": "arch" }, s.host?.arch || ""))),
      h("div", { class: "right" }, h("div", { "data-f": "seen" }, ""), h("div", { "data-f": "uptime" }, "")),
    ),
    h("div", { class: "meters" },
      meter("CPU", "cpu", "", "", null),
      meter("Memory", "mem", "", "", null),
      meter("Disk", "disk", "", "", null),
      meter(gpu ? "GPU" : "GPU", "gpu", "", "", null, !gpu),
    ),
    h("canvas", { class: "spark", "data-f": "spark" }),
    h("div", { class: "host-foot", "data-f": "foot" }),
    h("div", { class: "checks", "data-f": "checks" }),
  );
  updateCard(card, host);
  return card;
}

function setMeter(el, value, sub, pct, na = false) {
  el.classList.toggle("na", na);
  $('[data-f="val"]', el).innerHTML = value;
  $('[data-f="sub"]', el).textContent = sub ?? "";
  const fill = $('[data-f="fill"]', el);
  fill.style.width = (pct ?? 0) + "%";
  fill.className = "meter-fill " + levelFor(pct);
}

function updateCard(card, host) {
  const s = host.summary || {};
  card.className = `card host-card ${host.status}`;
  $('[data-f="dot"]', card).className = "status-dot " + host.status;
  $('[data-f="os"]', card).textContent = osLabel(s.host);
  $('[data-f="arch"]', card).textContent = s.host?.arch || "";
  $('[data-f="seen"]', card).textContent = host.status === "online" ? "live" : (host.status === "never" ? "no data yet" : "seen " + fmt.ago(host.age_sec));
  $('[data-f="seen"]', card).style.color = host.status === "offline" ? cssVar("--s-crit") : "";
  $('[data-f="uptime"]', card).textContent = s.host?.uptime_sec != null ? "up " + fmt.dur(s.host.uptime_sec) : "";

  const m = $$m(card);
  setMeter(m.cpu, fmt.pct(s.cpu?.percent), s.cpu?.load ? "load " + s.cpu.load[0].toFixed(2) : (s.cpu?.cores ? s.cpu.cores + " cores" : ""), s.cpu?.percent);
  setMeter(m.mem, fmt.pct(s.memory?.percent), s.memory?.total ? `${fmt.bytes(s.memory.used, 1)} of ${fmt.bytes(s.memory.total, 0)}` : "", s.memory?.percent);
  const dpct = s.disk?.percent ?? s.disk?.max_percent;
  setMeter(m.disk, fmt.pct(dpct), s.disk?.total ? `${fmt.bytes(s.disk.total - s.disk.used, 0)} free` : "", dpct, dpct == null);
  const g = s.gpus?.[0];
  if (g) {
    const extra = g.mem_percent != null ? `vram ${fmt.pct(g.mem_percent)}` : "";
    setMeter(m.gpu, fmt.pct(g.util_percent), extra, g.util_percent);
    $(".lbl span", m.gpu).textContent = s.gpus.length > 1 ? `GPU ×${s.gpus.length}` : "GPU";
  } else setMeter(m.gpu, "—", "no gpu", null, true);

  const foot = $('[data-f="foot"]', card); foot.innerHTML = "";
  const kv = (icon, text, title) => h("span", { class: "kv num", title, html: icon + esc(text) });
  if (s.network?.rx_bps != null) foot.append(kv(ICON.down, fmt.bps(s.network.rx_bps), "network receive"), kv(ICON.up, fmt.bps(s.network.tx_bps), "network transmit"));
  if (s.disk_io?.read_bps != null) foot.append(kv(ICON.disk, `${fmt.bps(s.disk_io.read_bps)} · ${fmt.bps(s.disk_io.write_bps)}`, "disk read · write"));
  const t = s.cpu?.temp_c ?? s.temp_max;
  if (t != null) foot.append(kv(ICON.temp, fmt.temp(t), "cpu temperature"));
  if (g?.temp_c != null) foot.append(kv(ICON.bolt, `${fmt.temp(g.temp_c)}${g.power_w != null ? " · " + fmt.watts(g.power_w) : ""}`, "gpu temperature · power"));
  if (s.battery?.percent != null) foot.append(kv(ICON.bolt, `${Math.round(s.battery.percent)}%${s.battery.plugged ? " ⚡" : ""}`, "battery"));
  if (s.top_process?.name && (s.top_process.cpu_percent || 0) >= 1) foot.append(kv(ICON.cpu, `${s.top_process.name} ${fmt.pct(s.top_process.cpu_percent)}`, "busiest process"));

  const checks = $('[data-f="checks"]', card); checks.innerHTML = "";
  for (const c of s.checks || []) {
    if (c.kind === "content" && c.ok) {
      checks.append(h("span", { class: "chip", title: `${c.name} · updated ${fmt.ago(c.value)}` }, h("b", {}, c.name + ": "), c.detail));
      continue;
    }
    if (c.kind === "job") {
      // active job scopes are informational: show what's running, stay quiet otherwise
      if ((c.value || 0) > 0) checks.append(h("span", { class: "chip", title: c.detail || "" }, h("b", {}, "job: "), c.detail.replace(/^active: /, "")));
      else if (!c.ok) checks.append(h("span", { class: "chip bad", title: c.detail || "", html: ICON.bad.replace("<svg", '<svg width="11" height="11"') + esc(c.name) }));
      continue;
    }
    if (c.kind === "rule" && c.ok) continue;  // hub rules only surface when they fire
    let label = c.name;
    if (c.kind === "queue") label = c.ok ? `${c.name}: ${c.value} pending` : `${c.name}: empty`;
    else if (c.kind === "marker" && !c.ok) label = `${c.value} ${c.name}`;
    else if (c.kind === "unit") label = `${c.name.replace(/\.(service|scope|timer)$/, "")}: ${(c.detail || "").split(":")[0].split(" ")[0].toLowerCase()}`;
    else if (c.kind === "memory" && !c.ok) label = `${c.value} GB free`;
    const cls = c.ok ? (c.kind === "unit" && !(c.value > 0) ? "" : "ok") : (c.level === "warning" ? "warn" : "bad");
    checks.append(h("span", { class: "chip " + cls, title: c.detail || "", html: (c.ok ? ICON.ok : ICON.bad).replace("<svg", '<svg width="11" height="11"') + esc(label) }));
  }
  if (host.agent_update_error) checks.append(h("span", { class: "chip bad", title: host.agent_update_error }, "agent update failed"));
  else if (host.agent_outdated && host.status === "online") checks.append(h("span", { class: "chip warn", title: `running agent ${host.agent_version || "?"}; the hub serves a newer script and the agent updates itself on its next report` }, "agent updating"));
  if (s.memory?.committed != null && s.memory?.commit_limit) {
    const cp = (100 * s.memory.committed) / s.memory.commit_limit;
    if (cp >= 85) checks.append(h("span", { class: "chip warn", title: `committed ${fmt.bytes(s.memory.committed)} of ${fmt.bytes(s.memory.commit_limit)} limit` }, `commit ${Math.round(cp)}%`));
  }
  drawCardSpark(card, host.name);
}
const $$m = (card) => ({ cpu: $('[data-m="cpu"]', card), mem: $('[data-m="mem"]', card), disk: $('[data-m="disk"]', card), gpu: $('[data-m="gpu"]', card) });

function drawCardSpark(card, name) {
  const c = $('[data-f="spark"]', card);
  if (c) drawSparkline(c, state.spark.get(name) || [], cssVar("--m-cpu"), { yMax: 100, span: RANGE_SEC[state.range] });
}

const sparkLoading = new Set();
async function loadSpark(name) {
  if (sparkLoading.has(name)) return;
  sparkLoading.add(name);
  try {
    const range = state.range;
    const d = await api(`/api/v1/hosts/${encodeURIComponent(name)}/series?metrics=cpu&range=${range}&points=240`);
    if (range !== state.range) return;  // range changed while loading
    state.spark.set(name, d.ts.map((t, i) => [t, d.cpu[i]]));
    const card = $(`.host-card[data-host="${CSS.escape(name)}"]`);
    if (card) drawCardSpark(card, name);
  } catch (e) { /* non-fatal */ }
  finally { sparkLoading.delete(name); }
}

function renderFleet() {
  const app = $("#app"); app.innerHTML = "";
  const grid = h("div", { class: "fleet" });
  const hosts = [...state.hosts.values()].sort((a, b) => (a.status === "online" ? 0 : 1) - (b.status === "online" ? 0 : 1) || a.display_name.localeCompare(b.display_name));
  if (!hosts.length) {
    grid.append(h("div", { class: "empty" }, h("div", { html: "No hosts enrolled yet.<br>On AVALON run <code>manage.py add-host &lt;name&gt;</code>, then start the agent on that machine with the printed token." })));
  }
  for (const host of hosts) grid.append(hostCard(host));
  app.append(grid);
  state.view = "fleet";
  for (const host of hosts) if (!state.spark.has(host.name)) loadSpark(host.name); else drawCardSpark($(`.host-card[data-host="${CSS.escape(host.name)}"]`), host.name);
}

// ----------------------------------------------------------------- detail
const CHARTS = [
  { id: "cpu", title: "CPU", yMax: 100, format: (v) => Math.round(v) + "%", series: [{ key: "cpu", label: "CPU", color: "--m-cpu" }, { key: "load1", label: "load (1m)", color: "--c-orange", hidden: true }] },
  { id: "mem", title: "Memory", yMax: 100, format: (v) => Math.round(v) + "%", series: [{ key: "mem", label: "RAM", color: "--m-mem" }, { key: "swap", label: "swap", color: "--c-orange" }] },
  { id: "gpu", title: "GPU", yMax: 100, format: (v) => Math.round(v) + "%", series: [{ key: "gpu_util", label: "utilisation", color: "--m-gpu" }, { key: "gpu_mem", label: "VRAM", color: "--c-orange" }], needs: "gpu" },
  { id: "net", title: "Network", yMax: null, bytes: true, format: (v) => fmt.bps(v), series: [{ key: "net_rx", label: "receive", color: "--c-blue" }, { key: "net_tx", label: "transmit", color: "--c-orange" }] },
  { id: "disk", title: "Disk I/O", yMax: null, bytes: true, format: (v) => fmt.bps(v), series: [{ key: "disk_read", label: "read", color: "--c-blue" }, { key: "disk_write", label: "write", color: "--c-orange" }] },
  { id: "temp", title: "Temperature", yMax: null, format: (v) => Math.round(v) + "°C", series: [{ key: "cpu_temp", label: "CPU", color: "--c-blue" }, { key: "gpu_temp", label: "GPU", color: "--c-orange" }, { key: "temp_max", label: "hottest sensor", color: "--c-aqua" }] },
];
const RANGE_SEC = { "15m": 900, "1h": 3600, "6h": 21600, "24h": 86400, "7d": 604800, "30d": 2592000 };

async function renderDetail(name) {
  const app = $("#app"); app.innerHTML = "";
  state.view = "detail";
  let host;
  try { host = await api(`/api/v1/hosts/${encodeURIComponent(name)}`); }
  catch (e) { app.append(h("div", { class: "empty" }, `Cannot load host "${name}": ${e.message}`)); return; }
  state.hosts.set(host.name, host);
  const s = host.sample || {};
  const hasGpu = (s.gpus || []).length > 0;

  const head = h("div", { class: "detail-head" },
    h("button", { class: "back", onclick: () => (location.hash = "#/") , html: ICON.back + " Fleet" }),
    h("div", { class: "detail-title" }, h("span", { class: "status-dot " + host.status, "data-f": "dot" }), host.display_name),
    h("div", { class: "detail-meta", "data-f": "meta" }),
    h("div", { class: "spacer" }),
    h("button", { class: "btn", title: "Ask the agent to re-exec itself on its next push (picks up a new script/config)", onclick: () => sendCommand(host.name, "respawn") }, "Respawn agent"),
  );
  const kpis = h("div", { class: "kpis", "data-f": "kpis" });
  const charts = h("div", { class: "charts" });
  const chartObjs = {};
  for (const c of CHARTS) {
    if (c.needs === "gpu" && !hasGpu) continue;
    const series = c.series.filter((x) => !x.hidden);
    const legend = series.length > 1 ? h("span", { class: "legend" }, ...series.map((x) => h("span", { html: `<i style="--c: var(${x.color})"></i>${esc(x.label)}` }))) : null;
    const box = h("div", { class: "chart" });
    charts.append(h("div", { class: "card chart-card" }, h("h3", {}, c.title, legend), box));
    chartObjs[c.id] = { def: c, chart: new TimeChart(box, { yMax: c.yMax, bytes: !!c.bytes, format: c.format, window: RANGE_SEC[state.range] }) };
  }
  // per-core panel
  const cores = h("div", { class: "cores", "data-f": "cores" });
  const coresCard = h("div", { class: "card chart-card", style: "min-height: 0" }, h("h3", {}, "Per-core load"), cores);

  const tables = h("div", { class: "tables", "data-f": "tables" });
  const editor = h("div", { class: "card table-card editor", "data-f": "editor" });
  app.append(head, kpis, charts, coresCard, tables, editor);
  state.detail = { name: host.name, charts: chartObjs, els: { head, kpis, cores, tables, editor } };
  updateDetail(host);
  renderEditor(host);
  await loadDetailSeries();
}

// ---------------------------------------------------------- checks editor
const CHECK_KINDS = [
  { key: "AVM_WATCH_PROCESSES", label: "process running", hint: "name or command-line substring, e.g. nginx" },
  { key: "AVM_WATCH_FILES", label: "file fresh", hint: "path:max-age-seconds, e.g. /srv/heartbeat:120" },
  { key: "AVM_WATCH_TCP", label: "tcp port open", hint: "host:port, e.g. 127.0.0.1:11434" },
  { key: "AVM_WATCH_MOUNTS", label: "volume mounted", hint: "mount point, e.g. /mnt/data" },
  { key: "AVM_WATCH_CONTENT", label: "show file line", hint: "small text file, e.g. /srv/queue/current.txt" },
  { key: "AVM_WATCH_MARKERS", label: "no marker files", hint: "glob, or glob::regex for content match" },
  { key: "AVM_WATCH_NONEMPTY", label: "queue not empty", hint: "file that should have pending lines" },
  { key: "AVM_WATCH_UNITS", label: "systemd unit", hint: "[system:|user:]unit, e.g. user:xrsec-queue.service" },
  { key: "AVM_WATCH_JOBS", label: "job scope active", hint: "[user:]glob, e.g. user:xrsec-*.scope" },
];
const CHECK_SINGLE = [
  { key: "AVM_MEM_AVAILABLE_WARN_GB", label: "Warn when free memory below (GB)", hint: "e.g. 8" },
  { key: "AVM_PROCESSES", label: "Top processes reported", hint: "8" },
  { key: "AVM_TAGS", label: "Tags", hint: "comma-separated chips" },
];
const hdrs = () => ({ "Content-Type": "application/json", "X-Requested-With": "avalon-monitor" });

async function sendCommand(name, cmd) {
  try {
    const r = await api(`/api/v1/hosts/${encodeURIComponent(name)}/command/${cmd}`, { method: "POST", headers: hdrs() });
    toast(`${cmd} queued for ${name} - ${r.note}`);
  } catch (e) { toast(`Failed: ${e.message}`, true); }
}

function renderEditor(host) {
  const d = state.detail; if (!d || !d.els.editor) return;
  const el = d.els.editor; el.innerHTML = "";
  const cfg = host.check_config || {};
  const rows = [];
  for (const k of CHECK_KINDS) for (const v of (cfg[k.key] || "").split(",").map((x) => x.trim()).filter(Boolean)) rows.push({ key: k.key, value: v });
  const agentRev = host.agent_rev, hubRev = host.check_rev || 0;
  const status = agentRev == null
    ? (host.status === "online" ? "agent predates remote checks (1.1+) - copy the new avalon_agent.py over once; from 1.2 on, agents update themselves" : "")
    : agentRev === hubRev ? `agent is running revision ${hubRev}` : `revision ${hubRev} saved - agent still on ${agentRev}, applies on its next push`;
  const list = h("div", { class: "editor-rows" });
  const rowEl = (r) => {
    const sel = h("select", {}, ...CHECK_KINDS.map((k) => h("option", { value: k.key, selected: k.key === r.key ? "" : null }, k.label)));
    const inp = h("input", { type: "text", value: r.value, placeholder: CHECK_KINDS.find((k) => k.key === r.key)?.hint || "", spellcheck: "false" });
    sel.onchange = () => { r.key = sel.value; inp.placeholder = CHECK_KINDS.find((k) => k.key === r.key)?.hint || ""; };
    inp.oninput = () => (r.value = inp.value);
    const row = h("div", { class: "editor-row" }, sel, inp, h("button", { class: "iconbtn small", title: "remove", onclick: () => { rows.splice(rows.indexOf(r), 1); row.remove(); } }, "×"));
    return row;
  };
  for (const r of rows) list.append(rowEl(r));
  const singles = h("div", { class: "editor-singles" }, ...CHECK_SINGLE.map((k) => {
    const inp = h("input", { type: "text", value: cfg[k.key] ?? "", placeholder: k.hint, "data-key": k.key, spellcheck: "false" });
    return h("label", {}, h("span", {}, k.label), inp);
  }));
  const save = async () => {
    const out = {};
    for (const r of rows) if (r.value.trim()) out[r.key] = (out[r.key] ? out[r.key] + "," : "") + r.value.trim();
    for (const inp of singles.querySelectorAll("input")) if (inp.value.trim()) out[inp.dataset.key] = inp.value.trim();
    try {
      const r = await api(`/api/v1/hosts/${encodeURIComponent(host.name)}/checks`, { method: "PUT", headers: hdrs(), body: JSON.stringify(out) });
      toast(`Saved revision ${r.rev}; the agent applies it on its next push`);
      const full = await api(`/api/v1/hosts/${encodeURIComponent(host.name)}`); state.hosts.set(full.name, full); renderEditor(full);
    } catch (e) { toast(`Save failed: ${e.message}`, true); }
  };
  el.append(
    h("h3", {}, "Checks", h("span", { class: "muted editor-status" }, status)),
    h("p", { class: "muted editor-help" }, "Settings saved here are pushed to the agent with its next report and override the same keys in its agent.conf. One target per row; the agent runs the check every interval."),
    list,
    h("div", { class: "editor-actions" },
      h("button", { class: "btn", onclick: () => { const r = { key: "AVM_WATCH_PROCESSES", value: "" }; rows.push(r); list.append(rowEl(r)); list.lastChild.querySelector("input").focus(); } }, "+ Add check"),
      h("span", { class: "spacer" }),
      h("button", { class: "btn primary", onclick: save }, "Save to agent"),
    ),
    singles,
  );
}

async function loadDetailSeries() {
  const d = state.detail; if (!d) return;
  const keys = [...new Set(Object.values(d.charts).flatMap((c) => c.def.series.filter((x) => !x.hidden).map((x) => x.key)))];
  try {
    const res = await api(`/api/v1/hosts/${encodeURIComponent(d.name)}/series?metrics=${keys.join(",")}&range=${state.range}&points=700`);
    if (state.detail !== d) return;
    for (const { def, chart } of Object.values(d.charts)) {
      chart.setSeries(def.series.filter((x) => !x.hidden).map((x) => ({
        key: x.key, label: x.label, color: cssVar(x.color),
        data: res.ts.map((t, i) => [t, res[x.key][i]]),
      })));
      chart.setWindow(RANGE_SEC[state.range]);
    }
  } catch (e) { toast("Failed to load history: " + e.message, true); }
}

function updateDetail(host) {
  const d = state.detail; if (!d || host.name !== d.name) return;
  const s = host.sample || {};
  const sm = host.summary || {};
  const hi = s.host || sm.host || {};
  $('[data-f="dot"]', d.els.head).className = "status-dot " + host.status;
  const meta = $('[data-f="meta"]', d.els.head); meta.innerHTML = "";
  const chip = (t, cls = "") => h("span", { class: "chip " + cls }, t);
  meta.append(chip(osLabel(hi)), chip(hi.arch || "?"), chip((hi.cpu_model || "cpu").replace(/\s+/g, " ").slice(0, 48) + (hi.cpu_count ? ` · ${hi.cpu_count}c` : "")));
  if (hi.uptime_sec != null) meta.append(chip("up " + fmt.dur(hi.uptime_sec)));
  meta.append(chip(host.status === "online" ? "live" : "seen " + fmt.ago(host.age_sec), host.status === "online" ? "ok" : "bad"));
  if (hi.agent_version) {
    const c = chip(`agent ${hi.agent_version}` + (host.agent_update_error ? " · update failed" : host.agent_outdated ? " → updating" : ""), host.agent_update_error ? "bad" : host.agent_outdated ? "warn" : "");
    if (host.agent_update_error) c.title = host.agent_update_error;
    meta.append(c);
  }
  for (const t of hi.tags || []) meta.append(chip("#" + t));

  // KPIs
  const k = d.els.kpis; k.innerHTML = "";
  const kpi = (lbl, val, sub) => h("div", { class: "card kpi" }, h("div", { class: "lbl" }, lbl), h("div", { class: "val num", html: val }), h("div", { class: "sub num" }, sub || ""));
  const cpu = s.cpu || {}, mem = s.memory || {}, net = s.network || {}, dio = s.disk_io || {};
  k.append(kpi("CPU", fmt.pct(cpu.percent), cpu.load ? "load " + fmt.load(cpu.load) : (cpu.freq_mhz ? Math.round(cpu.freq_mhz) + " MHz" : "")));
  k.append(kpi("Memory", fmt.pct(mem.percent), mem.total ? `${fmt.bytes(mem.used)} / ${fmt.bytes(mem.total, 0)}` : ""));
  if (mem.committed != null) k.append(kpi("Committed", fmt.bytes(mem.committed, 1), mem.commit_limit ? `limit ${fmt.bytes(mem.commit_limit, 0)} · swap ${fmt.pct(mem.swap_percent)}` : `swap ${fmt.pct(mem.swap_percent)}`));
  for (const g of s.gpus || []) k.append(kpi(`GPU${s.gpus.length > 1 ? " " + g.index : ""}`, fmt.pct(g.util_percent), `${g.mem_used != null ? fmt.bytes(g.mem_used, 1) + (g.mem_total ? "/" + fmt.bytes(g.mem_total, 0) : "") : "vram " + fmt.pct(g.mem_percent)}${g.temp_c != null ? " · " + fmt.temp(g.temp_c) : ""}${g.power_w != null ? " · " + fmt.watts(g.power_w) : ""}`));
  k.append(kpi("Network in", fmt.bps(net.rx_bps), `out ${fmt.bps(net.tx_bps)}${(net.interfaces || []).length ? " · " + net.interfaces.slice(0, 2).map((i) => i.name).join(", ") : ""}`));
  k.append(kpi("Disk read", fmt.bps(dio.read_bps), `write ${fmt.bps(dio.write_bps)}${dio.read_iops != null ? ` · ${Math.round(dio.read_iops + dio.write_iops)} iops` : ""}`));
  const t = cpu.temp_c ?? sm.temp_max;
  if (t != null) k.append(kpi("CPU temp", fmt.temp(t), sm.temp_max != null ? "hottest " + fmt.temp(sm.temp_max) : ""));
  if (s.battery?.percent != null) k.append(kpi("Battery", Math.round(s.battery.percent) + "%", s.battery.plugged ? "plugged in" : (s.battery.secs_left ? fmt.dur(s.battery.secs_left) + " left" : "on battery")));

  // per-core
  const cores = d.els.cores; cores.innerHTML = "";
  for (const [i, c] of (cpu.per_core || []).entries()) {
    cores.append(h("div", { class: "core", title: `core ${i}: ${fmt.pct(c)}` }, h("i", { style: `height:${c}%; opacity:${0.35 + c / 150}` }), h("span", { class: "num" }, i)));
  }
  cores.parentElement.classList.toggle("hidden", !(cpu.per_core || []).length);

  // tables
  const tb = d.els.tables; tb.innerHTML = "";
  const table = (title, head, rows) => h("div", { class: "card table-card" }, h("h3", {}, title), h("div", { class: "table-wrap" }, h("table", {}, h("thead", {}, h("tr", {}, ...head.map((x) => h("th", { class: x.startsWith("r:") ? "r" : "" }, x.replace(/^r:/, ""))))), h("tbody", {}, ...rows))));
  const bar = (p, c = "var(--m-disk)") => h("span", { class: "bar" }, h("i", { style: `width:${p ?? 0}%; background:${levelFor(p) === "crit" ? "var(--s-crit)" : levelFor(p) === "warn" ? "var(--s-warn)" : c}` }));
  if ((s.disks || []).length) tb.append(table("Storage", ["Mount", "Type", "r:Used", "r:Free", "r:Size", "r:"], s.disks.map((x) => h("tr", {},
    h("td", { title: x.device }, x.mountpoint), h("td", { class: "muted" }, x.fstype), h("td", { class: "r num" }, fmt.pct(x.percent, 1)), h("td", { class: "r num" }, fmt.bytes(x.free, 0)), h("td", { class: "r num" }, fmt.bytes(x.total, 0)), h("td", { class: "r" }, bar(x.percent))))));
  if ((s.gpus || []).length) tb.append(table("GPUs", ["GPU", "r:Util", "r:VRAM", "r:Temp", "r:Power", "r:Fan", "r:Clock"], s.gpus.map((g) => h("tr", {},
    h("td", { title: `${g.vendor} via ${g.source}${g.state ? " · " + g.state : ""}` }, `${g.name}${g.state === "suspended" ? " (sleeping)" : ""}`), h("td", { class: "r num" }, fmt.pct(g.util_percent)),
    h("td", { class: "r num" }, g.mem_used != null && g.mem_total ? `${fmt.bytes(g.mem_used, 1)} / ${fmt.bytes(g.mem_total, 0)}` : fmt.pct(g.mem_percent)),
    h("td", { class: "r num" }, fmt.temp(g.temp_c)), h("td", { class: "r num" }, g.power_w != null ? `${Math.round(g.power_w)}${g.power_limit_w ? " / " + Math.round(g.power_limit_w) : ""} W` : "—"),
    h("td", { class: "r num" }, fmt.pct(g.fan_percent)), h("td", { class: "r num" }, g.clock_mhz != null ? Math.round(g.clock_mhz) + " MHz" : "—")))));
  if ((s.processes || []).length) tb.append(table("Top processes", ["PID", "Name", "User", "r:CPU", "r:Memory"], s.processes.map((p) => h("tr", {},
    h("td", { class: "num muted" }, p.pid), h("td", {}, p.name), h("td", { class: "muted" }, p.user || ""), h("td", { class: "r num" }, fmt.pct(p.cpu_percent, 1)), h("td", { class: "r num" }, `${fmt.bytes(p.mem_rss, 0)} · ${fmt.pct(p.mem_percent, 1)}`)))));
  if ((s.checks || []).length) tb.append(table("Checks", ["Check", "Kind", "State", "Detail"], s.checks.map((c) => h("tr", {},
    h("td", {}, c.name), h("td", { class: "muted" }, c.kind), h("td", {}, h("span", { class: "chip " + (c.ok ? (c.level === "info" ? "" : "ok") : c.level === "warning" ? "warn" : "bad"), html: (c.ok ? ICON.ok : ICON.bad).replace("<svg", '<svg width="11" height="11"') + (c.ok ? (c.level === "info" ? "info" : "ok") : c.level === "warning" ? "warning" : "failing") })), h("td", { class: "muted" }, c.detail || "")))));
  if ((s.network?.interfaces || []).length) tb.append(table("Network interfaces", ["Interface", "r:Receive", "r:Transmit", "r:Link"], s.network.interfaces.map((i) => h("tr", {},
    h("td", {}, i.name), h("td", { class: "r num" }, fmt.bps(i.rx_bps)), h("td", { class: "r num" }, fmt.bps(i.tx_bps)), h("td", { class: "r num muted" }, i.speed_mbps ? i.speed_mbps + " Mb/s" : (i.up ? "up" : "down"))))));
  if ((s.temps || []).length) tb.append(table("Sensors", ["Sensor", "r:Temp", "r:High", "r:Critical"], s.temps.map((x) => h("tr", {},
    h("td", {}, x.label), h("td", { class: "r num" }, fmt.temp(x.current)), h("td", { class: "r num muted" }, fmt.temp(x.high)), h("td", { class: "r num muted" }, fmt.temp(x.critical))))));
  tb.append(h("div", { class: "card table-card" }, h("h3", {}, "Host"), h("dl", { class: "kvlist" },
    ...[["Hostname", hi.hostname], ["OS", osLabel(hi)], ["Kernel", hi.kernel], ["Platform", hi.platform], ["CPU", hi.cpu_model], ["Cores", hi.cpu_count != null ? `${hi.cpu_count} logical · ${hi.cpu_count_physical ?? "?"} physical` : null],
      ["Booted", hi.boot_time ? new Date(hi.boot_time * 1000).toLocaleString() : null], ["Agent", hi.agent_version ? "v" + hi.agent_version : null], ["Agent update", host.agent_update_error ? "FAILED: " + host.agent_update_error : null], ["Agent IP", host.last_ip], ["Interval", s.interval ? s.interval + " s" : null], ["Notes", host.notes]]
      .filter(([, v]) => v).flatMap(([k2, v]) => [h("dt", {}, k2), h("dd", {}, v)]))));
}

function appendLive(host) {
  const d = state.detail; if (!d || host.name !== d.name) return;
  const sm = host.summary; if (!sm?.ts) return;
  const g = sm.gpus?.[0];
  const vals = { cpu: sm.cpu?.percent, load1: sm.cpu?.load?.[0], mem: sm.memory?.percent, swap: sm.memory?.swap_percent,
    gpu_util: g ? avg(sm.gpus.map((x) => x.util_percent)) : null, gpu_mem: g ? avg(sm.gpus.map((x) => x.mem_percent)) : null,
    net_rx: sm.network?.rx_bps, net_tx: sm.network?.tx_bps, disk_read: sm.disk_io?.read_bps, disk_write: sm.disk_io?.write_bps,
    cpu_temp: sm.cpu?.temp_c, gpu_temp: g ? Math.max(...sm.gpus.map((x) => x.temp_c ?? -1)) : null, temp_max: sm.temp_max };
  if (vals.gpu_temp === -1) vals.gpu_temp = null;
  for (const { chart } of Object.values(d.charts)) {
    for (const s of chart.series) chart.append(s.key, sm.ts, vals[s.key] ?? null);
    chart.draw();
  }
}
const avg = (a) => { const v = a.filter((x) => x != null); return v.length ? v.reduce((p, c) => p + c, 0) / v.length : null; };

// ------------------------------------------------------------------ live
function onHostUpdate(host) {
  const prev = state.hosts.get(host.name);
  state.hosts.set(host.name, host);
  if (host.summary?.ts && host.status === "online" && host.summary.cpu?.percent != null && (!prev || prev.summary?.ts !== host.summary.ts)) {
    const arr = state.spark.get(host.name) || [];
    if (!arr.length || arr[arr.length - 1][0] < host.summary.ts) { arr.push([host.summary.ts, host.summary.cpu.percent]); while (arr.length && arr[0][0] < host.summary.ts - RANGE_SEC[state.range]) arr.shift(); }
    state.spark.set(host.name, arr);
  }
  renderFleetStats();
  if (state.view === "fleet") {
    const card = $(`.host-card[data-host="${CSS.escape(host.name)}"]`);
    if (card) updateCard(card, host); else renderFleet();
  } else if (state.view === "detail" && state.detail?.name === host.name) {
    if (host.summary?.ts !== prev?.summary?.ts) {
      api(`/api/v1/hosts/${encodeURIComponent(host.name)}`).then((full) => {
        state.hosts.set(full.name, full); updateDetail(full);
        if ((prev?.agent_rev ?? null) !== (full.agent_rev ?? null) && !document.activeElement?.closest?.(".editor")) renderEditor(full);
      }).catch(() => {});
      appendLive(host);
    } else updateDetail({ ...(state.hosts.get(host.name)), status: host.status, age_sec: host.age_sec });
  }
}

let wsRetry = 1000;
function connectWs() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const ws = new WebSocket(`${proto}//${location.host}/api/v1/live`);
  state.ws = ws;
  let ping;
  ws.onopen = () => { state.live = true; $("#conn").className = "conn live"; wsRetry = 1000; ping = setInterval(() => ws.readyState === 1 && ws.send("ping"), 25000); };
  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.type === "snapshot") { for (const hst of msg.hosts) state.hosts.set(hst.name, hst); renderFleetStats(); if (state.view === "fleet") renderFleet(); }
    else if (msg.type === "host") onHostUpdate(msg.host);
  };
  ws.onclose = (ev) => {
    state.live = false; $("#conn").className = "conn"; clearInterval(ping);
    if (ev.code === 4401) { toast("Session expired - reload to sign in again", true); return; }
    setTimeout(connectWs, wsRetry); wsRetry = Math.min(wsRetry * 2, 30000);
  };
  ws.onerror = () => ws.close();
}

// age refresh for "seen Xs ago" labels while offline
setInterval(() => {
  const now = Date.now() / 1000;
  for (const hst of state.hosts.values()) if (hst.last_seen) hst.age_sec = now - hst.last_seen;
  if (state.view === "fleet") for (const hst of state.hosts.values()) { const card = $(`.host-card[data-host="${CSS.escape(hst.name)}"]`); if (card && hst.status !== "online") $('[data-f="seen"]', card).textContent = "seen " + fmt.ago(hst.age_sec); }
}, 10000);

// ----------------------------------------------------------------- router
function setRange(r) {
  state.range = r;
  try { localStorage.setItem("avm-range", r); } catch (_) { /* ignore */ }
  renderTopbar();
  if (state.view === "detail") loadDetailSeries();
  else if (state.view === "fleet") { state.spark.clear(); renderFleet(); }
}

function redrawAll() {
  if (state.view === "fleet") renderFleet();
  else if (state.view === "detail" && state.detail) { for (const { chart } of Object.values(state.detail.charts)) chart.draw(); loadDetailSeries(); }
}

function route() {
  const m = location.hash.match(/^#\/host\/(.+)$/);
  if (state.detail) { for (const { chart } of Object.values(state.detail.charts)) chart.destroy(); state.detail = null; }
  if (m) renderDetail(decodeURIComponent(m[1]));
  else renderFleet();
}

async function init() {
  try { state.range = localStorage.getItem("avm-range") || "1h"; } catch (_) { /* ignore */ }
  if (!RANGE_ORDER.includes(state.range)) state.range = "1h";
  try {
    state.config = await api("/api/v1/config");
    const hs = await api("/api/v1/hosts");
    for (const hst of hs.hosts) state.hosts.set(hst.name, hst);
  } catch (e) {
    $("#app").innerHTML = `<div class="empty">Cannot reach the hub API: ${esc(e.message)}</div>`;
    return;
  }
  renderTopbar();
  renderFleetStats();
  window.addEventListener("hashchange", route);
  route();
  connectWs();
}
window.addEventListener("error", (e) => toast("JS error: " + e.message, true));
window.addEventListener("unhandledrejection", (e) => toast("Error: " + (e.reason?.message || e.reason), true));
window.__avm = state;  // debug handle
init();
