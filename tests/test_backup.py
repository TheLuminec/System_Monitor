"""The nightly backup runner (deploy/backup/restic-backup.py) against throwaway restic repositories, the hub's summary of
a host's backups, and the dashboard's backupHealth (core.js, run with node).

E-09: a stopped or crashed run writes "failed", never a stale "running"; the UI treats an old "running" as dead.
E-10: --seed keeps a status written by a real run unless --force.
E-11: restic exit 3 (unreadable files, missing source paths) and a failed SQLite copy make the run "partial".
E-32: the installer shows a new password right away; seed() works for repositories that are not local directories."""
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "deploy" / "backup" / "restic-backup.py"
INSTALLER = ROOT / "deploy" / "backup" / "install-restic-backup.sh"
RESTIC = shutil.which("restic")
needs_restic = pytest.mark.skipif(not RESTIC, reason="restic is not installed")


@pytest.fixture
def bk(tmp_path):
    """A repository with its password, a source tree and a config pointing everything into tmp_path."""
    (tmp_path / "pass").write_text("test-password")
    src = tmp_path / "src"
    (src / "d").mkdir(parents=True)
    (src / "a.txt").write_text("hello")
    (src / "d" / "b.txt").write_text("world")
    cfg = {"name": "test backup", "repository": str(tmp_path / "repo"), "password_file": str(tmp_path / "pass"),
           "paths": [str(src)], "status_file": str(tmp_path / "state" / "status.json"),
           "stamp_file": str(tmp_path / "state" / "last-success"), "cache_dir": str(tmp_path / "cache"),
           "check_weekday": 0, "timer": None}
    if RESTIC:
        subprocess.run([RESTIC, "init", "--repo", cfg["repository"], "--password-file", cfg["password_file"], "-q"],
                       check=True, capture_output=True, env=dict(os.environ, RESTIC_CACHE_DIR=cfg["cache_dir"]))

    class Bk:
        path, config = tmp_path, cfg

        def write(self, **over):
            (tmp_path / "config.json").write_text(json.dumps({**cfg, **over}))

        def env(self, path=None):
            return dict(os.environ, AVM_BACKUP_CONFIG=str(tmp_path / "config.json"), PATH=path or os.environ["PATH"])

        def run(self, *args, path=None, **over):
            self.write(**over)
            return subprocess.run([sys.executable, "-I", str(RUNNER), *args], env=self.env(path), capture_output=True,
                                  text=True, timeout=120)

        def status(self):
            with open(cfg["status_file"]) as f:
                return json.load(f)

        def set_status(self, data):
            os.makedirs(os.path.dirname(cfg["status_file"]), exist_ok=True)
            with open(cfg["status_file"], "w") as f:
                json.dump(data, f)

        def fake_restic(self, body):
            """A `restic` first on PATH: `body` is sh run before (or instead of) the real one."""
            d = tmp_path / "fakebin"
            d.mkdir(exist_ok=True)
            (d / "restic").write_text(f"#!/bin/sh\n{body}\nexec {RESTIC} \"$@\"\n")
            (d / "restic").chmod(0o755)
            return f"{d}:{os.environ['PATH']}"
    return Bk()


@needs_restic
def test_clean_run_is_ok(bk):
    db = bk.path / "live.db"
    with sqlite3.connect(db) as c:
        c.execute("create table t (x)")
    copy = bk.path / "copies" / "live.db"
    r = bk.run(sqlite=[[str(db), str(copy)]])
    assert r.returncode == 0, r.stdout + r.stderr
    st = bk.status()
    assert st["state"] == "ok" and st["error"] is None and st["warnings"] == 0, st
    assert st["max_run_sec"] == 43200 and st["last_success"] >= st["started"]
    assert copy.exists() and not os.path.exists(str(copy) + ".tmp")
    assert os.path.exists(bk.config["stamp_file"]) and "backup OK" in r.stdout


# ------------------------------------------------------------------------------------------------- E-09


@needs_restic
def test_sigterm_mid_run_writes_failed(bk):
    """systemd stop / TimeoutStartSec: the runner is killed while restic runs - the status must not stay "running"."""
    mark = bk.path / "backup-started"
    path = bk.fake_restic(f'if [ "$1" = backup ]; then echo $$ > "{mark}"; exec sleep 60; fi')
    bk.write()
    p = subprocess.Popen([sys.executable, "-I", str(RUNNER)], env=bk.env(path), stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True)
    try:
        for _ in range(200):
            if mark.exists() and mark.read_text().strip():
                break
            time.sleep(0.05)
        assert bk.status()["state"] == "running"
        p.send_signal(signal.SIGTERM)
        out, err = p.communicate(timeout=60)
    finally:
        p.kill()
    assert p.returncode == 128 + 15, out + err
    st = bk.status()
    assert st["state"] == "failed" and "stopped by signal 15" in st["error"], st
    assert st["finished"] >= st["started"]
    with pytest.raises(ProcessLookupError):   # the restic child was stopped too (it would hold the repository lock)
        os.kill(int(mark.read_text()), 0)


def test_crash_writes_failed(bk):
    """An uncaught exception (here: restic not installed) ends the run as "failed" with the reason."""
    empty = bk.path / "nobin"
    empty.mkdir()
    r = bk.run(path=str(empty))
    assert r.returncode == 1
    st = bk.status()
    assert st["state"] == "failed" and st["error"].startswith("interrupted: FileNotFoundError"), st
    assert "Traceback" in r.stderr   # the task log still shows where it broke


# ------------------------------------------------------------------------------------------------- E-10


@needs_restic
def test_seed_keeps_a_real_status_unless_forced(bk):
    assert bk.run().returncode == 0   # a tagged snapshot to seed from
    failed = {**bk.status(), "state": "failed", "error": "restic forget/prune exited 1", "finished": time.time()}
    bk.set_status(failed)
    r = bk.run("--seed")
    assert r.returncode == 0 and "kept the existing status" in r.stdout and "failed" in r.stdout, r.stdout
    assert bk.status() == failed

    r = bk.run("--seed", "--force")
    assert r.returncode == 0, r.stdout + r.stderr
    st = bk.status()
    assert st["state"] == "ok" and st["seeded"] is True and st["snapshots"] == 1

    r = bk.run("--seed")   # a seeded status is only a placeholder: refreshing it is fine
    assert r.returncode == 0 and "status seeded" in r.stdout


# ------------------------------------------------------------------------------------------------- E-11


@needs_restic
def test_restic_exit_3_is_partial(bk):
    gone = bk.path / "not-there"
    secret = bk.path / "src" / "d" / "secret"
    secret.write_text("x")
    secret.chmod(0)
    unreadable = os.geteuid() != 0   # root reads it anyway
    try:
        r = bk.run(paths=bk.config["paths"] + [str(gone)])
    finally:
        secret.chmod(0o600)
    assert r.returncode == 0, r.stdout + r.stderr
    st = bk.status()
    assert st["state"] == "partial", st
    assert f"1 source path missing: {gone}" in st["error"]
    if unreadable:
        assert "1 file unreadable" in st["error"] and str(secret) in st["error"] and "permission denied" in st["error"]
    assert st["warnings"] == (2 if unreadable else 1)
    assert st["snapshot"] and st["last_success"] >= st["started"]   # the snapshot was saved: last_success advances
    assert "backup PARTIAL" in r.stdout


@needs_restic
def test_sqlite_copy_failure_is_partial(bk):
    copy = bk.path / "copies" / "monitor.db"
    copy.parent.mkdir()
    copy.write_text("yesterday")
    r = bk.run(sqlite=[[str(bk.path / "missing.db"), str(copy)]])
    assert r.returncode == 0, r.stdout + r.stderr
    st = bk.status()
    assert st["state"] == "partial" and st["warnings"] == 1, st
    assert "SQLite copy failed" in st["error"] and "missing.db" in st["error"]
    assert copy.read_text() == "yesterday"   # the previous copy is kept, not half-overwritten


def test_problem_text_is_short():
    import runpy
    text = runpy.run_path(str(RUNNER))["problems_text"]([(f"/data/f{i}", "permission denied") for i in range(50)], ["/gone"], [])
    assert text.startswith("1 source path missing: /gone; 50 files unreadable: /data/f0 (permission denied)")
    assert "and 47 more" in text and len(text) <= 400


# ------------------------------------------------------------------------------------------------- E-32


def test_seed_handles_a_repository_that_is_not_a_directory(bk):
    """sftp:/s3:/rest: repositories: no local size or free space, instead of a traceback."""
    path = bk.fake_restic(
        """if [ "$1" = snapshots ]; then echo '[{"time": "2026-10-25T03:30:00.123456789+00:00", "short_id": "abcd1234"}]'; exit 0; fi""")
    r = bk.run("--seed", path=path, repository="sftp:backup@nas.invalid:/srv/restic")
    assert r.returncode == 0, r.stdout + r.stderr
    st = bk.status()
    assert st["state"] == "ok" and st["snapshot"] == "abcd1234" and st["repo_bytes"] is None and st["target_free_bytes"] is None


def test_installer_prints_a_new_password_before_anything_else_can_fail():
    sh = INSTALLER.read_text()
    shown = sh.index('echo "      $(cat "$PASS")"')
    assert sh.index("openssl rand") < shown < sh.index("restic init"), "the new password must be shown right after it is made"
    assert sh[shown:].split("\n", 1)[0].rstrip().endswith("echo") and ">&3" in sh[shown:shown + 200]   # terminal only
    assert "TimeoutStartSec=$MAXRUN" in sh


# ------------------------------------------------------------------------------------------------- hub + UI


def test_summary_forwards_backup_fields(agent, client):
    now = time.time()
    b = {"name": "nightly", "state": "partial", "started": now - 600, "finished": now - 60, "last_success": now - 60,
         "max_age_sec": 93600, "max_run_sec": 3600, "next_run": now + 80000, "error": "1 source path missing: /x",
         "snapshot": "abcd1234", "warnings": 1, "paths": ["/etc"]}
    body = {"ts": now, "host": {"hostname": agent.host_row.name, "agent_version": "1.8.1"}, "backups": [b]}
    assert agent.post("/api/v1/ingest", json=body).status_code == 200
    hosts = client.get("/api/v1/hosts").json()["hosts"]
    got = next(x for x in hosts if x["name"] == agent.host_row.name)["summary"]["backups"]
    assert got == [{k: v for k, v in b.items() if k != "paths"}]


JS = """
import { backupHealth } from %s;
const now = 1_800_000_000, H = 3600;
const cases = {
  stuck: { state: "running", started: now - 13 * H, last_success: now - 20 * H },
  stuck_first: { state: "running", started: now - 13 * H },
  stuck_own_limit: { state: "running", started: now - 2 * H, max_run_sec: H, last_success: now - 3 * H },
  running: { state: "running", started: now - H, last_success: now - 20 * H },
  first_running: { state: "running", started: now - H },
  partial: { state: "partial", last_success: now - H, warnings: 2 },
  partial_old: { state: "partial", last_success: now - 30 * H },
  failed: { state: "failed", last_success: now - 5 * H },
  ok: { state: "ok", last_success: now - H },
  old_running: { state: "running", started: now - H, last_success: now - 30 * H },
};
console.log(JSON.stringify(Object.fromEntries(Object.entries(cases).map(([k, b]) => [k, backupHealth(b, now)]))));
"""


@pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")
def test_backup_health_js():
    core = (ROOT / "server" / "avalon_monitor" / "static" / "core.js").as_uri()
    out = subprocess.run(["node", "--input-type=module", "-e", JS % json.dumps(core)], capture_output=True, text=True,
                         timeout=30, check=True).stdout
    r = json.loads(out)
    lv = {k: (v["level"], v["alert"]) for k, v in r.items()}
    assert lv["stuck"] == ("crit", True) and r["stuck"]["stale"] and "running" in r["stuck"]["label"]
    assert lv["stuck_first"] == ("crit", True) and lv["stuck_own_limit"] == ("crit", True)
    assert lv["running"] == ("ok", False) and lv["first_running"] == ("warn", False)
    assert lv["partial"] == ("warn", True) and r["partial"]["label"] == "backup partial (2 warnings)"
    assert lv["partial_old"] == ("crit", True)   # stale beats partial
    assert lv["failed"] == ("warn", True) and lv["ok"] == ("ok", False)
    assert lv["old_running"] == ("crit", False)   # a fresh run may still fix it: card red, no alert yet (as before)
