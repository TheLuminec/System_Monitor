// Shared utilities, state, API client, live event bus and preferences.

export const $ = (sel, root = document) => root.querySelector(sel);
export const h = (tag, attrs = {}, ...children) => {
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
export const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
export const cssVar = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();

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
  when: (ts) => (ts ? new Date(ts * 1000).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "—"),
  load: (l) => (l ? l.map((x) => x.toFixed(2)).join(" · ") : "—"),
  usd: (v) => (v == null ? "—" : "$" + v.toFixed(v < 0.1 ? 3 : 2)),
};

export const ICON = {
  cpu: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="6" y="6" width="12" height="12" rx="2"/><path d="M9 2v3M15 2v3M9 19v3M15 19v3M2 9h3M2 15h3M19 9h3M19 15h3"/></svg>',
  down: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M12 5v14M5 12l7 7 7-7"/></svg>',
  up: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M12 19V5M5 12l7-7 7 7"/></svg>',
  disk: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><ellipse cx="12" cy="6" rx="8" ry="3"/><path d="M4 6v12c0 1.7 3.6 3 8 3s8-1.3 8-3V6"/></svg>',
  temp: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M14 14.8V5a2 2 0 0 0-4 0v9.8a4 4 0 1 0 4 0z"/></svg>',
  bolt: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"><path d="M13 2 3 14h8l-1 8 10-12h-8l1-8z"/></svg>',
  ok: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><path d="M5 13l4 4L19 7"/></svg>',
  bad: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg>',
  back: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M15 6l-6 6 6 6"/></svg>',
  grip: '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><circle cx="9" cy="6" r="1.6"/><circle cx="15" cy="6" r="1.6"/><circle cx="9" cy="12" r="1.6"/><circle cx="15" cy="12" r="1.6"/><circle cx="9" cy="18" r="1.6"/><circle cx="15" cy="18" r="1.6"/></svg>',
  fleet: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="4" width="18" height="12" rx="2"/><path d="M8 20h8M12 16v4"/></svg>',
  tasks: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M9 6h11M9 12h11M9 18h11M4 6h.01M4 12h.01M4 18h.01"/></svg>',
  agents: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8z"/><path d="M19 17l.7 2 2 .7-2 .7-.7 2-.7-2-2-.7 2-.7z"/></svg>',
  alerts: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l9.5 16.5H2.5L12 3z"/><path d="M12 10v4M12 17.5v.5"/></svg>',
  menu: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M4 7h16M4 12h16M4 17h16"/></svg>',
  send: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 2L11 13M22 2l-7 20-4-9-9-4z"/></svg>',
};

export function levelFor(p, warn = 80, crit = 92) { return p == null ? "" : p >= crit ? "crit" : p >= warn ? "warn" : ""; }
export function osLabel(host) { return host?.os_version || host?.os || "unknown"; }

export const RANGE_ORDER = ["15m", "1h", "6h", "24h", "7d", "30d"];
export const RANGE_SEC = { "15m": 900, "1h": 3600, "6h": 21600, "24h": 86400, "7d": 604800, "30d": 2592000 };

// ------------------------------------------------------------------ state
export const state = {
  config: null, hosts: new Map(), range: "1h", view: null, ws: null, live: false,
  spark: new Map(),      // host name -> [[ts, cpu]]
  detail: null,          // host page runtime
  tasks: new Map(),      // id -> task
  tasksLoaded: false,
  jobs: new Map(),       // id -> claude job
  jobsLoaded: false,
  jobEvents: new Map(),  // job id -> [events]
  prefs: {},             // server-side per-user layout prefs
  customizing: false,
};

export async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) {
    let msg = r.statusText;
    try { msg = (await r.json()).detail || msg; } catch (_) { /* ignore */ }
    throw new Error(`${r.status} ${msg}`);
  }
  return r.json();
}
export const hdrs = () => ({ "Content-Type": "application/json", "X-Requested-With": "avalon-monitor" });
export const post = (path, body, method = "POST") => api(path, { method, headers: hdrs(), body: body === undefined ? undefined : JSON.stringify(body) });

let toastTimer;
export function toast(msg, err = false) {
  const t = $("#toast"); t.textContent = msg; t.className = "toast show" + (err ? " err" : "");
  clearTimeout(toastTimer); toastTimer = setTimeout(() => (t.className = "toast"), err ? 7000 : 4000);
}

// -------------------------------------------------------------- event bus
const listeners = new Map();
export function on(type, fn) { (listeners.get(type) || listeners.set(type, new Set()).get(type)).add(fn); return () => listeners.get(type)?.delete(fn); }
export function emit(type, payload) { for (const fn of listeners.get(type) || []) { try { fn(payload); } catch (e) { console.error(e); } } }

// ------------------------------------------------------------------ prefs
let prefsTimer = null;
export function savePrefs() {
  try { localStorage.setItem("avm-prefs", JSON.stringify(state.prefs)); } catch (_) { /* ignore */ }
  clearTimeout(prefsTimer);
  prefsTimer = setTimeout(() => post("/api/v1/prefs", state.prefs, "PUT").catch((e) => toast("Could not save layout: " + e.message, true)), 600);
}
export async function loadPrefs() {
  let local = {};
  try { local = JSON.parse(localStorage.getItem("avm-prefs") || "{}"); } catch (_) { /* ignore */ }
  try {
    const r = await api("/api/v1/prefs");
    state.prefs = Object.keys(r.prefs || {}).length ? r.prefs : local;
  } catch (_) { state.prefs = local; }
  state.prefs.hostLayout ||= {};
  state.prefs.fleetOrder ||= [];
}
export function hostLayout(name) {
  const hl = state.prefs.hostLayout;
  return hl[name] || hl["*"] || { order: [], hidden: [] };
}
export function setHostLayout(name, layout) { state.prefs.hostLayout[name] = layout; savePrefs(); }

// ------------------------------------------------------------- sortable DnD
/** Make direct children matching `item` of `container` reorderable by dragging `handle` (or the item). */
export function makeSortable(container, { item, handle, onReorder }) {
  let dragging = null;
  container.addEventListener("mousedown", (e) => {
    const hd = handle ? e.target.closest(handle) : null;
    const it = e.target.closest(item);
    if (!it || !it.parentElement.isSameNode(container)) return;
    if (handle && !hd) { it.draggable = false; return; }
    it.draggable = true;
  });
  container.addEventListener("dragstart", (e) => {
    const it = e.target.closest(item);
    if (!it || !it.draggable) return;
    dragging = it; it.classList.add("dragging");
    e.dataTransfer.effectAllowed = "move";
    try { e.dataTransfer.setData("text/plain", it.dataset.id || ""); } catch (_) { /* ignore */ }
  });
  container.addEventListener("dragover", (e) => {
    if (!dragging) return;
    e.preventDefault(); e.dataTransfer.dropEffect = "move";
    const it = e.target.closest(item);
    for (const x of container.querySelectorAll(".drop-before, .drop-after")) x.classList.remove("drop-before", "drop-after");
    if (!it || it === dragging) return;
    const r = it.getBoundingClientRect();
    const before = (e.clientX - r.left) / r.width < 0.5;
    it.classList.add(before ? "drop-before" : "drop-after");
  });
  container.addEventListener("drop", (e) => {
    if (!dragging) return;
    e.preventDefault();
    const it = e.target.closest(item);
    for (const x of container.querySelectorAll(".drop-before, .drop-after")) x.classList.remove("drop-before", "drop-after");
    if (it && it !== dragging) {
      const r = it.getBoundingClientRect();
      const before = (e.clientX - r.left) / r.width < 0.5;
      it.insertAdjacentElement(before ? "beforebegin" : "afterend", dragging);
      onReorder([...container.querySelectorAll(item)].map((x) => x.dataset.id));
    }
  });
  container.addEventListener("dragend", () => {
    if (dragging) { dragging.classList.remove("dragging"); dragging.draggable = false; }
    dragging = null;
    for (const x of container.querySelectorAll(".drop-before, .drop-after")) x.classList.remove("drop-before", "drop-after");
  });
}

export const navigate = (hash) => { location.hash = hash; };
