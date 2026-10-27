"""deploy/deploy-hub.sh against a throwaway world: a temp git repo with a bare "origin", a temp deploy root, fake
`systemctl`/`logger` (and, for one test, `rsync`) earlier on PATH, and a tiny HTTP server standing in for the hub.

The fake hub serves the version that was on disk at the last fake `systemctl restart`, like a real restarted process,
and on every request notes which agent/ is in the deploy root: that is what the real hub would hand out to hosts.
"""
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "deploy-hub.sh"
UNIT = "[Unit]\nDescription=fake unit\n"
OLD_AGENT = 'AGENT_VERSION = "old"\n'
NEW_AGENT = 'AGENT_VERSION = "new"\n'

pytestmark = pytest.mark.skipif(not (shutil.which("git") and shutil.which("rsync") and shutil.which("curl")),
                                reason="needs git, rsync and curl")


def version_file(v):
    return f'__version__ = "{v}"\n'


class FakeHub:
    """`broken` maps a version to the endpoint that fails while that version is live."""

    def __init__(self, root: Path):
        self.root = root
        self.version = self.read_version()
        self.broken: dict[str, str] = {}
        self.agents_seen: list[str] = []
        hub = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                hub.agents_seen.append((hub.root / "agent" / "avalon_agent.py").read_text())
                bad = hub.broken.get(hub.version)
                if self.path == "/healthz":
                    self.reply(503 if bad == "healthz" else 200, {"ok": True, "version": hub.version})
                elif self.path == "/":
                    if bad == "index":
                        self.reply(200, {"detail": "not html"})
                    else:
                        self.reply(200, b"<!doctype html><title>Avalon</title>", "text/html; charset=utf-8")
                elif self.path == "/api/v1/hosts":
                    self.reply(200, {"detail": "nope"} if bad == "hosts" else {"hosts": [], "server_time": 1.0})
                elif self.path == "/api/v1/stats":
                    self.reply(500 if bad == "stats" else 200, {"hosts": 0, "raw_rows": 0, "rollup_rows": 0})
                elif self.path == "/_restart":   # sent by the fake systemctl
                    hub.version = hub.read_version()
                    self.reply(200, {})
                else:
                    self.reply(404, {})

            def reply(self, code, body, ctype="application/json"):
                data = body if isinstance(body, bytes) else json.dumps(body, separators=(",", ":")).encode()  # like FastAPI
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def read_version(self):
        text = (self.root / "server" / "avalon_monitor" / "__init__.py").read_text()
        return text.split('"')[1]


class World:
    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
                    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.test",
                    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.test"}
        self.origin, self.repo, self.root = tmp / "origin.git", tmp / "repo", tmp / "opt"
        self.git("init", "-q", "--bare", "-b", "main", str(self.origin), cwd=tmp)
        self.git("init", "-q", "-b", "main", str(self.repo), cwd=tmp)
        self.git("remote", "add", "origin", str(self.origin))
        self.write("deploy/deploy-hub.sh", SCRIPT.read_text())
        self.write("deploy/avalon-monitor.service", UNIT)
        self.write("server/requirements.txt", "fastapi\n")
        self.write("server/avalon_monitor/web.py", "NEW = 1\n")
        self.write("agent/avalon_agent.py", NEW_AGENT)
        self.write("tests/run.sh", "#!/bin/bash\necho fake suite ok\n")
        self.commit("v1.2.0", version="1.2.0", push=True)

        # what is deployed now: v1.1.0 with an old file the deploy must remove and data it must keep. Back-dated like
        # an earlier deploy: same size + same mtime second as the archive would make rsync skip a changed file
        for rel, text in {"server/avalon_monitor/__init__.py": version_file("1.1.0"),
                          "server/requirements.txt": "fastapi\n", "server/avalon_monitor/old.py": "OLD = 1\n",
                          "server/monitor.db": "data", "agent/avalon_agent.py": OLD_AGENT}.items():
            (self.root / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.root / rel).write_text(text)
            os.utime(self.root / rel, (1_600_000_000, 1_600_000_000))
        (tmp / "units").mkdir()
        (tmp / "units" / "avalon-monitor.service").write_text(UNIT)

        self.hub = FakeHub(self.root)
        self.bin = tmp / "bin"
        self.bin.mkdir()
        self.calls = tmp / "systemctl.log"
        self.shim("systemctl", f'echo "$*" >> {self.calls}\n[[ -z "${{FAKE_SYSTEMCTL_FAIL:-}}" ]] || exit 1\n'
                               f'curl -fsS -o /dev/null {self.hub.url}/_restart\n')
        self.shim("logger", "exit 0\n")
        self.env.update({"PATH": f"{self.bin}:{os.environ['PATH']}", "XDG_STATE_HOME": str(tmp / "state"),
                         "AVM_DEPLOY_ROOT": str(self.root), "AVM_DEPLOY_UNIT_DIR": str(tmp / "units"),
                         "AVM_DEPLOY_HEALTH_URL": self.hub.url, "AVM_DEPLOY_HEALTH_TRIES": "2",
                         "AVM_DEPLOY_BACKUP_AVM": str(tmp / "no-backup-avm.py"),   # not this box's real one
                         "AVM_TEST_PYTHON": sys.executable})

    def git(self, *args, cwd=None):
        return subprocess.run(["git", *args], cwd=cwd or self.repo, env=self.env, check=True,
                              capture_output=True, text=True).stdout.strip()

    def write(self, rel, text):
        p = self.repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)

    def commit(self, msg, version=None, push=False):
        if version:
            self.write("server/avalon_monitor/__init__.py", version_file(version))
        self.git("add", "-A")
        self.git("commit", "-q", "-m", msg)
        if push:
            self.git("push", "-q", "origin", "main")
        return self.git("rev-parse", "HEAD")

    def shim(self, name, body):
        p = self.bin / name
        p.write_text("#!/bin/bash\n" + body)
        p.chmod(0o755)

    def deploy(self, *args, env=None):
        return subprocess.run(["bash", str(self.repo / "deploy" / "deploy-hub.sh"), *args], env={**self.env, **(env or {})},
                              capture_output=True, text=True, timeout=120)

    def restarts(self):
        return self.calls.read_text().count("restart") if self.calls.exists() else 0

    def log(self, name):
        return (self.tmp / "state" / "avalon-monitor-deploy" / name).read_text()

    def deployed(self):
        return self.hub.read_version()

    def agent(self):
        return (self.root / "agent" / "avalon_agent.py").read_text()


@pytest.fixture
def world(tmp_path):
    w = World(tmp_path)
    yield w
    w.hub.server.shutdown()


def test_success_puts_agent_in_place_last(world):
    r = world.deploy("main")
    assert r.returncode == 0, r.stderr
    assert world.deployed() == "1.2.0" and world.hub.version == "1.2.0"
    assert (world.root / "server/avalon_monitor/web.py").exists()
    assert not (world.root / "server/avalon_monitor/old.py").exists()
    assert (world.root / "server/monitor.db").read_text() == "data"
    assert world.restarts() == 1
    # every request the restarted hub answered (health + smoke test) still found the old agent on disk ...
    assert world.hub.agents_seen and set(world.hub.agents_seen) == {OLD_AGENT}
    # ... and the new one is in place afterwards
    assert world.agent() == NEW_AGENT


def test_refuses_ref_not_on_origin_main(world):
    world.write("server/avalon_monitor/web.py", "UNPUSHED = 1\n")
    world.commit("local only", version="1.3.0")
    r = world.deploy("main")
    assert r.returncode != 0
    assert "not on origin/main" in r.stderr
    assert world.deployed() == "1.1.0" and world.restarts() == 0


def test_fetches_before_resolving(world):
    """origin/main moved on elsewhere: deploying origin/main ships what is on origin now, not a stale copy."""
    other = world.tmp / "other"
    world.git("clone", "-q", str(world.origin), str(other), cwd=world.tmp)
    (other / "server/avalon_monitor/__init__.py").write_text(version_file("1.4.0"))
    world.git("commit", "-q", "-am", "elsewhere", cwd=other)
    world.git("push", "-q", "origin", "main", cwd=other)
    r = world.deploy("origin/main")
    assert r.returncode == 0, r.stderr
    assert world.deployed() == "1.4.0"


def test_refuses_downgrade_unless_forced(world):
    (world.root / "server/avalon_monitor/__init__.py").write_text(version_file("1.10.0"))  # 1.2.0 < 1.10.0
    world.hub.version = "1.10.0"
    r = world.deploy("main")
    assert r.returncode != 0
    assert "older than the deployed v1.10.0" in r.stderr
    assert world.deployed() == "1.10.0" and world.restarts() == 0 and world.agent() == OLD_AGENT

    r = world.deploy("--force", "main")
    assert r.returncode == 0, r.stderr
    assert "downgrading" in r.stderr
    assert world.deployed() == "1.2.0"


def test_failed_copy_restores_old_tree(world):
    real = shutil.which("rsync")
    # copy everything, then fail as if the disk filled up: only copies out of the stage dir fail
    world.shim("rsync", f'{real} "$@"; rc=$?\nfor a in "$@"; do [[ "$a" == */stage.*/server/ ]] && exit 23; done\nexit $rc\n')
    r = world.deploy("main")
    assert r.returncode != 0
    assert "restoring the previous server/" in r.stderr
    assert world.deployed() == "1.1.0"
    assert (world.root / "server/avalon_monitor/old.py").exists()
    assert not (world.root / "server/avalon_monitor/web.py").exists()
    assert (world.root / "server/monitor.db").read_text() == "data"
    assert world.restarts() == 0 and world.agent() == OLD_AGENT


@pytest.mark.parametrize("endpoint", ["healthz", "index", "hosts", "stats"])
def test_unhealthy_release_rolls_back_without_touching_agent(world, endpoint):
    world.hub.broken = {"1.2.0": endpoint}
    r = world.deploy("main")
    assert r.returncode != 0
    assert "rolled back; previous version is live" in r.stderr
    assert world.deployed() == "1.1.0" and world.hub.version == "1.1.0"
    assert (world.root / "server/avalon_monitor/old.py").exists()
    assert world.restarts() == 2
    # the agent was never replaced, not even while the broken release was live
    assert set(world.hub.agents_seen) == {OLD_AGENT} and world.agent() == OLD_AGENT


def test_failing_test_suite_refuses_deploy(world):
    world.write("tests/run.sh", "#!/bin/bash\necho 'FAIL  pytest'\nexit 1\n")
    world.commit("broken tests", push=True)
    world.write("tests/run.sh", "#!/bin/bash\nexit 0\n")   # uncommitted: the committed suite is what counts
    r = world.deploy("main")
    assert r.returncode != 0
    assert "test suite failed" in r.stderr and "FAIL  pytest" in r.stderr
    assert world.deployed() == "1.1.0" and world.restarts() == 0

    r = world.deploy("--skip-tests", "main")
    assert r.returncode == 0, r.stderr
    assert "WITHOUT running the test suite" in r.stderr
    assert world.deployed() == "1.2.0"


def test_rollback_puts_back_the_previous_release_hub_first(world):
    """F-09: --rollback restored agent/ before the restart, so the hosts got the old agent while the hub that serves it
    was not yet known to come back. Now, as in a deploy (E-13): server/, restart, health check, and only then agent/.
    F-08: the copy undoes one deploy - once it is used, a second --rollback is refused."""
    assert world.deploy("main").returncode == 0
    assert world.agent() == NEW_AGENT
    world.hub.agents_seen.clear()
    r = world.deploy("--rollback")
    assert r.returncode == 0, r.stderr
    assert "hub v1.1.0 is live" in r.stdout
    assert world.deployed() == "1.1.0" and world.hub.version == "1.1.0" and world.restarts() == 2
    assert (world.root / "server/avalon_monitor/old.py").exists() and (world.root / "server/monitor.db").read_text() == "data"
    # every request the rolled-back hub answered (the health check) still found the newer agent on disk ...
    assert world.hub.agents_seen and set(world.hub.agents_seen) == {NEW_AGENT}
    assert world.agent() == OLD_AGENT   # ... and the old one is back afterwards

    r = world.deploy("--rollback")
    assert r.returncode != 0 and "does not belong to what is live now" in r.stderr
    assert world.restarts() == 2


def test_rollback_refuses_a_copy_that_does_not_belong_to_the_live_release(world):
    """F-08: since the deploy, something else (a sudo upgrade.sh) put v1.3.0 live. The rollback copy undoes v1.2.0, not
    that, so --rollback refuses and touches nothing - unless --force."""
    assert world.deploy("main").returncode == 0
    (world.root / "server/avalon_monitor/__init__.py").write_text(version_file("1.3.0"))
    world.hub.version = "1.3.0"
    r = world.deploy("--rollback")
    assert r.returncode != 0
    assert "does not belong to what is live now" in r.stderr and "has v1.3.0" in r.stderr and "--force" in r.stderr
    assert world.deployed() == "1.3.0" and world.restarts() == 1 and world.agent() == NEW_AGENT

    r = world.deploy("--force", "--rollback")
    assert r.returncode == 0, r.stderr
    assert "--force: rolling back although" in r.stderr
    assert world.deployed() == "1.1.0" and world.agent() == OLD_AGENT


def test_failed_deploy_keeps_the_rollback_copy(world):
    """F-08: a deploy that failed its health check had already replaced the rollback copy with the release that stayed
    live, so a later --rollback "restored" what was running anyway. The copy of the last good deploy is kept."""
    assert world.deploy("main").returncode == 0   # 1.1.0 -> 1.2.0
    world.write("server/avalon_monitor/web.py", "NEWER = 1\n")
    world.commit("v1.3.0", version="1.3.0", push=True)
    world.hub.broken = {"1.3.0": "healthz"}
    r = world.deploy("main")
    assert r.returncode != 0 and "rolled back; previous version is live" in r.stderr
    assert world.deployed() == "1.2.0"

    r = world.deploy("--rollback")
    assert r.returncode == 0, r.stderr
    assert world.deployed() == "1.1.0" and world.agent() == OLD_AGENT
    assert "rollback: to v1.1.0" in world.deploy("--status").stdout


@pytest.mark.parametrize("mode", ["fails", "hangs"])
def test_unreachable_origin_falls_back_to_the_last_fetch(world, mode):
    """F-10: `git fetch` had no time limit and could prompt on the terminal. It now runs with GIT_TERMINAL_PROMPT=0
    under a timeout (AVM_DEPLOY_FETCH_TIMEOUT, 30 s); one that fails or hangs falls back to the last fetched
    origin/main (the offline rule as before), and a hang costs no more than the timeout."""
    real, seen = shutil.which("git"), world.tmp / "fetch-env"
    act = "exit 128" if mode == "fails" else "exec sleep 30"
    world.shim("git", f'for a in "$@"; do [[ "$a" != fetch ]] || {{ echo "prompt=${{GIT_TERMINAL_PROMPT:-unset}}" > {seen}; {act}; }}; done\n'
                      f'exec {real} "$@"\n')
    t = time.monotonic()
    r = world.deploy("main", env={"AVM_DEPLOY_FETCH_TIMEOUT": "1"})
    assert time.monotonic() - t < 20
    assert r.returncode == 0, r.stderr
    assert "cannot fetch origin" in r.stderr and world.deployed() == "1.2.0"
    assert seen.read_text().strip() == "prompt=0"


FAKE_SUITE = """#!/bin/bash
# stand-in for tests/run.sh: pytest only, with its flags and its summary line
cd "$(dirname "$0")/.."
if "$AVM_TEST_PYTHON" -m pytest tests -q -p no:warnings; then echo "ok    pytest"
else echo "FAIL  pytest" >&2; echo "tests/run.sh: 1 check(s) FAILED: pytest" >&2; exit 1; fi
echo "tests/run.sh: all checks passed"
"""
FLAKY = """import os, pathlib
D = pathlib.Path(os.environ["FLAKY_DIR"])
def test_solid():
    with open(D / "solid", "a") as f:
        f.write("x")
def test_flaky():
    first = not (D / "flaky").exists()
    (D / "flaky").write_text("ran")
    assert not (first or os.environ.get("FLAKY_ALWAYS")), "fails"
"""


@pytest.mark.parametrize("always", [False, True])
def test_failed_tests_are_retried_once(world, always):
    """F-11: a failure of pytest alone gets one retry of only the failed tests (pytest --lf): a flaky test then passes
    and the deploy goes ahead (logged as flaky), a real failure fails twice and refuses. Both attempts are in tests.log."""
    world.write("tests/run.sh", FAKE_SUITE)
    world.write("tests/test_flaky.py", FLAKY)
    world.commit("a flaky test", push=True)
    flaky = world.tmp / "flaky"
    flaky.mkdir()
    r = world.deploy("main", env={"FLAKY_DIR": str(flaky), **({"FLAKY_ALWAYS": "1"} if always else {})})
    log = world.log("tests.log")
    assert "retry of the failed tests only: tests/test_flaky.py::test_flaky" in log
    assert (flaky / "solid").read_text() == "x"   # the retry ran only the test that failed
    if always:
        assert r.returncode != 0 and "test suite failed" in r.stderr
        assert log.count("FAILED tests/test_flaky.py::test_flaky") == 2
        assert world.deployed() == "1.1.0" and world.restarts() == 0
    else:
        assert r.returncode == 0, r.stderr
        assert world.deployed() == "1.2.0"
        assert "flaky: pytest failed" in world.log("deploy.log")


def test_hung_test_suite_times_out(world):
    """F-11: tests/run.sh ran without a time limit, so a hung test held the deploy forever. It gets one now (600 s,
    AVM_DEPLOY_TESTS_TIMEOUT); a suite that runs out of time refuses the deploy and is not retried."""
    world.write("tests/run.sh", "#!/bin/bash\necho started\nsleep 30\n")
    world.commit("hangs", push=True)
    t = time.monotonic()
    r = world.deploy("main", env={"AVM_DEPLOY_TESTS_TIMEOUT": "1"})
    assert time.monotonic() - t < 20
    assert r.returncode != 0 and "timed out after 1s" in r.stderr and "retry" not in world.log("tests.log")
    assert world.deployed() == "1.1.0" and world.restarts() == 0


def test_skip_tests_still_runs_the_static_checks(world):
    """F-11: --skip-tests skipped tests/run.sh altogether. It now runs it with AVM_TEST_SKIP_PYTEST=1 (bash -n,
    node --check, py_compile: seconds), and a failing static check still refuses the deploy."""
    world.write("tests/run.sh", '#!/bin/bash\necho "pytest skipped: ${AVM_TEST_SKIP_PYTEST:-no}"\necho "FAIL  bash -n" >&2\nexit 1\n')
    world.commit("a syntax error", push=True)
    r = world.deploy("--skip-tests", "main")
    assert r.returncode != 0
    assert "static checks failed" in r.stderr and "pytest skipped: 1" in r.stderr and "FAIL  bash -n" in r.stderr
    assert world.deployed() == "1.1.0" and world.restarts() == 0


@pytest.mark.skipif(not shutil.which("node"), reason="tests/run.sh's static checks need node")
def test_run_sh_can_skip_only_pytest(tmp_path):
    """F-11: AVM_TEST_SKIP_PYTEST=1 makes tests/run.sh run its static checks without pytest (what --skip-tests runs).
    The bogus AVM_TEST_PYTHON makes a run.sh that still tried pytest fail, instead of recursing into this suite."""
    r = subprocess.run(["bash", str(ROOT / "tests" / "run.sh")], capture_output=True, text=True, timeout=300,
                       env={**os.environ, "AVM_TEST_SKIP_PYTEST": "1", "AVM_TEST_PYTHON": str(tmp_path / "no-python")})
    assert r.returncode == 0, r.stdout + r.stderr
    assert "skip  pytest" in r.stdout and "ok    bash -n" in r.stdout and "ok    py_compile" in r.stdout


def test_agent_signatures_go_in_place_before_the_code(world):
    """F-22: a single rsync renames in name order, so agent/avalon_agent.py went live before avalon_agent.py.sig and
    for a moment the hub served the new code with the old signature. The *.sig files now land first."""
    world.write("agent/avalon_agent.py.sig", "new-sig\n")
    world.commit("signed", push=True)
    (world.root / "agent/avalon_agent.py.sig").write_text("old-sig\n")
    os.utime(world.root / "agent/avalon_agent.py.sig", (1_600_000_000, 1_600_000_000))   # an earlier deploy (see World)
    real, order = shutil.which("rsync"), world.tmp / "rsync-order"
    # copies INTO the deployed agent/ list what they replace, in the order they replace it
    world.shim("rsync", f'[[ "${{!#}}" != "{world.root}/agent/" ]] || {{ {real} --out-format=%n "$@" >> {order}; exit; }}\n'
                        f'exec {real} "$@"\n')
    r = world.deploy("main")
    assert r.returncode == 0, r.stderr
    names = order.read_text().split()
    assert names.index("avalon_agent.py.sig") < names.index("avalon_agent.py"), names
    assert world.agent() == NEW_AGENT and (world.root / "agent/avalon_agent.py.sig").read_text() == "new-sig\n"


@pytest.mark.parametrize("copy", ["1.9.0", "1.9.1", None])
def test_warns_when_the_backup_services_avm_is_stale(world, copy):
    """F-05: the root backup unit runs its own copy of avm (from install-restic-backup.sh) that no deploy refreshes. A
    deploy whose agent/avm.py has another AVM_VERSION warns - it never fails - and prints the command that refreshes
    it; a current copy, or none, says nothing."""
    world.write("agent/avm.py", 'AVM_VERSION = "1.9.1"\n')
    world.commit("avm", push=True)
    backup = world.tmp / "backup-avm.py"
    if copy:
        backup.write_text(f'#!/usr/bin/python3\nAVM_VERSION = "{copy}"\n')
    r = world.deploy("main", env={"AVM_DEPLOY_BACKUP_AVM": str(backup)})
    assert r.returncode == 0, r.stderr
    stale = copy == "1.9.0"
    assert ("runs its own copy of avm" in r.stderr) == stale, r.stderr
    if stale:
        assert f"({backup}, v1.9.0), not this release's v1.9.1" in r.stderr
        assert f"sudo {world.repo}/deploy/backup/install-restic-backup.sh /etc/avalon-backup/config.json" in r.stderr
