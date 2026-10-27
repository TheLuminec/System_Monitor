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
