// Avalon Monitor dashboard - shell, router, fleet and host views. Zero-build ES modules.
import { TimeChart, drawSparkline } from "./charts.js?v=1.14.3";
import { $, h, esc, cssVar, fmt, ICON, levelFor, osLabel, state, api, post, toast, on, emit, RANGE_ORDER, RANGE_SEC,
         loadPrefs, savePrefs, hostLayout, setHostLayout, makeSortable, navigate, commitWarnPct, backupHealth, diskFree,
         canChange, roTip, guard, MODE_RANK } from "./core.js?v=1.14.3";
import { renderResearch } from "./research.js?v=1.14.3";
import { renderTasks, renderAgents, renderAlerts, computeAlerts, loadTasks, loadJobs, loadTracked, taskAlerting, ackTasks } from "./views.js?v=1.14.3";

// ------------------------------------------------------------------ shell
const NAV = [
  { id: "fleet", hash: "#/", label: "Fleet", icon: ICON.fleet },
  { id: "tasks", hash: "#/tasks", label: "Tasks", icon: ICON.tasks },
  { id: "agents", hash: "#/agents", label: "Agents", icon: ICON.agents },
  { id: "research", hash: "#/research", label: "Research", icon: ICON.research },
  { id: "alerts", hash: "#/alerts", label: "Alerts", icon: ICON.alerts },
];

function renderNav() {
  const nav = $("#nav"); nav.innerHTML = "";
  const cur = currentSection();
  const hosts = [...state.hosts.values()];
  const offline = hosts.filter((x) => x.status === "offline").length;
  const runningTasks = [...state.tasks.values()].filter((t) => t.status === "running").length;
  const badTasks = [...state.tasks.values()].filter((t) => taskAlerting(t)).length;
  const runningJobs = [...state.jobs.values()].filter((j) => j.status === "running" || j.status === "queued").length;
  const alerts = computeAlerts();
  const crit = alerts.filter((a) => a.level === "crit").length;
  const badge = { fleet: offline ? [offline, "hot"] : null, tasks: badTasks ? [badTasks, "hot"] : runningTasks ? [runningTasks, ""] : null,
                  agents: runningJobs ? [runningJobs, ""] : null, alerts: alerts.length ? [alerts.length, crit ? "hot" : "warm"] : null };
  for (const n of NAV) {
    const a = h("a", { href: n.hash, class: cur === n.id ? "active" : "", html: n.icon + esc(n.label) });
    const b = badge[n.id];
    if (b) a.append(h("span", { class: "badge " + b[1] }, String(b[0])));
    a.onclick = () => $("#sidebar").classList.remove("open");
    nav.append(a);
  }
}
function currentSection() {
  const hsh = location.hash;
  if (hsh.startsWith("#/tasks")) return "tasks";
  if (hsh.startsWith("#/agents")) return "agents";
  if (hsh.startsWith("#/alerts")) return "alerts";
  if (hsh.startsWith("#/research")) return "research";
  return "fleet";
}
document.addEventListener("avm-badges", renderNav);

function renderTopbar() {
  const seg = $("#range-seg"); seg.innerHTML = "";
  for (const r of RANGE_ORDER) seg.append(h("button", { "aria-pressed": String(r === state.range), onclick: () => setRange(r) }, r));
  const sec = currentSection();
  seg.style.display = sec === "fleet" ? "" : "none";
  $("#fleet-stats").style.display = sec === "fleet" ? "" : "none";
  $("#theme-btn").onclick = () => {
    const cur = document.documentElement.dataset.theme === "light" ? "dark" : "light";
    document.documentElement.dataset.theme = cur;
    try { localStorage.setItem("avm-theme", cur); } catch (_) { /* ignore */ }
    redrawAll();
  };
  $("#menu-btn").innerHTML = ICON.menu;
  $("#menu-btn").onclick = () => $("#sidebar").classList.toggle("open");
  if (state.config) {
    const u = state.config.user;
    // read-only viewers (tailnet / localhost, E-01) see why, with a link to where changes can be made
    const who = u.method === "access" ? u.subject : "tailnet";
    $("#user").replaceChildren(canChange() ? (u.method === "access" ? who : "tailnet · direct")
      : h("span", { title: roTip() }, who + " · ", state.config.public_url ? h("a", { href: state.config.public_url, class: "ro-link" }, "read-only") : "read-only"));
    $("#version").textContent = `hub ${state.config.version} · agent ${state.config.agent_version || "?"}`;
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
  const alerts = computeAlerts().length;
  const stat = (b, s, go) => h("div", { class: "stat", onclick: go ? () => navigate(go) : null, style: go ? "cursor:pointer" : "" }, h("b", { class: "num" }, b), h("span", {}, s));
  const el = $("#fleet-stats"); el.innerHTML = "";
  el.append(
    stat(`${online.length} / ${hosts.length}`, "online"),
    stat(fmt.pct(avgCpu), "avg cpu"),
    stat(memTot ? `${fmt.bytes(memUsed, 0)} / ${fmt.bytes(memTot, 0)}` : "—", "memory"),
    stat(gpuUtil.length ? fmt.pct(gpuUtil.reduce((a, b) => a + b, 0) / gpuUtil.length) : "—", `${gpus.length} gpu${gpus.length === 1 ? "" : "s"}`),
    stat(String(alerts), "alerts", "#/alerts"),
  );
  el.lastChild.querySelector("b").style.color = alerts ? cssVar("--s-crit") : "";
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
  const card = h("div", { class: `card host-card ${host.status}`, "data-host": host.name, "data-id": host.name, onclick: (e) => { if (!e.target.closest(".drag-handle")) navigate("#/host/" + encodeURIComponent(host.name)); } });
  const gpu = s.gpus?.[0];
  card.append(
    h("div", { class: "host-head" },
      h("span", { class: "status-dot " + host.status, "data-f": "dot" }),
      h("div", {},
        h("div", { class: "host-name" }, host.display_name),
        h("div", { class: "host-sub" }, h("span", { "data-f": "os" }, osLabel(s.host)), h("span", { "data-f": "arch" }, s.host?.arch || ""))),
      h("div", { class: "right" }, h("div", { "data-f": "seen" }, ""), h("div", { "data-f": "uptime" }, "")),
      h("span", { class: "drag-handle", title: "drag to reorder", html: ICON.grip }),
    ),
    h("div", { class: "meters" },
      meter("CPU", "cpu", "", "", null),
      meter("Memory", "mem", "", "", null),
      meter("Disk", "disk", "", "", null),
      meter("GPU", "gpu", "", "", null, !gpu),
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

  const m = { cpu: $('[data-m="cpu"]', card), mem: $('[data-m="mem"]', card), disk: $('[data-m="disk"]', card), gpu: $('[data-m="gpu"]', card) };
  setMeter(m.cpu, fmt.pct(s.cpu?.percent), s.cpu?.load ? "load " + s.cpu.load[0].toFixed(2) : (s.cpu?.cores ? s.cpu.cores + " cores" : ""), s.cpu?.percent);
  setMeter(m.mem, fmt.pct(s.memory?.percent), s.memory?.total ? `${fmt.bytes(s.memory.used, 1)} of ${fmt.bytes(s.memory.total, 0)}` : "", s.memory?.percent);
  const dpct = s.disk?.percent ?? s.disk?.max_percent;
  setMeter(m.disk, fmt.pct(dpct), s.disk?.total ? `${fmt.bytes(diskFree(s.disk), 0)} free` : "", dpct, dpct == null);
  const g = s.gpus?.[0];
  if (g) {
    setMeter(m.gpu, fmt.pct(g.util_percent), g.mem_percent != null ? `vram ${fmt.pct(g.mem_percent)}` : "", g.util_percent);
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
  const running = [...state.tasks.values()].filter((x) => x.host === host.name && x.status === "running");
  if (running.length) foot.append(kv(ICON.tasks.replace("<svg", '<svg width="13" height="13"'), running.length === 1 ? running[0].name : `${running.length} tasks running`, "running tasks"));
  if (host.claude?.running_job) foot.append(kv(ICON.agents.replace("<svg", '<svg width="13" height="13"'), host.claude.running_job.title, "claude is working on this"));

  const checks = $('[data-f="checks"]', card); checks.innerHTML = "";
  for (const c of s.checks || []) {
    if (c.kind === "content" && c.ok) { checks.append(h("span", { class: "chip", title: `${c.name} · updated ${fmt.ago(c.value)}` }, h("b", {}, c.name + ": "), c.detail)); continue; }
    if (c.kind === "job") {
      if ((c.value || 0) > 0) checks.append(h("span", { class: "chip", title: c.detail || "" }, h("b", {}, "job: "), c.detail.replace(/^active: /, "")));
      else if (!c.ok) checks.append(h("span", { class: "chip bad", title: c.detail || "", html: ICON.bad.replace("<svg", '<svg width="11" height="11"') + esc(c.name) }));
      continue;
    }
    if (c.kind === "rule" && c.ok) continue;
    let label = c.name;
    if (c.kind === "queue") label = c.ok ? `${c.name}: ${c.value} pending` : `${c.name}: empty`;
    else if (c.kind === "marker" && !c.ok) label = `${c.value} ${c.name}`;
    else if (c.kind === "unit") label = `${c.name.replace(/\.(service|scope|timer)$/, "")}: ${(c.detail || "").split(":")[0].split(" ")[0].toLowerCase()}`;
    else if (c.kind === "memory" && !c.ok) label = `${c.value} GB free`;
    const cls = c.ok ? (c.kind === "unit" && !(c.value > 0) ? "" : "ok") : (c.level === "warning" ? "warn" : "bad");
    checks.append(h("span", { class: "chip " + cls, title: c.detail || "", html: (c.ok ? ICON.ok : ICON.bad).replace("<svg", '<svg width="11" height="11"') + esc(label) }));
  }
  for (const b of s.backups || []) {
    const bh = backupHealth(b);
    checks.append(h("span", { class: "chip " + (bh.level === "crit" ? "bad" : bh.level), title: `${b.name}: last success ${fmt.when(b.last_success)}${b.snapshot ? " · snapshot " + b.snapshot : ""}${b.next_run ? " · next run " + fmt.when(b.next_run) : ""}${b.error ? " · " + b.error : ""}`,
      html: (bh.level === "ok" ? ICON.ok : ICON.bad).replace("<svg", '<svg width="11" height="11"') + esc(bh.label) }));
  }
  const failed = [...state.tasks.values()].filter((x) => x.host === host.name && taskAlerting(x));
  for (const x of failed.slice(0, 3)) checks.append(h("span", { class: "chip " + (x.status === "failed" ? "bad" : "warn"), title: (x.note ? x.note + " · " : "") + "click to open (acknowledges the alert)", onclick: (e) => { e.stopPropagation(); ackTasks([x.id]); navigate(`#/tasks?open=${x.id}`); } }, `task ${x.status}: ${x.name}`));
  if (host.agent_update_error) checks.append(h("span", { class: "chip bad", title: host.agent_update_error }, "agent update failed"));
  else if (host.agent_outdated && host.status === "online") checks.append(h("span", { class: "chip warn", title: `running agent ${host.agent_version || "?"}; the hub serves a newer script and the agent updates itself on its next report` }, "agent updating"));
  const hostTasks = [...state.tasks.values()].filter((x) => x.host === host.name);
  const gpuHolder = hostTasks.find((x) => x.status === "running" && x.meta?.resources?.gpu === "exclusive");
  const queuedHere = hostTasks.filter((x) => x.status === "queued").length;
  if (gpuHolder) checks.append(h("span", { class: "chip gpu-hold", title: "exclusive GPU job (avm run --exclusive)", onclick: (e) => { e.stopPropagation(); navigate(`#/tasks?open=${gpuHolder.id}`); } }, `GPU: ${gpuHolder.name}`));
  if (queuedHere) checks.append(h("span", { class: "chip warn", title: "jobs waiting in this host's avm queue", onclick: (e) => { e.stopPropagation(); navigate(`#/tasks?host=${encodeURIComponent(host.name)}&status=queued`); } }, `${queuedHere} queued`));
  if (s.memory?.committed != null && s.memory?.commit_limit) {
    const cp = (100 * s.memory.committed) / s.memory.commit_limit;
    if (cp >= commitWarnPct(host)) checks.append(h("span", { class: "chip warn", title: `committed ${fmt.bytes(s.memory.committed)} of ${fmt.bytes(s.memory.commit_limit)} limit (warns at ${commitWarnPct(host)}%; set AVM_COMMIT_WARN_PCT in the host settings)` }, `commit ${Math.round(cp)}%`));
  }
  drawCardSpark(card, host.name);
}

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
    if (range !== state.range) return;
    state.spark.set(name, d.ts.map((t, i) => [t, d.cpu[i]]));
    const card = $(`.host-card[data-host="${CSS.escape(name)}"]`);
    if (card) drawCardSpark(card, name);
  } catch (e) { /* non-fatal */ }
  finally { sparkLoading.delete(name); }
}

function orderedHosts() {
  const order = state.prefs.fleetOrder || [];
  const idx = (n) => { const i = order.indexOf(n); return i < 0 ? 1e9 : i; };
  return [...state.hosts.values()].sort((a, b) => idx(a.name) - idx(b.name) || (a.status === "online" ? 0 : 1) - (b.status === "online" ? 0 : 1) || a.display_name.localeCompare(b.display_name));
}

function renderFleet() {
  const app = $("#app"); app.innerHTML = "";
  const grid = h("div", { class: "fleet" });
  const hosts = orderedHosts();
  if (!hosts.length) grid.append(h("div", { class: "empty" }, h("div", { html: "No hosts enrolled yet.<br>On AVALON run <code>avalon-monitor-manage add-host &lt;name&gt;</code>, then install the agent on that machine with the printed token." })));
  for (const host of hosts) grid.append(hostCard(host));
  app.append(grid);
  makeSortable(grid, { item: ".host-card", handle: ".drag-handle", onReorder: (ids) => { state.prefs.fleetOrder = ids; savePrefs(); } });
  state.view = "fleet";
  loadTasks().then(() => { for (const host of hosts) { const c = $(`.host-card[data-host="${CSS.escape(host.name)}"]`); if (c) updateCard(c, host); } }).catch(() => {});
  for (const host of hosts) if (!state.spark.has(host.name)) loadSpark(host.name); else drawCardSpark($(`.host-card[data-host="${CSS.escape(host.name)}"]`), host.name);
}

// ----------------------------------------------------------------- detail
const CHARTS = [
  { id: "cpu", title: "CPU", yMax: 100, format: (v) => Math.round(v) + "%", series: [{ key: "cpu", label: "CPU", color: "--m-cpu" }] },
  { id: "mem", title: "Memory", yMax: 100, format: (v) => Math.round(v) + "%", series: [{ key: "mem", label: "RAM", color: "--m-mem" }, { key: "swap", label: "swap", color: "--c-orange" }] },
  { id: "gpu", title: "GPU", yMax: 100, format: (v) => Math.round(v) + "%", series: [{ key: "gpu_util", label: "utilisation", color: "--m-gpu" }, { key: "gpu_mem", label: "VRAM", color: "--c-orange" }], needs: "gpu" },
  { id: "net", title: "Network", yMax: null, bytes: true, format: (v) => fmt.bps(v), series: [{ key: "net_rx", label: "receive", color: "--c-blue" }, { key: "net_tx", label: "transmit", color: "--c-orange" }] },
  { id: "disk", title: "Disk I/O", yMax: null, bytes: true, format: (v) => fmt.bps(v), series: [{ key: "disk_read", label: "read", color: "--c-blue" }, { key: "disk_write", label: "write", color: "--c-orange" }] },
  { id: "temp", title: "Temperature", yMax: null, format: (v) => Math.round(v) + "°C", series: [{ key: "cpu_temp", label: "CPU", color: "--c-blue" }, { key: "gpu_temp", label: "GPU", color: "--c-orange" }, { key: "temp_max", label: "hottest sensor", color: "--c-aqua" }] },
];
const PANELS = [...CHARTS.map((c) => ({ id: c.id, title: c.title })), { id: "cores", title: "Per-core load" }, { id: "storage", title: "Storage" }, { id: "gpus", title: "GPUs" }, { id: "procs", title: "Top processes" },
  { id: "net-if", title: "Network interfaces" }, { id: "sensors", title: "Sensors" }, { id: "hostinfo", title: "Host" }, { id: "backups", title: "Backups" }];

async function renderDetail(name) {
  const app = $("#app"); app.innerHTML = "";
  state.view = "detail";
  let host;
  try { host = await api(`/api/v1/hosts/${encodeURIComponent(name)}`); }
  catch (e) { app.append(h("div", { class: "empty" }, `Cannot load host "${name}": ${e.message}`)); return; }
  state.hosts.set(host.name, host);
  const s = host.sample || {};
  const hasGpu = (s.gpus || []).length > 0;
  const layout = hostLayout(host.name);
  const hidden = new Set(layout.hidden || []);

  const customizeBtn = h("button", { class: "btn" + (state.customizing ? " active" : ""), onclick: () => { state.customizing = !state.customizing; app.classList.toggle("customizing", state.customizing); customizeBtn.classList.toggle("active", state.customizing); renderHiddenBar(); } }, "Customize");
  const head = h("div", { class: "detail-head" },
    h("button", { class: "back", onclick: () => navigate("#/"), html: ICON.back + " Fleet" }),
    h("div", { class: "detail-title" }, h("span", { class: "status-dot " + host.status, "data-f": "dot" }), host.display_name),
    h("div", { class: "detail-meta", "data-f": "meta" }),
    h("div", { class: "actions" },
      h("button", { class: "btn", title: canChange() ? "" : "read-only here: prompts and transcripts can be read, not sent", onclick: () => navigate("#/agents/" + encodeURIComponent(host.name)) }, "Prompt Claude"),
      customizeBtn,
      guard(h("button", { class: "btn", title: "Ask the agent to re-exec itself on its next push (picks up a new script/config)", onclick: () => sendCommand(host.name, "respawn") }, "Respawn agent"))),
  );
  const hiddenBar = h("div", { class: "hidden-panels" });
  const kpis = h("div", { class: "kpis", "data-f": "kpis" });
  const charts = h("div", { class: "charts", "data-f": "charts" });
  const chartObjs = {};
  const panelEls = {};
  const panelCard = (id, title, body, cls = "card chart-card") => {
    const el = h("div", { class: cls, "data-id": id }, h("h3", {}, h("span", { class: "drag-handle", html: ICON.grip }), title, h("span", { class: "spacer" }), h("button", { class: "hide-btn", title: "hide panel", onclick: () => hidePanel(id) }, "×")), body);
    panelEls[id] = el;
    return el;
  };
  for (const c of CHARTS) {
    if (c.needs === "gpu" && !hasGpu) continue;
    const series = c.series.filter((x) => !x.hidden);
    const legend = series.length > 1 ? h("span", { class: "legend" }, ...series.map((x) => h("span", { html: `<i style="--c: var(${x.color})"></i>${esc(x.label)}` }))) : null;
    const box = h("div", { class: "chart" });
    const el = panelCard(c.id, c.title, box);
    if (legend) el.querySelector(".spacer").replaceWith(legend, h("span", { class: "spacer", style: "flex:0" }));
    charts.append(el);
    chartObjs[c.id] = { def: c, chart: new TimeChart(box, { yMax: c.yMax, bytes: !!c.bytes, format: c.format, window: RANGE_SEC[state.range] }) };
  }
  const cores = h("div", { class: "cores", "data-f": "cores" });
  charts.append(panelCard("cores", "Per-core load", cores, "card chart-card cores-card"));

  const tables = h("div", { class: "tables", "data-f": "tables" });
  for (const id of ["backups", "storage", "gpus", "procs", "net-if", "sensors", "hostinfo"]) tables.append(panelCard(id, PANELS.find((p) => p.id === id).title, h("div", { "data-f": "body" }), "card table-card"));
  const checksPanel = h("div", { class: "card checks-panel", "data-f": "checks-panel" }, h("h3", {}, "Checks"));

  app.append(head, hiddenBar, kpis, charts, tables, checksPanel);
  app.classList.toggle("customizing", state.customizing);
  state.detail = { name: host.name, charts: chartObjs, els: { head, kpis, cores, tables, charts, checks: checksPanel, hiddenBar }, panels: panelEls, hidden };

  // apply saved order + hidden
  const applyOrder = (container) => {
    const order = layout.order || [];
    const items = [...container.children];
    items.sort((a, b) => { const ia = order.indexOf(a.dataset.id), ib = order.indexOf(b.dataset.id); return (ia < 0 ? 1e9 : ia) - (ib < 0 ? 1e9 : ib); });
    for (const it of items) container.append(it);
  };
  applyOrder(charts); applyOrder(tables);
  for (const [id, el] of Object.entries(panelEls)) el.classList.toggle("hidden", hidden.has(id));
  const persist = () => setHostLayout(host.name, { order: [...charts.children, ...tables.children].map((x) => x.dataset.id), hidden: [...hidden] });
  makeSortable(charts, { item: ".chart-card", handle: ".drag-handle", onReorder: persist });
  makeSortable(tables, { item: ".table-card", handle: ".drag-handle", onReorder: persist });
  function hidePanel(id) { hidden.add(id); panelEls[id].classList.add("hidden"); persist(); renderHiddenBar(); }
  function renderHiddenBar() {
    hiddenBar.innerHTML = "";
    if (!state.customizing && !hidden.size) return;
    if (hidden.size) hiddenBar.append(h("span", {}, "hidden:"), ...[...hidden].map((id) => h("span", { class: "chip", title: "click to show", onclick: () => { hidden.delete(id); panelEls[id]?.classList.remove("hidden"); persist(); renderHiddenBar(); } }, "+ " + (PANELS.find((p) => p.id === id)?.title || id))));
    if (state.customizing) hiddenBar.append(h("span", { class: "spacer" }),
      h("button", { class: "btn small", onclick: () => { setHostLayout("*", hostLayout(host.name)); toast("Layout applied to all hosts"); } }, "Use this layout for all hosts"),
      h("button", { class: "btn small", onclick: () => { delete state.prefs.hostLayout[host.name]; savePrefs(); renderDetail(host.name); } }, "Reset"));
  }
  renderHiddenBar();
  updateDetail(host);
  renderChecksPanel(host.name, true);
  await loadDetailSeries();
}

// ----------------------------------------------------------- checks panel
// One full-width panel: every check with its live result, its responder (a Claude agent on this host that is
// prompted, read-only, when the check starts failing) and expiry; adding and editing checks; and the few other host
// settings. Agents manage the same rows with `avm check ...`. Saved on the hub, pushed to the agent on its next report.
const TTL_OPTS = [["", "no expiry"], ["3600", "1 h"], ["21600", "6 h"], ["43200", "12 h"], ["86400", "1 day"], ["259200", "3 days"], ["604800", "7 days"]];
const SETTING_LABELS = { AVM_COMMIT_WARN_PCT: ["Warn when memory commit above (%)", "85 (default) · off"], AVM_TAILSCALE_MIN: ["Minimum Tailscale version", "1.104.2 (default)"], AVM_DATA_USE: ["Data-use host (hub drops logs, transcripts, snippets, notes)", "restricted · off = lifted · empty = auto (set when the agent reports a restricted policy)"], AVM_PROCESSES: ["Top processes reported", "8"], AVM_TAGS: ["Tags", "comma-separated chips"] };

async function sendCommand(name, cmd) {
  try {
    const r = await post(`/api/v1/hosts/${encodeURIComponent(name)}/command/${cmd}`, {});
    toast(`${cmd} queued for ${name} - ${r.note}`);
  } catch (e) { toast(`Failed: ${e.message}`, true); }
}

function checkForm(cl, c, submit, cancel) {
  const kind = h("select", { disabled: c ? "" : null }, ...cl.kinds.map((k) => h("option", { value: k.kind }, k.label)));
  kind.value = c?.kind || "file";
  const hint = () => cl.kinds.find((k) => k.kind === kind.value)?.hint || "";
  const target = h("input", { type: "text", value: c?.target || "", placeholder: hint(), spellcheck: "false", class: "mono" });
  kind.onchange = () => { target.placeholder = hint(); };
  const label = h("input", { type: "text", value: c?.label || "", placeholder: "label, e.g. R4 chain (optional)" });
  const ttl = h("select", { title: "remove the check by itself after" }, ...TTL_OPTS.map(([v, l]) => h("option", { value: v }, l)));
  const resp = h("select", { title: "prompted (read-only) when the check goes from ok to failing" },
    h("option", { value: "none" }, "no responder"), ...cl.responders.map((r) => h("option", { value: r.value }, `wake ${r.label}`)), h("option", { value: "new" }, "new read-only session"));
  resp.value = c?.notify || "none";
  const cwd = h("input", { type: "text", value: c?.notify_cwd || "", placeholder: "working dir for a new session", spellcheck: "false", class: "mono" });
  const withLog = h("input", { type: "checkbox" }); withLog.checked = !!c?.notify_log;
  const cooldown = h("input", { type: "number", min: "5", value: c?.cooldown_min ?? 30, title: "minutes between alerts", style: "width:70px" });
  const cap = h("input", { type: "number", min: "1", max: "48", value: c?.daily_cap ?? 6, title: "alerts per rolling 24 h at most", style: "width:60px" });
  // Alert mode: the responder prompt's permission mode (plan by default); the host's AVM_MAX_JOB_MODE still caps it
  const alertMode = guard(h("select", { title: "permission mode of the alert prompt; held, not lowered, if the host's agent allows less" },
    ...Object.keys(MODE_RANK).map((m) => h("option", { value: m }, m))));
  alertMode.value = c?.alert_mode || "plan";
  const go = async () => {
    const body = { target: target.value.trim(), label: label.value.trim(), notify: resp.value, notify_cwd: cwd.value.trim(), notify_log: withLog.checked,
      cooldown_min: Number(cooldown.value) || 30, daily_cap: Number(cap.value) || 6, alert_mode: alertMode.value };
    if (alertMode.value === "full" && c?.alert_mode !== "full") {
      if (!confirm("Alerts from this check will run with FULL ACCESS (no permission prompts) when the host allows it. Continue?")) return;
    }
    if (alertMode.value === "full") body.confirm_full = true;
    if (ttl.value) body.ttl_sec = Number(ttl.value); else if (c?.expires) body.ttl_sec = 0;
    if (!c) body.kind = kind.value;
    if (!body.target) { target.focus(); return; }
    await submit(body);
  };
  target.onkeydown = (e) => { if (e.key === "Enter") go(); };
  return h("div", { class: "check-form" },
    h("div", { class: "row1" }, kind, target, label),
    h("div", { class: "row2" }, h("label", {}, "expires ", ttl), h("label", {}, "responder ", resp), cwd,
      h("label", { title: "add related avm task log tails to the alert prompt (leave off for data-use-agreement data)" }, withLog, " with log tails"),
      h("label", {}, "cooldown ", cooldown, " min"), h("label", { title: "alerts per rolling 24 h at most" }, "cap ", cap, " per 24 h"),
      h("label", {}, "Alert mode ", alertMode), h("span", { class: "spacer" }),
      cancel ? h("button", { class: "btn small", onclick: cancel }, "Cancel") : null,
      guard(h("button", { class: "btn small primary", onclick: go }, c ? "Save" : "Add check"))));
}

async function renderChecksPanel(name, force = false) {
  const d = state.detail; if (!d || !d.els.checks || d.name !== name) return;
  const el = d.els.checks;
  const ae = document.activeElement;
  if (!force && el.contains(ae) && ae.matches("input, select, textarea")) return;   // never clobber an edit in progress
  if (!force && el.querySelector('.check-edit[data-open="1"], .check-add[data-open="1"]')) return;
  let cl;
  try { cl = await api(`/api/v1/hosts/${encodeURIComponent(name)}/checklist`); }
  catch (e) { el.replaceChildren(h("h3", {}, "Checks"), h("div", { class: "muted" }, e.message)); return; }
  if (state.detail !== d) return;
  const now = Date.now() / 1000;
  const status = cl.agent_rev == null ? "" : cl.agent_rev === cl.rev ? `agent is running revision ${cl.rev}` : `revision ${cl.rev} saved · applies on the agent's next report (it runs ${cl.agent_rev})`;
  const reload = () => renderChecksPanel(name, true);
  const patch = async (id, body) => { try { await post(`/api/v1/checklist/${id}`, body, "PATCH"); toast("Saved"); reload(); } catch (e) { toast(e.message, true); } };
  const tbody = h("tbody");
  for (const c of cl.checks) {
    const r = c.result, st = c.state || (r ? (r.ok ? "ok" : "failing") : "unknown");
    const cls = st === "ok" ? "ok" : st === "failing" ? (r?.level === "warning" ? "warn" : "bad") : "";
    const respName = !c.notify ? "—" : c.notify === "new" ? "new read-only session" : (cl.responders.find((x) => x.value === c.notify)?.label || "tracked agent (gone)");
    const alert = c.open_job ? h("a", { class: "chip warn", href: `#/agents/${encodeURIComponent(name)}?job=${c.open_job}`, title: "the responder prompt sent for this failure" }, `alert #${c.open_job}`)
      : c.pending ? h("span", { class: "chip", title: c.hold_reason ? `held: ${c.hold_reason} - it goes out on a later report if the check is still failing` : "held: the cap (alerts per rolling 24 h) is reached, it is cooling down, the target session is mid-turn, or there is nothing to prompt on this host yet - it goes out on a later report if the check is still failing" }, "alert pending") : null;
    const editTr = h("tr", { class: "check-edit", style: "display:none" });
    const closeEdit = () => { editTr.style.display = "none"; editTr.dataset.open = ""; };
    editTr.append(h("td", { colspan: 6 }, checkForm(cl, c, (body) => patch(c.id, body), closeEdit)));
    tbody.append(h("tr", { class: "check-row" + (st === "failing" ? " failing" : "") },
      h("td", {}, h("span", { class: "chip " + cls }, st)),
      h("td", {}, h("div", {}, h("b", {}, c.kind_label), " ", h("code", { title: c.target }, c.kind === "memory" ? `${c.target} GB` : c.target)), c.label ? h("div", { class: "muted small" }, c.label) : null),
      h("td", {}, h("div", { class: "muted" }, (r?.detail ?? c.detail ?? "") || "—"), c.since ? h("div", { class: "small muted" }, `${st} for ${fmt.dur(now - c.since)}`) : null),
      h("td", {}, h("div", {}, respName), c.notify ? h("div", { class: "small muted" }, `cooldown ${c.cooldown_min} min · ≤${c.daily_cap} per 24 h${c.notify_log ? " · with logs" : ""}${c.alert_mode && c.alert_mode !== "plan" ? ` · ${c.alert_mode} mode` : ""}`) : null, alert),
      h("td", { class: "small muted" }, c.expires ? `expires in ${fmt.dur(c.expires - now)}` : "", h("div", {}, c.created_by ? `added by ${c.created_by}` : "")),
      h("td", { class: "r nowrap" },
        guard(h("button", { class: "btn small", onclick: () => { const open = editTr.style.display === "none"; editTr.style.display = open ? "" : "none"; editTr.dataset.open = open ? "1" : ""; } }, "Edit")),
        guard(h("button", { class: "iconbtn small", title: "remove this check", onclick: async () => { if (!confirm(`Remove the ${c.kind_label} check on ${c.target}?`)) return; try { await post(`/api/v1/checklist/${c.id}`, undefined, "DELETE"); reload(); } catch (e) { toast(e.message, true); } } }, "×")))));
    tbody.append(editTr);
  }
  for (const r of cl.other_results) {
    tbody.append(h("tr", { class: "check-row rule" },
      h("td", {}, h("span", { class: "chip " + (r.ok ? (r.level === "info" ? "" : "ok") : r.level === "warning" ? "warn" : "bad") }, r.ok ? (r.level === "info" ? "info" : "ok") : "failing")),
      h("td", {}, h("div", {}, h("b", {}, r.kind === "rule" ? "hub rule" : r.kind), " ", h("code", {}, r.name))),
      h("td", { class: "muted" }, r.detail || ""), h("td", { class: "muted small" }, r.kind === "rule" ? "built in" : "from agent.conf (editable here once the agent updates to 1.7)"), h("td", {}), h("td", {})));
  }
  const addRow = h("div", { class: "check-add", style: "display:none" });
  const closeAdd = () => { addRow.style.display = "none"; addRow.dataset.open = ""; };
  addRow.append(checkForm(cl, null, async (body) => {
    try { await post(`/api/v1/hosts/${encodeURIComponent(name)}/checklist`, body); toast("Check added; the agent picks it up on its next report"); addRow.dataset.open = ""; reload(); }
    catch (e) { toast(e.message, true); }
  }, closeAdd));
  const settings = h("div", { class: "check-settings" }, ...Object.entries(SETTING_LABELS).map(([k, [lbl, hint]]) =>
    h("label", {}, h("span", {}, lbl), guard(h("input", { type: "text", value: cl.settings[k] || "", placeholder: hint, "data-key": k, spellcheck: "false" })))),
    guard(h("button", { class: "btn small", onclick: async () => {
      const body = {}; for (const i of settings.querySelectorAll("input")) body[i.dataset.key] = i.value.trim();
      try { const r = await post(`/api/v1/hosts/${encodeURIComponent(name)}/check-settings`, body, "PUT"); toast(`Saved revision ${r.rev}`); reload(); } catch (e) { toast(e.message, true); }
    } }, "Save settings")));
  const failing = cl.checks.filter((c) => c.state === "failing").length;
  const settingsOpen = !!el.querySelector(".check-settings-wrap")?.open;   // the panel re-renders on every report: keep it open
  el.replaceChildren(...[
    h("h3", {}, "Checks", h("span", { class: "muted head-note" }, `${cl.checks.length} configured${failing ? ` · ${failing} failing` : ""}${status ? " · " + status : ""}`),
      h("span", { class: "spacer" }), guard(h("button", { class: "btn small primary", onclick: () => { addRow.style.display = ""; addRow.dataset.open = "1"; addRow.querySelector("input")?.focus(); } }, "+ Add check"))),
    !cl.can_prompt ? h("div", { class: "chip", style: "margin-bottom:8px;white-space:normal" }, "Responders can't run on this host: it has no Claude Code CLI (Claude Desktop's built-in copy only runs inside the app).") : null,
    addRow,
    cl.checks.length || cl.other_results.length
      ? h("div", { class: "table-wrap" }, h("table", { class: "checks-table" }, h("thead", {}, h("tr", {}, h("th", {}, "State"), h("th", {}, "Check"), h("th", {}, "Result"), h("th", {}, "Responder"), h("th", {}, "Expiry"), h("th", {}))), tbody))
      : h("div", { class: "muted", style: "padding:8px 0" }, "No checks on this host yet."),
    h("p", { class: "muted small check-help" }, "Agents can manage these themselves: ", h("code", {}, "avm check add file ~/run/heartbeat:600 --label \"R4 chain\" --notify self --ttl 12h"),
      ". A responder is prompted (plan mode, read-only, unless its alert mode is raised) when its check goes from ok to failing (cooldown, a cap per rolling 24 h, one open alert until the check recovers); it investigates and reports, never acts."),
    h("details", { class: "check-settings-wrap", open: settingsOpen ? "" : null }, h("summary", {}, "Host settings"), settings),
  ].filter(Boolean));
}

async function loadDetailSeries() {
  const d = state.detail; if (!d) return;
  const keys = [...new Set(Object.values(d.charts).flatMap((c) => c.def.series.filter((x) => !x.hidden).map((x) => x.key)))];
  try {
    const res = await api(`/api/v1/hosts/${encodeURIComponent(d.name)}/series?metrics=${keys.join(",")}&range=${state.range}&points=700`);
    if (state.detail !== d) return;
    for (const { def, chart } of Object.values(d.charts)) {
      chart.setSeries(def.series.filter((x) => !x.hidden).map((x) => ({ key: x.key, label: x.label, color: cssVar(x.color), data: res.ts.map((t, i) => [t, res[x.key][i]]) })));
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
  const running = [...state.tasks.values()].filter((x) => x.host === host.name && x.status === "running");
  if (running.length) k.append(kpi("Tasks", String(running.length), running.map((x) => x.name).join(", ").slice(0, 60)));
  if (host.claude?.running_job) k.append(kpi("Claude", "working", host.claude.running_job.title.slice(0, 60)));

  const cores = d.els.cores; cores.innerHTML = "";
  for (const [i, c] of (cpu.per_core || []).entries()) cores.append(h("div", { class: "core", title: `core ${i}: ${fmt.pct(c)}` }, h("i", { style: `height:${c}%; opacity:${0.35 + c / 150}` }), h("span", { class: "num" }, i)));
  d.panels.cores.classList.toggle("hidden", d.hidden.has("cores") || !(cpu.per_core || []).length);

  const body = (id) => { const el = d.panels[id].querySelector('[data-f="body"]'); el.innerHTML = ""; return el; };
  const show = (id, yes) => d.panels[id]?.classList.toggle("hidden", d.hidden.has(id) || !yes);
  // head: "Label", "r:Label" (right-aligned), "...|hide-narrow" / "|hide-xnarrow": a low-value column dropped in a narrow card (F-20)
  const table = (head, rows) => h("div", { class: "table-wrap" }, h("table", {}, h("thead", {}, h("tr", {}, ...head.map((x) => { const [l, c = ""] = x.split("|"); return h("th", { class: (l.startsWith("r:") ? "r " : "") + c }, l.replace(/^r:/, "")); }))), h("tbody", {}, ...rows)));
  const bar = (p, c = "var(--m-disk)") => h("span", { class: "bar" }, h("i", { style: `width:${p ?? 0}%; background:${levelFor(p) === "crit" ? "var(--s-crit)" : levelFor(p) === "warn" ? "var(--s-warn)" : c}` }));
  show("storage", (s.disks || []).length);
  if ((s.disks || []).length) body("storage").append(table(["Mount", "Type|hide-narrow", "r:Used", "r:Free", "r:Size|hide-xnarrow", "r:"], s.disks.map((x) => h("tr", {},
    h("td", { title: x.device }, x.mountpoint), h("td", { class: "muted hide-narrow" }, x.fstype), h("td", { class: "r num" }, fmt.pct(x.percent, 1)), h("td", { class: "r num" }, fmt.bytes(x.free, 0)), h("td", { class: "r num hide-xnarrow" }, fmt.bytes(x.total, 0)), h("td", { class: "r" }, bar(x.percent))))));
  show("gpus", (s.gpus || []).length);
  if ((s.gpus || []).length) body("gpus").append(table(["GPU", "r:Util", "r:VRAM", "r:Temp", "r:Power|hide-xnarrow", "r:Fan|hide-narrow", "r:Clock|hide-narrow"], s.gpus.map((g) => h("tr", {},
    h("td", { class: "wrap", title: `${g.vendor} via ${g.source}${g.state ? " · " + g.state : ""}` }, `${g.name}${g.state === "suspended" ? " (sleeping)" : ""}`), h("td", { class: "r num" }, fmt.pct(g.util_percent)),
    h("td", { class: "r num" }, g.mem_used != null && g.mem_total ? `${fmt.bytes(g.mem_used, 1)} / ${fmt.bytes(g.mem_total, 0)}` : fmt.pct(g.mem_percent)),
    h("td", { class: "r num" }, fmt.temp(g.temp_c)), h("td", { class: "r num hide-xnarrow" }, g.power_w != null ? `${Math.round(g.power_w)}${g.power_limit_w ? " / " + Math.round(g.power_limit_w) : ""} W` : "—"),
    h("td", { class: "r num hide-narrow" }, fmt.pct(g.fan_percent)), h("td", { class: "r num hide-narrow" }, g.clock_mhz != null ? Math.round(g.clock_mhz) + " MHz" : "—")))));
  show("procs", (s.processes || []).length);
  if ((s.processes || []).length) body("procs").append(table(["PID", "Name", "User|hide-xnarrow", "r:CPU", "r:Memory"], s.processes.map((p) => h("tr", {},
    h("td", { class: "num muted" }, p.pid), h("td", {}, p.name), h("td", { class: "muted hide-xnarrow" }, p.user || ""), h("td", { class: "r num" }, fmt.pct(p.cpu_percent, 1)), h("td", { class: "r num" }, `${fmt.bytes(p.mem_rss, 0)} · ${fmt.pct(p.mem_percent, 1)}`)))));
  show("net-if", (s.network?.interfaces || []).length);
  if ((s.network?.interfaces || []).length) body("net-if").append(table(["Interface", "r:Receive", "r:Transmit", "r:Link"], s.network.interfaces.map((i) => h("tr", {},
    h("td", {}, i.name), h("td", { class: "r num" }, fmt.bps(i.rx_bps)), h("td", { class: "r num" }, fmt.bps(i.tx_bps)), h("td", { class: "r num muted" }, i.speed_mbps ? i.speed_mbps + " Mb/s" : (i.up ? "up" : "down"))))));
  show("sensors", (s.temps || []).length);
  if ((s.temps || []).length) body("sensors").append(table(["Sensor", "r:Temp", "r:High", "r:Critical"], s.temps.map((x) => h("tr", {},
    h("td", {}, x.label), h("td", { class: "r num" }, fmt.temp(x.current)), h("td", { class: "r num muted" }, fmt.tempLimit(x.high)), h("td", { class: "r num muted" }, fmt.tempLimit(x.critical))))));
  show("backups", (s.backups || []).length);
  if ((s.backups || []).length) {
    const el = body("backups"), now = Date.now() / 1000;
    for (const b of s.backups) {
      const bh = backupHealth(b, now);
      const run = [...state.tasks.values()].filter((x) => x.host === host.name && x.name === b.name).sort((x, y) => y.started - x.started)[0];
      const keep = b.keep ? Object.entries(b.keep).map(([k2, v]) => `${v} ${k2}`).join(" / ") : "";
      el.append(h("div", { class: "backup-head" }, h("b", {}, b.name || "backup"), " ", chip(bh.label, bh.level === "crit" ? "bad" : bh.level),
        run ? h("a", { href: `#/tasks?open=${run.id}`, class: "muted", style: "margin-left:8px" }, `last run: task #${run.id} (${run.status})`) : null));
      el.append(h("dl", { class: "kvlist", style: "margin:6px 0 14px" },
        ...[["Last success", b.last_success ? `${fmt.when(b.last_success)} · ${fmt.ago(now - b.last_success)}` : "never"],
          ["Last run", b.state === "running" ? `running since ${fmt.when(b.started)}${bh.stale ? " - never finished: killed (OOM, reboot, SIGKILL) or hung" : ""}` : b.finished ? `${b.state} · ${fmt.when(b.finished)}${b.duration_sec != null ? " · took " + fmt.dur(b.duration_sec) : ""}` : null],
          [b.state === "partial" ? "Not backed up" : "Error", b.error], ["Next run", b.next_run ? fmt.when(b.next_run) : null],
          ["Snapshot", b.snapshot ? `${b.snapshot}${b.files != null ? ` · ${b.files.toLocaleString()} files` : ""}${b.bytes_scanned != null ? " · " + fmt.bytes(b.bytes_scanned) : ""}` : null],
          ["Last run added", b.bytes_added != null ? `${fmt.bytes(b.bytes_added)} new data${b.files_new != null ? ` · ${b.files_new.toLocaleString()} new / ${(b.files_changed || 0).toLocaleString()} changed files` : ""}` : null],
          ["Warnings", b.warnings ? `${b.warnings} (unreadable files, missing paths or a failed SQLite copy - the snapshot was still saved)` : null],
          ["Repository", `${b.repository || "?"}${b.repo_bytes != null ? " · " + fmt.bytes(b.repo_bytes) : ""}${b.target_free_bytes != null ? " · " + fmt.bytes(b.target_free_bytes, 0) + " free" : ""}`],
          ["Kept", b.snapshots != null ? `${b.snapshots} snapshot${b.snapshots === 1 ? "" : "s"}${keep ? " · keeps " + keep : ""}` : null],
          ["Verified", b.checked ? `${fmt.when(b.checked)} (part of the data re-read)` : null],
          ["Covers", (b.paths || []).join("  ")],
          ["Note", b.seeded ? "read from the repository; per-run numbers appear after the next run" : null]]
          .filter(([, v]) => v).flatMap(([k2, v]) => [h("dt", {}, k2), h("dd", {}, v)])));
    }
  }
  show("hostinfo", true);
  body("hostinfo").append(h("dl", { class: "kvlist" },
    ...[["Hostname", hi.hostname], ["OS", osLabel(hi)], ["Kernel", hi.kernel], ["Platform", hi.platform], ["CPU", hi.cpu_model], ["Cores", hi.cpu_count != null ? `${hi.cpu_count} logical · ${hi.cpu_count_physical ?? "?"} physical` : null],
      ["Booted", hi.boot_time ? new Date(hi.boot_time * 1000).toLocaleString() : null], ["Agent", hi.agent_version ? "v" + hi.agent_version : null], ["Agent update", host.agent_update_error ? "FAILED: " + host.agent_update_error : null], ["Agent IP", host.last_ip], ["Interval", s.interval ? s.interval + " s" : null],
      ["Claude", s.claude ? (s.claude.available ? `${s.claude.version || "?"} as ${s.claude.user}` : "cli not found") : null], ["Notes", host.notes]]
      .filter(([, v]) => v).flatMap(([k2, v]) => [h("dt", {}, k2), h("dd", {}, v)])));
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
  for (const { chart } of Object.values(d.charts)) { for (const s of chart.series) chart.append(s.key, sm.ts, vals[s.key] ?? null); chart.draw(); }
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
  renderFleetStats(); renderNav();
  if (state.view === "fleet") {
    const card = $(`.host-card[data-host="${CSS.escape(host.name)}"]`);
    if (card) updateCard(card, host); else renderFleet();
  } else if (state.view === "detail" && state.detail?.name === host.name) {
    if (host.summary?.ts !== prev?.summary?.ts) {
      api(`/api/v1/hosts/${encodeURIComponent(host.name)}`).then((full) => {
        state.hosts.set(full.name, full); updateDetail(full);
        renderChecksPanel(full.name);   // live results; skipped while an input in the panel has focus
      }).catch(() => {});
      appendLive(host);
    } else updateDetail({ ...(state.hosts.get(host.name)), status: host.status, age_sec: host.age_sec });
  }
  emit("host", host);
}
on("task", (t) => { if (state.view === "fleet") { const card = $(`.host-card[data-host="${CSS.escape(t.host)}"]`); const host = state.hosts.get(t.host); if (card && host) updateCard(card, host); } renderNav(); renderFleetStats(); });
on("claude_job", () => { renderNav(); });
on("check", (c) => { if (state.view === "detail" && state.detail?.name === c.host) renderChecksPanel(c.host); });
on("check_deleted", (m) => { if (state.view === "detail" && state.detail && (!m.host || state.detail.name === m.host)) renderChecksPanel(state.detail.name); });
on("claude_tracked", () => { renderNav(); });
on("claude_untracked", () => { renderNav(); });

let wsRetry = 1000;
function connectWs() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const ws = new WebSocket(`${proto}//${location.host}/api/v1/live`);
  state.ws = ws;
  let ping;
  ws.onopen = () => { state.live = true; $("#conn").className = "conn live"; wsRetry = 1000; ping = setInterval(() => ws.readyState === 1 && ws.send("ping"), 25000); };
  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.type === "snapshot") { for (const hst of msg.hosts) state.hosts.set(hst.name, hst); renderFleetStats(); renderNav(); if (state.view === "fleet") renderFleet(); }
    else if (msg.type === "host") onHostUpdate(msg.host);
    else if (msg.type === "task") emit("task", msg.task);
    else if (msg.type === "task_deleted") emit("task_deleted", msg);
    else if (msg.type === "claude_job") emit("claude_job", msg.job);
    else if (msg.type === "claude_event") emit("claude_event", msg);
    else if (msg.type === "claude_tracked") emit("claude_tracked", msg.row);
    else if (msg.type === "check") emit("check", msg.check);
    else if (msg.type === "check_deleted") emit("check_deleted", msg);
    else if (msg.type === "claude_untracked") emit("claude_untracked", msg);
    else if (msg.type === "results") emit("results", msg);
  };
  ws.onclose = (ev) => {
    state.live = false; $("#conn").className = "conn"; clearInterval(ping);
    if (ev.code === 4401) { toast("Session expired - reload to sign in again", true); return; }
    setTimeout(connectWs, wsRetry); wsRetry = Math.min(wsRetry * 2, 30000);
  };
  ws.onerror = () => ws.close();
}

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
  const hsh = location.hash || "#/";
  if (state.detail) { for (const { chart } of Object.values(state.detail.charts)) chart.destroy(); state.detail = null; }
  state.tasksView = null; state.agentsView = null; state.alertsView = null;
  renderNav(); renderTopbar();
  let m;
  if ((m = hsh.match(/^#\/host\/(.+)$/))) renderDetail(decodeURIComponent(m[1]));
  else if (hsh.startsWith("#/tasks")) { const q = new URLSearchParams(hsh.split("?")[1] || ""); renderTasks({ host: q.get("host"), status: q.get("status"), open: q.get("open") }); }
  else if ((m = hsh.match(/^#\/agents(?:\/([^?]+))?(?:\?(.*))?$/))) { const q = new URLSearchParams(m[2] || ""); renderAgents({ host: m[1] ? decodeURIComponent(m[1]) : null, job: q.get("job") }); }
  else if (hsh.startsWith("#/alerts")) renderAlerts();
  else if ((m = hsh.match(/^#\/research(?:\/([^?]+))?(?:\?(.*))?$/))) { const q = new URLSearchParams(m[2] || ""); renderResearch({ project: m[1] ? decodeURIComponent(m[1]) : null, push: q.get("push") }); }
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
  await loadPrefs();
  Promise.all([loadTasks(), loadJobs(), loadTracked()]).then(() => { renderNav(); renderFleetStats(); }).catch(() => {});
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
