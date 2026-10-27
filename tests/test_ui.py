"""Dashboard and installer follow-ups: read-only mode and per-host prompt caps in the UI (E-01/E-04 follow-ups, logic run
with node), E-27 wording, E-34 (root disk free in the summary), E-35 (claude's versioned binary named "claude"),
E-36 (chart tooltip escaping) and E-19 (Windows install dir ACL, python -I)."""
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


def test_fleet_card_uses_free():
    js = (STATIC / "app.js").read_text()
    assert "s.disk.free ??" in js


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
    r = _node(JS % {"core": json.dumps((STATIC / "core.js").as_uri()), "charts": json.dumps((STATIC / "charts.js").as_uri())})
    tip = r["tipHtml"]
    assert "<img" not in tip and "<b>" not in tip and '" onmouseover' not in tip
    assert "&lt;img src=x onerror=alert(1)&gt;" in tip and "12:00 &lt;b&gt;" in tip and "5 &amp; 6" in tip
    assert 'style="background:red&quot; onmouseover=&quot;x"' in tip


def test_no_unescaped_interpolation_in_html_strings():
    """E-36 audit: every innerHTML / html: string in the dashboard interpolates only escaped or static values."""
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
    js = (STATIC / "app.js").read_text()
    assert "/day" not in js and "per day" not in js and "daily cap" not in js
    assert "alerts per rolling 24 h" in js and "held: " in js


# ------------------------------------------------------------------ E-19
def test_windows_installer_locks_down_install_dir_and_isolates_python():
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
