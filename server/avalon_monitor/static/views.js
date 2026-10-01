// Tasks, Agents (Claude Code sessions + prompt console) and Alerts views.
import { $, h, esc, fmt, ICON, state, api, post, toast, on, navigate } from "./core.js?v=1.3.0";

// =================================================================== tasks
export async function loadTasks(force = false) {
  if (state.tasksLoaded && !force) return;
  const r = await api("/api/v1/tasks?limit=300");
  state.tasks.clear();
  for (const t of r.tasks) state.tasks.set(t.id, t);
  state.tasksLoaded = true;
}

const TASK_ORDER = { running: 0, stalled: 1, failed: 2, lost: 3, done: 4 };
function sortedTasks(filter = {}) {
  return [...state.tasks.values()]
    .filter((t) => (!filter.host || t.host === filter.host) && (!filter.status || t.status === filter.status))
    .sort((a, b) => (TASK_ORDER[a.status] ?? 9) - (TASK_ORDER[b.status] ?? 9) || b.started - a.started);
}

function taskRow(t, open = false) {
  const now = Date.now() / 1000;
  const elapsed = (t.ended || now) - t.started;
  const row = h("div", { class: `card task ${t.status}${open ? " open" : ""}`, "data-id": t.id });
  const statusText = { running: "running", done: "done", failed: `failed${t.rc != null ? " · rc " + t.rc : ""}`, stalled: `stalled · no heartbeat for ${fmt.dur(t.beat_age)}`, lost: "lost · host went offline" }[t.status] || t.status;
  row.append(
    h("span", { class: "dot" }),
    h("div", {},
      h("div", { class: "name" }, t.name),
      h("div", { class: "sub" }, h("span", {}, t.host), h("span", {}, statusText), t.command ? h("span", { class: "num", title: t.command }, t.command.length > 60 ? t.command.slice(0, 60) + "…" : t.command) : null, t.user ? h("span", {}, t.user) : null, t.note ? h("span", {}, t.note) : null),
    ),
    h("div", { class: "right num" }, h("b", { "data-f": "elapsed" }, fmt.dur(elapsed)), t.status === "running" ? `started ${fmt.when(t.started)}` : `${fmt.when(t.ended || t.updated)}`),
  );
  if (t.status === "running" && t.progress != null) row.append(h("div", { class: "prog" }, h("i", { style: `width:${Math.round(t.progress * 100)}%` })));
  const detail = h("div", { class: "detail" });
  row.append(detail);
  row.onclick = (e) => {
    if (e.target.closest("button, a")) return;
    row.classList.toggle("open");
    if (row.classList.contains("open")) fillTaskDetail(detail, t.id);
  };
  if (open) fillTaskDetail(detail, t.id);
  return row;
}

async function fillTaskDetail(el, id) {
  el.innerHTML = "";
  let t;
  try { t = await api(`/api/v1/tasks/${id}`); } catch (e) { el.append(h("div", { class: "muted" }, e.message)); return; }
  state.tasks.set(t.id, { ...state.tasks.get(t.id), ...t, log: undefined });
  const kv = [["Host", t.host], ["Source", `avm ${t.source}`], ["Command", t.command], ["Working dir", t.cwd], ["PID", t.pid], ["User", t.user], ["Started", fmt.when(t.started)], ["Ended", t.ended ? fmt.when(t.ended) : null],
    ["Heartbeat", t.expect_beat ? `every ≤${t.expect_beat}s · last ${fmt.ago(t.beat_age)}` : "none expected"], ["Exit code", t.rc], ["Progress", t.progress != null ? Math.round(t.progress * 100) + "%" : null], ["Note", t.note]].filter(([, v]) => v != null && v !== "");
  el.append(h("dl", { class: "kvlist" }, ...kv.flatMap(([k, v]) => [h("dt", {}, k), h("dd", { class: "num" }, String(v))])));
  const log = h("div", { class: "log", "data-f": "log" }, (t.log || []).join("\n") || "(no output captured)");
  el.append(h("div", { style: "margin-top:10px" }, h("div", { class: "muted", style: "font-size:11px;margin-bottom:4px" }, "last output lines"), log));
  log.scrollTop = log.scrollHeight;
  const actions = h("div", { class: "actions" });
  if (t.status !== "running") actions.append(h("button", { class: "btn small danger", onclick: async () => { try { await post(`/api/v1/tasks/${t.id}`, undefined, "DELETE"); } catch (e) { toast(e.message, true); } } }, "Delete"));
  if (t.status === "running" || t.status === "stalled") actions.append(h("span", { class: "muted", style: "font-size:12px" }, "live · updates as the task reports"));
  el.append(actions);
}

export async function renderTasks(params = {}) {
  const app = $("#app"); app.innerHTML = "";
  state.view = "tasks";
  try { await loadTasks(); } catch (e) { app.append(h("div", { class: "empty" }, `Cannot load tasks: ${e.message}`)); return; }
  const filter = { host: params.host || "", status: params.status || "" };
  const hostSel = h("select", { onchange: () => { filter.host = hostSel.value; redraw(); } }, h("option", { value: "" }, "all hosts"), ...[...state.hosts.keys()].sort().map((n) => h("option", { value: n, selected: n === filter.host ? "" : null }, n)));
  const statusSel = h("select", { onchange: () => { filter.status = statusSel.value; redraw(); } }, h("option", { value: "" }, "any status"), ...["running", "stalled", "failed", "lost", "done"].map((s) => h("option", { value: s, selected: s === filter.status ? "" : null }, s)));
  const list = h("div", { class: "task-list" });
  const counts = h("span", { class: "muted", "data-f": "counts" });
  const redraw = () => {
    list.innerHTML = "";
    const rows = sortedTasks(filter);
    const running = rows.filter((t) => t.status === "running").length;
    counts.textContent = `${running} running · ${rows.length} shown`;
    if (!rows.length) list.append(h("div", { class: "empty" }, "No tasks yet. Start one with the avm command below."));
    for (const t of rows) list.append(taskRow(t));
  };
  redraw();
  const howto = h("div", { class: "card howto" },
    h("b", {}, "Track anything with one command"), " — on any machine with the agent installed:",
    h("pre", {}, `avm run "seed 3 training" -- python train.py --seed 3     # wraps a command: output tail, heartbeat, exit code
avm watch "nightly backup" --pid 4242                     # follow an already-running process
avm task start "data prep" --expect 300   # -> TASK_ID     then: avm task beat ID --progress 0.4
avm task done ID    |    avm task fail ID --rc 1 --note "why"`),
    h("div", { class: "muted", style: "margin-top:8px;font-size:12px" }, "A running task that stops heart-beating turns amber (stalled); a non-zero exit turns red. Both count as alerts. Claude sessions started from the Agents tab are told to use avm run for long jobs."),
  );
  app.append(h("div", { class: "page-head" }, h("h2", {}, "Tasks"), counts, h("div", { class: "filters" }, hostSel, statusSel, h("button", { class: "btn small", onclick: async () => { await loadTasks(true); redraw(); } }, "Refresh"))), list, howto);
  state.tasksView = { list, filter, redraw };
}

on("task", (t) => {
  const prev = state.tasks.get(t.id);
  state.tasks.set(t.id, { ...(prev || {}), ...t });
  if (state.view === "tasks" && state.tasksView) {
    const existing = state.tasksView.list.querySelector(`.task[data-id="${t.id}"]`);
    const wasOpen = existing?.classList.contains("open");
    const f = state.tasksView.filter;
    if ((f.host && t.host !== f.host) || (f.status && t.status !== f.status)) { existing?.remove(); return; }
    const fresh = taskRow(state.tasks.get(t.id), wasOpen);
    if (existing) existing.replaceWith(fresh); else state.tasksView.redraw();
  }
  if (prev && prev.status !== t.status && (t.status === "failed" || t.status === "stalled")) toast(`Task "${t.name}" on ${t.host} ${t.status}`, true);
});
on("task_deleted", ({ id }) => { state.tasks.delete(id); if (state.view === "tasks" && state.tasksView) state.tasksView.redraw(); });

// elapsed timers tick
setInterval(() => {
  if (state.view !== "tasks") return;
  const now = Date.now() / 1000;
  for (const el of document.querySelectorAll(".task.running, .task.stalled")) {
    const t = state.tasks.get(Number(el.dataset.id)); if (!t) continue;
    const b = el.querySelector('[data-f="elapsed"]'); if (b) b.textContent = fmt.dur(now - t.started);
  }
}, 1000);

// ================================================================== agents
export async function loadJobs(force = false) {
  if (state.jobsLoaded && !force) return;
  const r = await api("/api/v1/claude/jobs?limit=200");
  state.jobs.clear();
  for (const j of r.jobs) state.jobs.set(j.id, j);
  state.jobsLoaded = true;
}

const MODE_LABEL = { plan: "Plan / read-only", acceptEdits: "Accept edits", full: "Full access" };

export async function renderAgents(params = {}) {
  const app = $("#app"); app.innerHTML = "";
  state.view = "agents";
  try { await loadJobs(); } catch (e) { app.append(h("div", { class: "empty" }, `Cannot load jobs: ${e.message}`)); return; }
  const hosts = [...state.hosts.values()].sort((a, b) => (a.status === "online" ? 0 : 1) - (b.status === "online" ? 0 : 1) || a.display_name.localeCompare(b.display_name));
  const sel = params.host && state.hosts.has(params.host) ? params.host : (hosts.find((x) => x.status === "online" && x.claude?.available)?.name || hosts[0]?.name);
  const left = h("div", {});
  const right = h("div", { class: "console" });
  app.append(h("div", { class: "page-head" }, h("h2", {}, "Agents"), h("span", { class: "muted" }, "Claude Code sessions on each machine · prompt them from here")), h("div", { class: "agents-grid" }, left, right));
  state.agentsView = { left, right, host: sel, job: params.job ? Number(params.job) : null };
  renderAgentHosts();
  await renderConsole();
}

function renderAgentHosts() {
  const v = state.agentsView; if (!v) return;
  v.left.innerHTML = "";
  const hosts = [...state.hosts.values()].sort((a, b) => (a.status === "online" ? 0 : 1) - (b.status === "online" ? 0 : 1) || a.display_name.localeCompare(b.display_name));
  for (const host of hosts) {
    const c = host.claude;
    const running = [...state.jobs.values()].filter((j) => j.host === host.name && j.status === "running").length;
    const queued = [...state.jobs.values()].filter((j) => j.host === host.name && j.status === "queued").length;
    const card = h("div", { class: "card agent-host" + (host.name === v.host ? " sel" : ""), onclick: () => navigate("#/agents/" + encodeURIComponent(host.name)) },
      h("div", { class: "row" }, h("span", { class: "status-dot " + host.status }), h("span", { class: "name" }, host.display_name),
        h("span", { class: "muted" }, c ? (c.available ? `claude ${c.version || ""}` : "no claude cli") : (host.status === "online" ? "agent < 1.3" : host.status))),
      h("div", { class: "row", style: "font-size:12px;color:var(--ink-2);gap:12px" },
        c ? h("span", {}, `${c.sessions} session${c.sessions === 1 ? "" : "s"} · ${c.active} active`) : null,
        running ? h("span", { style: "color:var(--c-blue)" }, `${running} running`) : null,
        queued ? h("span", { style: "color:var(--s-warn)" }, `${queued} queued`) : null,
        c?.user ? h("span", { class: "muted" }, "as " + c.user) : null),
      c?.running_job ? h("div", { class: "muted", style: "font-size:12px" }, "▶ " + c.running_job.title) : null,
    );
    v.left.append(card);
  }
}

async function renderConsole() {
  const v = state.agentsView; if (!v) return;
  v.right.innerHTML = "";
  const host = state.hosts.get(v.host);
  if (!host) { v.right.append(h("div", { class: "empty" }, "Enroll a host to prompt it.")); return; }
  let full;
  try { full = await api(`/api/v1/hosts/${encodeURIComponent(host.name)}`); } catch (e) { v.right.append(h("div", { class: "empty" }, e.message)); return; }
  const c = full.sample?.claude;
  const sessions = c?.sessions || [];
  const cwds = [...new Set(sessions.map((s) => s.cwd).filter(Boolean))];

  // ---- prompt box
  const ta = h("textarea", { placeholder: `Ask Claude on ${host.display_name}… (Ctrl+Enter to send)`, spellcheck: "true" });
  const mode = h("select", { title: "Permission mode" }, ...Object.entries(MODE_LABEL).map(([k, l]) => h("option", { value: k, class: k === "full" ? "mode-full" : "" }, l)));
  const cwd = h("input", { class: "cwd", list: "cwd-list", placeholder: "working directory (default: home)", value: cwds[0] || "", spellcheck: "false" });
  const dl = h("datalist", { id: "cwd-list" }, ...cwds.map((x) => h("option", { value: x })));
  const resume = h("select", { title: "Continue an existing session" }, h("option", { value: "" }, "new session"),
    ...sessions.slice(0, 15).map((s) => h("option", { value: s.session_id }, `${(s.first_prompt || s.session_id).slice(0, 48)} · ${fmt.ago(Date.now() / 1000 - s.last_activity)}`)));
  if (v.resumeSession) { resume.value = v.resumeSession; const s = sessions.find((x) => x.session_id === v.resumeSession); if (s?.cwd) cwd.value = s.cwd; }
  const model = h("input", { placeholder: "model (optional)", style: "width:150px", spellcheck: "false" });
  const sendBtn = h("button", { class: "btn primary", html: ICON.send + " Send" });
  const send = async () => {
    const prompt = ta.value.trim(); if (!prompt) return;
    const body = { host: host.name, prompt, cwd: cwd.value.trim() || null, mode: mode.value, model: model.value.trim() || null, resume_session: resume.value || null };
    if (mode.value === "full") {
      if (!confirm(`Run with FULL ACCESS on ${host.display_name}?\n\nThe session can edit files and run any command without asking. This is logged.`)) return;
      body.confirm_full = true;
    }
    sendBtn.disabled = true;
    try {
      const r = await post("/api/v1/claude/jobs", body);
      state.jobs.set(r.job.id, r.job); ta.value = ""; v.job = r.job.id; v.resumeSession = null;
      toast(`Queued for ${host.display_name} — the agent picks it up on its next report`);
      renderJobs(); await renderTranscript();
    } catch (e) { toast("Could not queue: " + e.message, true); }
    sendBtn.disabled = false;
  };
  sendBtn.onclick = send;
  ta.onkeydown = (e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); send(); } };
  const avail = !c ? h("div", { class: "muted", style: "font-size:12.5px" }, host.status === "online" ? "This host's agent predates Claude support (needs 1.3). It updates itself on its next report once the hub is upgraded." : `Host is ${host.status}; prompts queue until it reports.`)
    : !c.available ? h("div", { class: "chip bad" }, "claude CLI not found on this host") : null;
  v.right.append(h("div", { class: "card prompt-box" },
    h("div", { style: "display:flex;align-items:center;gap:8px" }, h("b", {}, host.display_name), h("span", { class: "muted", style: "font-size:12px" }, c ? `claude ${c.version || "?"} · runs as ${c.user || "?"}` : ""), h("span", { class: "spacer" }), avail),
    ta, dl,
    h("div", { class: "opts" }, mode, resume, cwd, model, sendBtn),
  ));

  // ---- recent sessions on the host
  if (sessions.length) {
    const box = h("div", { class: "sessions" });
    for (const s of sessions.slice(0, 8)) {
      box.append(h("div", { class: "session" },
        h("div", { class: "t" }, h("span", { class: "status-dot " + (s.active ? "online" : "never"), style: "width:7px;height:7px" }), h("span", { class: "proj", title: s.cwd }, s.cwd || s.project),
          h("span", { class: "muted", style: "margin-left:auto;white-space:nowrap" }, fmt.ago(Date.now() / 1000 - s.last_activity)), s.dashboard_job ? h("span", { class: "chip", style: "font-size:10px" }, "from dashboard") : null),
        s.first_prompt ? h("div", { class: "snip" }, h("b", {}, "started with: "), s.first_prompt) : null,
        s.last_assistant ? h("div", { class: "snip" }, h("b", {}, "last said: "), s.last_assistant) : (s.last_user ? h("div", { class: "snip" }, h("b", {}, "last asked: "), s.last_user) : null),
        h("div", { class: "actions" }, h("button", { class: "btn small", onclick: () => { resume.value = s.session_id; if (s.cwd) cwd.value = s.cwd; ta.focus(); toast("Next prompt continues this session"); } }, "Continue this session")),
      ));
    }
    v.right.append(h("div", { class: "card table-card" }, h("h3", {}, `Sessions on ${host.display_name}`, h("span", { class: "muted", style: "font-weight:400;font-size:12px;margin-left:8px" }, "last 24 h · from ~/.claude")), box));
  }

  // ---- jobs + transcript
  v.jobsEl = h("div", { class: "job-list" });
  v.transcriptEl = h("div", { class: "card transcript", "data-f": "transcript" });
  v.right.append(h("div", { class: "card table-card" }, h("h3", {}, "Prompts sent from the dashboard"), v.jobsEl), v.transcriptEl);
  renderJobs();
  await renderTranscript();
}

function renderJobs() {
  const v = state.agentsView; if (!v || !v.jobsEl) return;
  v.jobsEl.innerHTML = "";
  const jobs = [...state.jobs.values()].filter((j) => j.host === v.host).sort((a, b) => b.created - a.created).slice(0, 30);
  if (!jobs.length) { v.jobsEl.append(h("div", { class: "muted", style: "font-size:12.5px" }, "Nothing sent yet.")); return; }
  if (v.job == null) v.job = jobs[0].id;
  for (const j of jobs) {
    v.jobsEl.append(h("div", { class: `card job ${j.status}${j.id === v.job ? " sel" : ""}`, onclick: () => { v.job = j.id; renderJobs(); renderTranscript(); } },
      h("span", { class: "dot" }),
      h("div", {}, h("div", {}, j.title), h("div", { class: "meta" }, `${j.status} · ${MODE_LABEL[j.mode] || j.mode} · ${fmt.when(j.created)}${j.created_by ? " · " + j.created_by : ""}${j.cost_usd != null ? " · " + fmt.usd(j.cost_usd) : ""}`)),
      (j.status === "queued" || j.status === "running") ? h("button", { class: "btn small danger", onclick: async (e) => { e.stopPropagation(); try { await post(`/api/v1/claude/jobs/${j.id}/cancel`, {}); } catch (err) { toast(err.message, true); } } }, "Cancel") : h("span", { class: "muted num", style: "font-size:11px" }, j.num_turns != null ? `${j.num_turns} turn${j.num_turns === 1 ? "" : "s"}` : ""),
    ));
  }
}

async function renderTranscript() {
  const v = state.agentsView; if (!v || !v.transcriptEl) return;
  const el = v.transcriptEl; el.innerHTML = "";
  const j = v.job != null ? state.jobs.get(v.job) : null;
  if (!j) { el.append(h("div", { class: "muted" }, "Select a prompt to see its transcript.")); return; }
  let events = state.jobEvents.get(j.id);
  if (!events || j.status === "queued" || j.status === "running") {
    try { const r = await api(`/api/v1/claude/jobs/${j.id}`); events = r.events; state.jobEvents.set(j.id, events); state.jobs.set(j.id, r.job); } catch (e) { el.append(h("div", { class: "muted" }, e.message)); return; }
  }
  const head = h("div", { style: "display:flex;gap:10px;align-items:center;flex-wrap:wrap" },
    h("b", {}, `#${j.id}`), h("span", { class: "chip " + (j.status === "failed" ? "bad" : j.status === "done" ? "ok" : j.status === "running" ? "" : "warn") }, j.status),
    h("span", { class: "muted", style: "font-size:12px" }, `${MODE_LABEL[j.mode] || j.mode}${j.cwd ? " · " + j.cwd : ""}${j.session_id ? " · session " + j.session_id.slice(0, 8) : ""}${j.duration_ms ? " · " + fmt.dur(j.duration_ms / 1000) : ""}${j.cost_usd != null ? " · " + fmt.usd(j.cost_usd) : ""}`),
    h("span", { class: "spacer" }),
    j.session_id && (j.status === "done" || j.status === "failed") ? h("button", { class: "btn small", onclick: () => { v.resumeSession = j.session_id; renderConsole(); } }, "Continue this conversation") : null,
  );
  el.append(head, h("div", { class: "msg user" }, h("div", { class: "who" }, "prompt" + (j.created_by ? " · " + j.created_by : "")), h("div", { class: "body" }, j.prompt)));
  for (const ev of events) el.append(...eventNodes(ev));
  if (j.status === "running") el.append(h("div", { class: "msg assistant", "data-f": "live" }, h("div", { class: "body" }, h("span", { class: "cursor" }))));
  else if (j.status === "queued") el.append(h("div", { class: "muted" }, "Waiting for the agent to pick this up…"));
  if (j.error) el.append(h("div", { class: "result-box err" }, j.error));
  el.scrollTop = el.scrollHeight;
}

function eventNodes(ev) {
  const out = [];
  if (ev.type === "system" && ev.subtype === "init") out.push(h("div", { class: "muted", style: "font-size:11.5px" }, `session started · ${ev.model || ""}${ev.cwd ? " · " + ev.cwd : ""}${ev.permissionMode ? " · " + ev.permissionMode : ""}`));
  else if (ev.type === "assistant" || ev.type === "user") {
    for (const p of ev.parts || []) {
      if (p.type === "text" && p.text.trim()) out.push(h("div", { class: "msg " + ev.type }, h("div", { class: "who" }, ev.type === "assistant" ? "claude" : "tool results"), h("div", { class: "body" }, p.text)));
      else if (p.type === "thinking" && p.text.trim()) out.push(h("div", { class: "msg thinking" }, h("div", { class: "body" }, p.text)));
      else if (p.type === "tool_use") out.push(h("div", { class: "tool" }, h("b", {}, p.name + " "), p.input));
      else if (p.type === "tool_result") out.push(h("div", { class: "tool res" + (p.is_error ? " err" : "") }, p.text || "(no output)"));
    }
  } else if (ev.type === "result") out.push(h("div", { class: "result-box" + (ev.is_error ? " err" : "") }, ev.result || (ev.is_error ? "error" : "(done)")));
  return out;
}

on("claude_job", (j) => {
  const prev = state.jobs.get(j.id);
  state.jobs.set(j.id, j);
  if (state.view === "agents" && state.agentsView) {
    renderAgentHosts(); renderJobs();
    if (state.agentsView.job === j.id && prev?.status !== j.status) renderTranscript();
  }
  if (prev && prev.status !== j.status && (j.status === "done" || j.status === "failed")) toast(`Claude on ${j.host}: "${j.title}" ${j.status}`, j.status === "failed");
});
on("claude_event", ({ job_id, event }) => {
  const list = state.jobEvents.get(job_id);
  if (list) list.push(event);
  const v = state.agentsView;
  if (state.view === "agents" && v && v.job === job_id && v.transcriptEl) {
    const live = v.transcriptEl.querySelector('[data-f="live"]');
    const nodes = eventNodes(event);
    if (live) live.before(...nodes); else v.transcriptEl.append(...nodes);
    v.transcriptEl.scrollTop = v.transcriptEl.scrollHeight;
  }
});
on("host", () => { if (state.view === "agents" && state.agentsView) renderAgentHosts(); });

// ================================================================== alerts
export function computeAlerts() {
  const out = [];
  const now = Date.now() / 1000;
  for (const host of state.hosts.values()) {
    const s = host.summary || {};
    if (host.status === "offline") out.push({ level: "crit", what: `${host.display_name} is offline`, why: `last seen ${fmt.ago(host.age_sec)}`, host: host.name, go: `#/host/${encodeURIComponent(host.name)}`, ts: host.last_seen });
    for (const c of s.checks || []) if (!c.ok && c.level !== "info") out.push({ level: c.level === "warning" ? "warn" : "crit", what: `${c.kind === "rule" ? "" : c.kind + " check: "}${c.name}`, why: c.detail || "", host: host.name, go: `#/host/${encodeURIComponent(host.name)}`, ts: s.ts });
    if (host.agent_update_error) out.push({ level: "warn", what: "agent self-update failed", why: host.agent_update_error, host: host.name, go: `#/host/${encodeURIComponent(host.name)}`, ts: s.ts });
    if (s.memory?.committed != null && s.memory?.commit_limit && s.memory.committed / s.memory.commit_limit >= 0.85) out.push({ level: "warn", what: "memory commit pressure", why: `committed ${fmt.bytes(s.memory.committed)} of ${fmt.bytes(s.memory.commit_limit)}`, host: host.name, go: `#/host/${encodeURIComponent(host.name)}`, ts: s.ts });
  }
  for (const t of state.tasks.values()) {
    if (t.status === "failed" && now - (t.ended || t.updated) < 86400 * 3) out.push({ level: "crit", what: `task failed: ${t.name}`, why: `exit code ${t.rc ?? "?"} · ${fmt.ago(now - (t.ended || t.updated))}${t.note ? " · " + t.note : ""}`, host: t.host, go: "#/tasks", ts: t.ended });
    if (t.status === "stalled") out.push({ level: "warn", what: `task stalled: ${t.name}`, why: `no heartbeat for ${fmt.dur(now - t.heartbeat)}`, host: t.host, go: "#/tasks", ts: t.heartbeat });
    if (t.status === "lost") out.push({ level: "warn", what: `task lost: ${t.name}`, why: "its host went offline while it was running", host: t.host, go: "#/tasks", ts: t.updated });
  }
  for (const j of state.jobs.values()) if (j.status === "failed" && now - (j.ended || j.created) < 86400) out.push({ level: "warn", what: `claude prompt failed: ${j.title}`, why: (j.error || "").slice(0, 160), host: j.host, go: `#/agents/${encodeURIComponent(j.host)}`, ts: j.ended });
  return out.sort((a, b) => (a.level === "crit" ? 0 : 1) - (b.level === "crit" ? 0 : 1) || (b.ts || 0) - (a.ts || 0));
}

export async function renderAlerts() {
  const app = $("#app"); app.innerHTML = "";
  state.view = "alerts";
  try { await Promise.all([loadTasks(), loadJobs()]); } catch (_) { /* partial is fine */ }
  const list = h("div", { class: "alert-list" });
  const redraw = () => {
    list.innerHTML = "";
    const alerts = computeAlerts();
    if (!alerts.length) list.append(h("div", { class: "empty" }, "All quiet. Offline hosts, failing checks, stalled or failed tasks and failed prompts show up here."));
    for (const a of alerts) list.append(h("div", { class: "card alert " + a.level, onclick: () => navigate(a.go) }, h("span", { class: "dot" }), h("div", {}, h("div", { class: "what" }, a.what), h("div", { class: "why" }, a.why)), h("div", { class: "host" }, a.host)));
    emit_badges();
  };
  redraw();
  app.append(h("div", { class: "page-head" }, h("h2", {}, "Alerts"), h("span", { class: "muted" }, "what needs attention right now")), list);
  state.alertsView = { redraw };
}
const emit_badges = () => document.dispatchEvent(new CustomEvent("avm-badges"));
for (const t of ["host", "task", "claude_job", "task_deleted"]) on(t, () => { if (state.view === "alerts" && state.alertsView) state.alertsView.redraw(); else emit_badges(); });
