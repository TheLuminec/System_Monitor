"""Dashboard and installer follow-ups: read-only mode and per-host prompt caps in the UI (E-01/E-04 follow-ups, logic run
with node), E-27 wording, E-34 (root disk free in the summary), E-35 (claude's versioned binary named "claude"),
E-36 (chart tooltip escaping), E-19 (Windows install dir ACL, python -I), F-18 (read-only controls disabled and styled
so), F-19 (unset sensor limits) and F-20 (narrow host tables).

F-17: each test says whether it is behavioural (runs the code: pytest, or the dashboard JS with node and, for views.js, a
small fake DOM) or a static (source-text) check, and what it guards."""
import json
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest

import avalon_agent as a

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "server" / "avalon_monitor" / "static"
needs_node = pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")


def _node(js: str) -> dict:
    out = subprocess.run(["node", "--input-type=module", "-e", js], capture_output=True, text=True, timeout=30, check=True)
    return json.loads(out.stdout)


# ------------------------------------------------------------------ E-34
def test_summary_carries_root_disk_free(agent, client):
    """E-34 (behavioural, through the hub API): the host summary carries the root disk's free figure."""
    gb = 1024 ** 3
    disks = [{"device": "/dev/sda1", "mountpoint": "/", "fstype": "ext4", "total": 915 * gb, "used": 532 * gb,
              "free": 337 * gb, "percent": 61.2},
             {"device": "/dev/sdb1", "mountpoint": "/data", "fstype": "ext4", "total": 2000 * gb, "used": 10 * gb,
              "free": 1990 * gb, "percent": 0.5}]
    body = {"ts": time.time(), "host": {"hostname": agent.host_row.name, "agent_version": "1.8.1"}, "disks": disks}
    assert agent.post("/api/v1/ingest", json=body).status_code == 200
    hosts = client.get("/api/v1/hosts").json()["hosts"]
    disk = next(x for x in hosts if x["name"] == agent.host_row.name)["summary"]["disk"]
    # free excludes ext4's reserved blocks; total - used (383 GB here) does not
    assert disk["mountpoint"] == "/" and disk["free"] == 337 * gb and disk["total"] - disk["used"] == 383 * gb


@needs_node
def test_fleet_card_uses_free_js():
    """E-34 (behavioural, run with node): the fleet card's "free" is the agent's free figure, total - used only without it."""
    r = _node(f"""import {{ diskFree }} from {json.dumps((STATIC / "core.js").as_uri())};
console.log(JSON.stringify([diskFree({{ total: 915, used: 532, free: 337 }}), diskFree({{ total: 915, used: 532 }}),
  diskFree({{ total: 915, used: 532, free: 0 }}), diskFree({{ used: 5 }}), diskFree(null)]));""")
    assert r == [337, 383, 0, None, None]
    # static (source-text) check: the card actually uses it
    assert "fmt.bytes(diskFree(s.disk), 0)} free" in (STATIC / "app.js").read_text()


# ------------------------------------------------------------------ E-35
class _P:
    def __init__(self, pid, name, exe=None, cmdline=None):
        self.info = {"pid": pid, "name": name, "cpu_percent": 50.0 - pid, "memory_percent": 1.0,
                     "memory_info": None, "username": "me"}
        self._exe, self._cmd = exe, cmdline

    def exe(self):
        if self._exe is None:
            raise a.psutil.AccessDenied(self.info["pid"])
        return self._exe

    def cmdline(self):
        if self._cmd is None:
            raise a.psutil.AccessDenied(self.info["pid"])
        return self._cmd


def test_versioned_claude_binary_is_named_claude(monkeypatch):
    """E-35 (behavioural, the agent's collector with psutil faked)."""
    procs = {p.info["pid"]: p for p in [
        _P(1, "2.1.311", exe="/home/me/.local/share/claude/versions/2.1.311", cmdline=["claude", "--bg"]),
        _P(2, "2.1.300", exe=None, cmdline=["/home/me/.local/share/claude/versions/2.1.300", "-p", "x"]),   # exe denied
        _P(3, "2.1.311", exe=r"C:\Users\me\.local\share\claude\versions\2.1.311.exe", cmdline=None),       # Windows path
        _P(4, "python3", exe="/usr/bin/python3.12", cmdline=["python3", "/srv/claude/versions/x.py"]),      # only argv[0] counts
        _P(5, "1.2.3", exe="/opt/other/1.2.3", cmdline=["/opt/other/1.2.3"]),
        _P(6, "gone", exe=None, cmdline=None),
    ]}
    monkeypatch.setattr(a.psutil, "process_iter", lambda attrs: iter(procs.values()))
    monkeypatch.setattr(a.psutil, "Process", lambda pid: procs[pid])
    monkeypatch.setattr(a.psutil, "cpu_count", lambda logical=True: 1)
    names = {p["pid"]: p["name"] for p in a.collect_processes(10)}
    assert names == {1: "claude", 2: "claude", 3: "claude", 4: "python3", 5: "1.2.3", 6: "gone"}


# --------------------------------------------- read-only mode, prompt caps, E-36 (dashboard logic, run with node)
JS = """
import { state, canChange, roTip, modeAllowed, actionsAllowed, MODE_RANK } from %(core)s;
import { tipHtml } from %(charts)s;
const out = {};
out.noConfig = canChange();
state.config = { can_change: true };   out.access = canChange();
state.config = { can_change: false, public_url: "https://monitor.example.test" };   out.ro = canChange(); out.tip = roTip();
state.config = { can_change: false, public_url: "" };   out.tipNoUrl = roTip();
state.config = { version: "old hub" };   out.oldHub = canChange();
const modes = Object.keys(MODE_RANK);
const pol = (p) => Object.fromEntries(modes.map((m) => [m, modeAllowed(m, p)]));
out.unknown = pol(null);
out.plan = pol({ max_mode: "plan", actions: true, jobs: true, transcript: "full" });
out.edits = pol({ max_mode: "acceptEdits", actions: true, jobs: true });
out.full = pol({ max_mode: "full", actions: true, jobs: true });
out.bogus = pol({ max_mode: "everything", jobs: true });
out.root = pol({ max_mode: "full", actions: true, jobs: false });
out.act = [null, { actions: true, jobs: true }, { actions: false, jobs: true }, { actions: true, jobs: false }].map(actionsAllowed);
out.tipHtml = tipHtml("12:00 <b>", [{ color: 'red" onmouseover="x', label: "<img src=x onerror=alert(1)>", value: "5 & 6" }]);
console.log(JSON.stringify(out));
"""


@needs_node
def test_dashboard_permission_logic_js():
    """E-01/E-04 follow-ups (behavioural, run with node): read-only detection, its tooltip, and the per-host prompt caps."""
    r = _node(JS % {"core": json.dumps((STATIC / "core.js").as_uri()), "charts": json.dumps((STATIC / "charts.js").as_uri())})
    assert r["noConfig"] and r["access"] and not r["ro"] and r["oldHub"]
    assert r["tip"] == "read-only here: changes need your Cloudflare Access login - open https://monitor.example.test"
    assert r["tipNoUrl"] == "read-only here: changes need your Cloudflare Access login"
    assert r["unknown"] == {"plan": False, "acceptEdits": False, "full": False}   # older agents: refused by the hub (F-06)
    assert r["plan"] == {"plan": True, "acceptEdits": False, "full": False}
    assert r["edits"] == {"plan": True, "acceptEdits": True, "full": False}
    assert r["full"] == {"plan": True, "acceptEdits": True, "full": True}
    assert r["bogus"] == r["plan"]   # the agent itself treats an unknown cap as plan
    assert r["root"] == {"plan": False, "acceptEdits": False, "full": False}
    assert r["act"] == [False, True, False, False]   # no policy (older agent): no restart/stop offered


@needs_node
def test_chart_tooltip_escapes_everything_js():
    """E-36 (behavioural, run with node): the chart tooltip escapes labels, values, the time and the colour."""
    r = _node(JS % {"core": json.dumps((STATIC / "core.js").as_uri()), "charts": json.dumps((STATIC / "charts.js").as_uri())})
    tip = r["tipHtml"]
    assert "<img" not in tip and "<b>" not in tip and '" onmouseover' not in tip
    assert "&lt;img src=x onerror=alert(1)&gt;" in tip and "12:00 &lt;b&gt;" in tip and "5 &amp; 6" in tip
    assert 'style="background:red&quot; onmouseover=&quot;x"' in tip


def test_no_unescaped_interpolation_in_html_strings():
    """E-36 audit, static (source-text) check: every innerHTML / html: template string in the dashboard interpolates only
    escaped (esc(...)) or static values. Guards against a new raw ${...} in HTML; the escaping itself is run with node in
    test_chart_tooltip_escapes_everything_js."""
    allowed = re.compile(r"^(esc\(|x\.color$)")   # x.color: the static CHARTS definitions
    bad = []
    for f in ("app.js", "views.js", "charts.js", "research.js"):
        for i, line in enumerate((STATIC / f).read_text().splitlines(), 1):
            for m in re.finditer(r"(?:innerHTML\s*=|html:)\s*(`[^`]*`)", line):
                for expr in re.findall(r"\$\{([^}]*)\}", m.group(1)):
                    if not allowed.match(expr.strip()):
                        bad.append(f"{f}:{i}: ${{{expr}}}")
    assert not bad, bad


# ------------------------------------------------------------------ E-27
def test_responder_cap_wording_is_rolling_24h():
    """E-27, static (source-text) check: the responder cap is worded as a rolling 24 h window, never "per day" (the hub
    counts the last 24 h). Guards the wording only; there is no logic to run."""
    js = (STATIC / "app.js").read_text()
    assert "/day" not in js and "per day" not in js and "daily cap" not in js
    assert "alerts per rolling 24 h" in js and "held: " in js


# ------------------------------------------------------------------ E-19
def test_windows_installer_locks_down_install_dir_and_isolates_python():
    """E-19, static (source-text) check of install-windows.ps1 (PowerShell can't run here): the install dir gets its
    explicit ACL before anything is copied into it, and every python the installer or the scheduled task starts runs
    with -I."""
    ps = (ROOT / "agent" / "install" / "install-windows.ps1").read_text()
    acl = re.search(r'icacls \$AppDir /inheritance:r /grant:r "\*S-1-5-18:\(OI\)\(CI\)F" "\*S-1-5-32-544:\(OI\)\(CI\)F" '
                    r'"\*S-1-5-32-545:\(OI\)\(CI\)RX"', ps)
    assert acl, "explicit ACL on the install dir"
    # set before anything is copied or the venv is created in it
    assert acl.start() < ps.index('Copy-Item (Join-Path $SrcDir "avalon_agent.py")') < ps.index("-m venv")
    assert ps.index("New-Item -ItemType Directory -Force -Path $AppDir") < acl.start()
    # every python started by the installer or the scheduled task runs isolated
    assert '-Argument "-I `"$AppDir\\avalon_agent.py`"' in ps
    assert "& $vpy -I (Join-Path $AppDir \"avalon_agent.py\")" in ps
    assert "`\"$vpy`\" -I `\"$AppDir\\avm.py`\"" in ps
    launches = [ln for ln in ps.splitlines() if re.search(r"& \$v?py ", ln)]
    assert launches and all(" -I " in ln for ln in launches), launches
    assert "& $py -I -m venv" in ps


# ------------------------------------------------------------------ F-18: read-only controls are disabled, and look it
# A small fake DOM (just what core.js's h() and views.js touch), so views.js renders the real pages under node.
FAKE_DOM = """
class ClassList {
  constructor(el) { this.el = el; }
  get set() { return new Set(this.el.className.split(/\\s+/).filter(Boolean)); }
  add(...c) { const s = this.set; for (const x of c) s.add(x); this.el.className = [...s].join(" "); }
  remove(...c) { const s = this.set; for (const x of c) s.delete(x); this.el.className = [...s].join(" "); }
  toggle(c, force) { const want = force === undefined ? !this.set.has(c) : !!force; want ? this.add(c) : this.remove(c); return want; }
  contains(c) { return this.set.has(c); }
}
const simple = (el, sel) => {   // tag, #id, .class, [attr="v"] compounds; no combinators
  const m = sel.trim().match(/^([a-z]*)((?:[#.][\\w-]+|\\[[^\\]]+\\])*)$/i); if (!m) return false;
  if (m[1] && el.tagName !== m[1].toUpperCase()) return false;
  for (const p of m[2].match(/[#.][\\w-]+|\\[[^\\]]+\\]/g) || []) {
    if (p[0] === "#" && el.getAttribute("id") !== p.slice(1)) return false;
    if (p[0] === "." && !el.classList.contains(p.slice(1))) return false;
    if (p[0] === "[") {
      const [k, v] = p.slice(1, -1).split("=");
      const a = k.startsWith("data-") ? el.dataset[k.slice(5)] ?? el.getAttribute(k) : el.getAttribute(k);
      if (a == null || (v !== undefined && String(a) !== JSON.parse(v))) return false;
    }
  }
  return true;
};
class El {
  constructor(tag) { this.tagName = tag.toUpperCase(); this.nodeType = 1; this.attrs = {}; this.children = []; this.className = "";
    this.style = {}; this.dataset = {}; this.listeners = {}; this.classList = new ClassList(this); this.text = ""; this.value = ""; this.parentElement = null; }
  setAttribute(k, v) { this.attrs[k] = String(v); if (k === "value") this.value = String(v); }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  hasAttribute(k) { return k in this.attrs; }
  removeAttribute(k) { delete this.attrs[k]; }
  get disabled() { return this.hasAttribute("disabled"); }
  set disabled(v) { if (v) this.setAttribute("disabled", ""); else this.removeAttribute("disabled"); }
  get title() { return this.attrs.title ?? ""; }
  set title(v) { this.attrs.title = String(v); }
  addEventListener(t, f) { (this.listeners[t] ||= []).push(f); }
  append(...cs) { for (let c of cs.flat()) { if (c == null) continue; if (typeof c === "string") c = document.createTextNode(c); c.parentElement = this; this.children.push(c); } }
  before(...cs) { const p = this.parentElement; if (!p) return; const t = new El("x"); t.append(...cs); for (const c of t.children) c.parentElement = p; p.children.splice(p.children.indexOf(this), 0, ...t.children); }
  replaceChildren(...cs) { this.children = []; this.append(...cs); }
  set innerHTML(v) { this.children = []; this.html = String(v); }
  get innerHTML() { return this.html || ""; }
  set textContent(v) { this.children = []; this.html = ""; this.text = String(v); }
  get textContent() { return this.text + (this.html || "").replace(/<[^>]*>/g, "") + this.children.map((c) => c.textContent).join(""); }
  *walk() { for (const c of this.children) if (c.nodeType === 1) { yield c; yield* c.walk(); } }
  matches(sel) { return sel.split(",").some((s) => simple(this, s)); }
  closest(sel) { for (let e = this; e; e = e.parentElement) if (e.matches(sel)) return e; return null; }
  contains(e) { for (; e; e = e.parentElement) if (e === this) return true; return false; }
  querySelectorAll(sel) { return [...this.walk()].filter((e) => e.matches(sel)); }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
  focus() {}
  scrollIntoView() {}
}
const byId = {};
globalThis.document = {
  createElement: (t) => new El(t), createTextNode: (s) => ({ nodeType: 3, textContent: String(s) }),
  querySelector: (sel) => (sel[0] === "#" ? (byId[sel.slice(1)] ||= Object.assign(new El("div"), { attrs: { id: sel.slice(1) } })) : null),
  querySelectorAll: () => [], addEventListener() {}, dispatchEvent() {}, activeElement: null, documentElement: new El("html"),
};
globalThis.location = { hash: "" };
globalThis.confirm = () => false;
globalThis.localStorage = { getItem: () => null, setItem() {} };
"""

VIEWS_JS = FAKE_DOM + """
const { state } = await import(%(core)s);
const views = await import(%(views)s);
const now = Date.now() / 1000;
const policy = { max_mode: "full", actions: true, jobs: true, transcript: "full" };
const claude = { available: true, version: "2.1", user: "me", policy, features: ["agent_actions"],
  sessions: [{ session_id: "s9", cwd: "/w", first_prompt: "hello", last_activity: now - 60, active: false }],
  agents: [{ key: "k3", name: "other one", kind: "interactive", live: true, listed: true, last_activity: now }] };
const tracked = [
  { id: 1, host: "h1", key: "k1", session_id: "s1", name: "stopped one", kind: "interactive", live: false, state: "ended", cwd: "/w", last_activity: now - 30 },
  { id: 2, host: "h1", key: "k2", session_id: "s2", name: "live bg", kind: "background", live: true, state: "idle", cwd: "/w", last_activity: now }];
const jobs = [{ id: 7, host: "h1", status: "queued", title: "queued prompt", mode: "plan", created: now - 10 },
  { id: 8, host: "h1", status: "done", title: "done prompt", mode: "plan", created: now, session_id: "s8", prompt: "hi" },
  { id: 9, host: "h1", status: "failed", title: "failed prompt", mode: "plan", created: now - 20, ended: now - 15, error: "boom" }];
const task = { id: 5, host: "h1", name: "seed 3 training", status: "failed", rc: 1, started: now - 100, ended: now - 50, updated: now - 50, source: "run", meta: {} };
const R = { "/api/v1/claude/jobs?limit=200": { jobs }, "/api/v1/claude/tracked": { tracked },
  "/api/v1/hosts/h1": { sample: { claude } }, "/api/v1/claude/jobs/8": { job: jobs[1], events: [] },
  "/api/v1/tasks?limit=300": { tasks: [task] }, "/api/v1/tasks/5": { ...task, log: ["x"] } };
globalThis.fetch = async (path) => (path in R ? { ok: true, json: async () => R[path] }
  : { ok: false, status: 404, statusText: "unexpected " + path, json: async () => ({}) });
state.hosts.set("h1", { name: "h1", display_name: "h1", status: "online", claude: { available: true }, summary: {} });
const app = document.querySelector("#app");
const settle = () => new Promise((r) => setTimeout(r, 20));
const controls = () => app.querySelectorAll("button, textarea").map((b) => ({ text: b.textContent.trim(), tag: b.tagName, disabled: b.disabled, cls: b.className, title: b.title }));
const out = {};
for (const [mode, cfg] of [["ro", { can_change: false, public_url: "https://monitor.example.test" }], ["rw", { can_change: true }]]) {
  state.config = cfg;
  await views.renderAgents({ host: "h1" }); await settle(); out[mode + "_agents"] = controls();
  await views.renderAlerts(); await settle(); out[mode + "_alerts"] = controls();
  await views.renderTasks({ open: "5" }); await settle(); out[mode + "_tasks"] = controls();
}
console.log(JSON.stringify(out));
process.exit(0);   // views.js keeps interval timers
"""

def _views_js() -> str:
    # core.js under the exact specifier views.js imports it by (cache-busting query included): the same module instance,
    # so the test's `state` is the one views.js reads
    q = re.search(r'from "\./core\.js(\?[^"]*)?"', (STATIC / "views.js").read_text()).group(1) or ""
    return VIEWS_JS % {"core": json.dumps((STATIC / "core.js").as_uri() + q), "views": json.dumps((STATIC / "views.js").as_uri())}


WRITE_LABELS = {"Restart", "Stop", "Untrack", "Track", "Prompt it", "Send", "Cancel", "Continue this session",
                "Continue this conversation", "Dismiss", "Dismiss all task/prompt alerts", "Acknowledge", "Delete"}


@needs_node
def test_read_only_write_controls_are_disabled_js():
    """F-18 (behavioural, run with node on a fake DOM): views.js renders the Agents, Alerts and Tasks pages (a task open)
    for a read-only viewer and for one who can change things. Read-only: every write control there (Restart, Stop,
    Untrack, Track, Prompt it, Send and the prompt box, Cancel, Continue, Dismiss, Acknowledge, Delete) carries
    `disabled` and the read-only tooltip, and nothing else is disabled; with access the same controls are enabled, so
    the read-only result is not vacuous."""
    r = _node(_views_js())
    is_write = lambda c: c["text"] in WRITE_LABELS or c["tag"] == "TEXTAREA"   # noqa: E731
    for page in ("agents", "alerts", "tasks"):
        ro, rw = [c for c in r["ro_" + page] if is_write(c)], [c for c in r["rw_" + page] if is_write(c)]
        assert ro and len(ro) == len(rw), (page, ro, rw)
        assert all(c["disabled"] and c["title"].startswith("read-only here") for c in ro), (page, [c for c in ro if not c["disabled"]])
        assert not any(c["disabled"] for c in rw), (page, [c for c in rw if c["disabled"]])
        # navigation and view controls (Refresh, Expand all, ...) stay usable for a read-only viewer
        assert not [c for c in r["ro_" + page] if c["disabled"] and not is_write(c)], page
    seen = {c["text"] for p in ("agents", "alerts", "tasks") for c in r["ro_" + p]}
    assert WRITE_LABELS <= seen, WRITE_LABELS - seen   # the fixture really renders every one of them
    restart = next(c for c in r["ro_agents"] if c["text"] == "Restart")
    send = next(c for c in r["ro_agents"] if c["text"] == "Send")
    assert "primary" in restart["cls"] and "primary" in send["cls"]   # still .primary: the disabled CSS must win over it


def test_app_js_write_controls_are_guarded():
    """F-18, static (source-text) check for app.js (it boots the whole app on import, so it is not run here): every write
    control on the host page (Respawn agent, the checks panel's Add/Save/Edit/remove, host settings and their inputs)
    is wrapped in guard(), which sets `disabled` for a read-only viewer (guard itself runs in the test above), and
    app.js sends no write that is not behind one of them."""
    js = (STATIC / "app.js").read_text()
    # the button's label, or for the remove "×" its title (the panels' own "×" only hides a panel: a per-viewer layout pref)
    for mark in ('"Respawn agent")', '"Save" : "Add check")', '"Edit")', 'title: "remove this check"', '"Save settings")', '"+ Add check")'):
        at = js.index(mark)
        btn = js.rindex('h("button"', 0, at)   # the button the label/title belongs to
        assert js[btn - 6:btn] == "guard(", mark
    assert 'guard(h("input", { type: "text", value: cl.settings[k]' in js
    writes = re.findall(r"\bpost\(`([^`]*)`", js)
    assert sorted(writes) == sorted(["/api/v1/hosts/${encodeURIComponent(name)}/command/${cmd}", "/api/v1/checklist/${id}",
                                     "/api/v1/checklist/${c.id}", "/api/v1/hosts/${encodeURIComponent(name)}/checklist",
                                     "/api/v1/hosts/${encodeURIComponent(name)}/check-settings"]), writes


def test_disabled_controls_css():
    """F-18, static (source-text) check of app.css (no layout engine here): disabled buttons get a muted, non-primary
    look with selectors that outrank .btn.primary / .btn.danger / .btn.active / .btn:hover, and disabled fields are dimmed."""
    css = (STATIC / "app.css").read_text()
    m = re.search(r"^(button:disabled[^{]*)\{([^}]*)\}", css, re.M)
    assert m, "disabled button rule"
    sel, body = m.group(1), m.group(2)
    for s in (".btn[disabled]", ".btn.btn:disabled", ".btn.btn:disabled:hover", ".iconbtn.iconbtn:disabled"):
        assert s in sel, s
    for decl in ("background: var(--surface-2)", "border-color: var(--border)", "color: var(--ink-3)", "cursor: not-allowed", "opacity: 0.55"):
        assert decl in body, decl
    assert css.index(".btn.primary {") < m.start() and css.index(".btn.active {") < m.start()
    assert re.search(r"input:disabled, select:disabled, textarea:disabled \{[^}]*cursor: not-allowed", css)


# ------------------------------------------------------------------ F-19: unset sensor limits
@needs_node
def test_sensor_limit_unset_shows_dash_js():
    """F-19 (behavioural, run with node): a high/critical limit of 200 °C or more (an unset 0xFFFF kelvin limit from an
    older agent comes through as 65262 °C) shows as a dash; real limits as before."""
    r = _node(f"""import {{ fmt }} from {json.dumps((STATIC / "core.js").as_uri())};
console.log(JSON.stringify([65261.85, 200, 199.6, 80.85, 120, 0, null, undefined, NaN].map(fmt.tempLimit)));""")
    assert r == ["—", "—", "200°C", "81°C", "120°C", "0°C", "—", "—", "—"]
    # static (source-text) part: the Sensors table uses it for both limits; the current reading keeps fmt.temp
    js = (STATIC / "app.js").read_text()
    assert "fmt.temp(x.current)" in js and "fmt.tempLimit(x.high)" in js and "fmt.tempLimit(x.critical)" in js


# ------------------------------------------------------------------ F-20: narrow host tables
def test_narrow_host_tables_drop_low_value_columns():
    """F-20, static (source-text) check (no layout engine here): host-page table cards are size containers; below 640 px
    of card width the GPU Fan/Clock and Storage Type columns are hidden and the usage bar shrinks, below 460 px also GPU
    Power and Storage Size; a table still too wide scrolls with edge shadows and a visible scrollbar."""
    css = (STATIC / "app.css").read_text()
    assert ".tables > .table-card { container-type: inline-size; }" in css
    assert "@container (max-width: 640px) { .hide-narrow { display: none; } td .bar { width: 56px; } }" in css
    assert "@container (max-width: 460px) { .hide-xnarrow { display: none; } }" in css
    wrap = re.search(r"\.table-wrap \{([^}]*)\}", css).group(1)
    assert "overflow-x: auto" in wrap and "scrollbar-width: thin" in wrap
    assert wrap.count("no-repeat local") == 2 and wrap.count("no-repeat scroll") == 2
    js = (STATIC / "app.js").read_text()
    assert '"r:Power|hide-xnarrow", "r:Fan|hide-narrow", "r:Clock|hide-narrow"' in js
    assert '"Type|hide-narrow", "r:Used", "r:Free", "r:Size|hide-xnarrow"' in js
    # the header's class is on the matching body cells too
    assert js.count('class: "r num hide-narrow"') == 2 and js.count('class: "r num hide-xnarrow"') == 2
    assert 'class: "muted hide-narrow"' in js
