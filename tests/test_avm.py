"""avm (agent/avm.py) against the real hub: a uvicorn server on a free loopback port, serving the conftest app and DB.
Most runs are in-process (avm.main) with Hub.call spied on, so the exact bodies sent are visible; the ones that
need a real process (signals to avm itself, a closed stdout pipe) run avm as a subprocess. No test touches the real
/tmp/avm-queue, ~/.config or any agent.conf: avm's config lookup is pinned to a temp file and AVM_QUEUE_DIR to a temp dir."""
import io
import os
import signal
import socket
import stat
import subprocess
import sys
import textwrap
import threading
import time
import urllib.error

import pytest

from conftest import ROOT
from avalon_monitor import core
from avalon_monitor.db import Database

import avm

AGENT = str(ROOT / "agent")
PY = sys.executable
pytestmark = pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX shell commands and signals")


# ------------------------------------------------------------------ fixtures
@pytest.fixture(scope="module")
def live_hub(app_obj):
    import uvicorn
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    server = uvicorn.Server(uvicorn.Config(app_obj, host="127.0.0.1", port=port, lifespan="off", log_level="error",
                                           access_log=False))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)
    assert server.started, "uvicorn did not start"
    yield "http://localhost:%d" % port
    server.should_exit = True
    th.join(5)


def _closed_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Env:
    """One test's avm world: a config file avm is pinned to, a queue dir, the host, and a spy on what avm sends."""

    def __init__(self, tmp_path, monkeypatch, host, url):
        self.tmp, self.mp = tmp_path, monkeypatch
        self.row, self.token = host
        self.url = url
        self.conf = str(tmp_path / "agent.conf")
        self.qdir = str(tmp_path / "queue")
        self.calls = []
        for k in ("AVM_SERVER_URL", "AVM_TOKEN", "AVM_TASK_LOG", "AVM_QUEUE_DIR", "AVM_QUEUE_HOLD", "AVM_CONFIG",
                  "AVM_WATCH_CHILD", "AVM_TASK_ID"):
            monkeypatch.delenv(k, raising=False)
        monkeypatch.setenv("AVM_QUEUE_DIR", self.qdir)
        monkeypatch.setattr(avm, "_candidates", lambda: [self.conf])
        monkeypatch.setattr(avm, "MIN_BEAT", 0.2)
        self.write()
        real = avm.Hub.call
        calls = self.calls

        def spy(hub, path, body=None, method="POST", timeout=10.0, raw=None):
            if path == "/tasks" and body and (body.get("expect_beat") or 5) < 5:
                body = dict(body, expect_beat=5)   # the hub's floor; tests beat every 0.2 s instead of 5
            entry = {"path": path, "body": body, "method": method}
            calls.append(entry)
            r = real(hub, path, body, method, timeout, raw)
            entry["result"] = r
            return r
        monkeypatch.setattr(avm.Hub, "call", spy)

    def write(self, url=None, token=None, **extra):
        lines = ["AVM_SERVER_URL=%s" % (url or self.url), "AVM_TOKEN=%s" % (token or self.token)]
        lines += ["%s=%s" % kv for kv in extra.items()]
        with open(self.conf, "w") as f:
            f.write("\n".join(lines) + "\n")

    def run(self, *argv, stdout=None):
        self.mp.setattr(sys, "stdout", stdout or FakeOut())
        return avm.main(list(argv))

    def tid(self):
        ids = [c["result"]["id"] for c in self.calls if c["path"] == "/tasks" and c.get("result")]
        assert ids, "no task was started"
        return ids[-1]

    def task(self, tid=None):
        return core.db.task_get(tid or self.tid())

    def sent_lines(self):
        return [ln for c in self.calls if isinstance(c["body"], dict) for ln in (c["body"].get("log") or [])]

    def env(self):
        e = {k: v for k, v in os.environ.items() if not k.startswith("AVM_")}
        e["AVM_QUEUE_DIR"] = self.qdir
        return e

    def popen(self, *argv, **kw):
        boot = "import sys; sys.path.insert(0, %r); import avm; avm._candidates = lambda: [%r]; sys.exit(avm.main(sys.argv[1:]))" % (
            AGENT, self.conf)
        return subprocess.Popen([PY, "-c", boot, *argv], env=self.env(), **kw)

    def task_named(self, name, timeout=10.0):
        end = time.time() + timeout
        while time.time() < end:
            rows = [t for t in core.db.task_list(self.row.id, None, 50, with_log=True) if t["name"] == name]
            if rows:
                return rows[0]
            time.sleep(0.05)
        return None


class FakeOut:
    """sys.stdout stand-in: records the exact bytes avm writes; optionally breaks like a closed pipe."""

    def __init__(self, on_write=None, broken=False):
        self.buffer = self
        self.data = bytearray()
        self.on_write, self.broken = on_write, broken

    def write(self, b):
        if self.broken:
            raise BrokenPipeError(32, "Broken pipe")
        if isinstance(b, str):
            b = b.encode()
        self.data += b
        if self.on_write:
            self.on_write(bytes(self.data))
        return len(b)

    def flush(self):
        pass


@pytest.fixture
def env(tmp_path, monkeypatch, host, live_hub):
    return Env(tmp_path, monkeypatch, host, live_hub)


def sh(script):
    return ["sh", "-c", script]


# ------------------------------------------------------------------ E-08: the hub never stops the command
def test_hub_down_command_still_runs_and_rc_passes(env, capsys):
    env.write(url="http://127.0.0.1:%d" % _closed_port())
    marker = env.tmp / "ran"
    rc = env.run("run", "nightly backup", "--", *sh("touch %s; exit 7" % marker))
    assert rc == 7 and marker.exists()
    err = capsys.readouterr().err
    assert "untracked: cannot reach hub" in err and "running the command anyway" in err


def test_rotated_token_command_still_runs(env, capsys):
    env.write(token="rotated-away")
    rc = env.run("run", "job", "--", *sh("exit 3"))
    assert rc == 3
    assert "untracked: hub rejected /tasks: HTTP 401" in capsys.readouterr().err


def test_no_config_command_still_runs(env, monkeypatch, capsys):
    monkeypatch.setattr(avm, "_candidates", lambda: [])
    rc = env.run("run", "job", "--", "true")
    assert rc == 0
    assert "untracked: no agent config" in capsys.readouterr().err


def test_hub_failing_after_start_never_crashes_beats_or_finish(env, monkeypatch):
    real = avm.Hub.call   # the spy: the start goes through, then every beat/finish fails in a different way
    errors = iter([urllib.error.URLError("refused"), ValueError("not json"),
                   urllib.error.HTTPError("u", 500, "boom", {}, io.BytesIO(b"oops"))] * 50)

    def flaky(hub, path, body=None, method="POST", timeout=10.0, raw=None):
        if path != "/tasks":
            raise next(errors)
        return real(hub, path, body, method, timeout, raw)
    monkeypatch.setattr(avm.Hub, "call", flaky)
    rc = env.run("run", "job", "--beat", "0.2", "--", *sh("echo a; sleep 0.7; echo b; exit 4"))
    assert rc == 4


# ------------------------------------------------------------------ E-06: task log switches
CHATTY = sh("echo secret-1; sleep 0.6; echo secret-2; printf 'secret-3'")


def test_log_on_by_default_lines_reach_hub(env):
    assert env.run("run", "chatty", "--beat", "0.2", "--", *CHATTY) == 0
    log = env.task()["log"] or ""
    assert "secret-1" in log and "secret-3" in log
    start = env.calls[0]["body"]
    assert start["command"] and start["cwd"]


def _assert_nothing_leaked(env):
    beats = [c for c in env.calls if c["path"].endswith("/beat")]
    assert len(beats) >= 2, "heartbeats must still be sent"   # pid beat + at least one timed beat
    assert env.sent_lines() == []
    assert not any("log" in (c["body"] or {}) for c in env.calls)
    t = env.task()
    assert not t["log"] and t["status"] == "done" and t["rc"] == 0
    assert t["command"] is None and t["cwd"] is None


def test_log_off_in_config_sends_no_lines(env):
    env.write(AVM_TASK_LOG="off")
    assert env.run("run", "chatty", "--beat", "0.2", "--", *CHATTY) == 0
    _assert_nothing_leaked(env)


def test_no_log_flag_forces_off(env):
    assert env.run("run", "chatty", "--no-log", "--beat", "0.2", "--", *CHATTY) == 0
    _assert_nothing_leaked(env)


def test_log_flag_forces_on_over_config_off(env):
    env.write(AVM_TASK_LOG="off", AVM_TASK_LOG_OFF_DIRS=str(env.tmp))
    env.mp.chdir(env.tmp)
    assert env.run("run", "chatty", "--log", "--beat", "0.2", "--", *CHATTY) == 0
    assert "secret-2" in (env.task()["log"] or "")


def test_log_off_dirs(env):
    dua = env.tmp / "dua"
    (dua / "sub").mkdir(parents=True)
    (env.tmp / "other").mkdir()
    os.symlink(dua, env.tmp / "dua-link")
    env.write(AVM_TASK_LOG_OFF_DIRS="/nonexistent/x, %s" % dua)
    for where, leaks in (("dua/sub", False), ("dua-link", False), ("other", True)):
        env.calls.clear()
        env.mp.chdir(env.tmp / where)
        assert env.run("run", "in " + where, "--beat", "0.2", "--", *CHATTY) == 0
        assert ("secret-1" in (env.task()["log"] or "")) is leaks, where
        if not leaks:
            assert env.sent_lines() == []


def test_log_off_in_any_config_file_wins(env, tmp_path, monkeypatch):
    second = tmp_path / "etc.conf"
    second.write_text("AVM_TASK_LOG=off\nAVM_TASK_LOG_OFF_DIRS=/srv/dua\n")
    env.write(AVM_TASK_LOG="on", AVM_TASK_LOG_OFF_DIRS="/data/a")
    monkeypatch.setattr(avm, "_candidates", lambda: [env.conf, str(second)])
    cfg = avm.load_config()
    assert cfg["AVM_TASK_LOG"] == "off" and cfg["AVM_TASK_LOG_OFF_DIRS"] == "/data/a,/srv/dua"
    assert cfg["AVM_SERVER_URL"] == env.url   # the first file still provides the hub settings


def test_log_off_child_env_and_env_cannot_turn_it_on(env):
    env.write(AVM_TASK_LOG="off")
    env.mp.setenv("AVM_TASK_LOG", "on")   # the environment can only turn it off
    seen = env.tmp / "seen"
    assert env.run("run", "nested", "--beat", "0.2", "--", *sh("echo secret; echo $AVM_TASK_LOG > %s" % seen)) == 0
    assert env.sent_lines() == [] and seen.read_text().strip() == "off"
    env.write()   # config on, but a parent run with the log off passed AVM_TASK_LOG=off down
    env.mp.setenv("AVM_TASK_LOG", "off")
    env.calls.clear()
    assert env.run("run", "child", "--beat", "0.2", "--", *CHATTY) == 0
    assert env.sent_lines() == []


def test_log_off_cannot_start_sends_no_log(env):
    env.write(AVM_TASK_LOG="off")
    assert env.run("run", "missing", "--", str(env.tmp / "no-such-program")) == 127
    assert env.sent_lines() == [] and env.task()["status"] == "failed"
    assert "no-such-program" not in (env.task()["note"] or "")


def test_log_off_queued_path(env):
    env.write(AVM_TASK_LOG="off")
    assert env.run("run", "gpu job", "--gpu", "--beat", "0.2", "--", *CHATTY) == 0
    _assert_nothing_leaked(env)


def test_log_off_task_cli(env, capsys):
    env.write(AVM_TASK_LOG="off")
    assert env.run("task", "start", "manual") == 0
    tid = env.tid()
    assert env.run("task", "beat", str(tid), "--log", "secret beat", "--progress", "0.5") == 0
    assert env.run("task", "done", str(tid), "--log", "secret done", "--note", "fine") == 0
    assert env.sent_lines() == []
    t = env.task(tid)
    assert not t["log"] and t["status"] == "done" and t["note"] == "fine" and t["cwd"] is None
    assert "--log lines not sent" in capsys.readouterr().err


def test_log_off_watch_sends_no_cwd(env, monkeypatch):
    env.write(AVM_TASK_LOG="off")
    monkeypatch.setattr(avm, "WATCH_RC_GRACE", 0.1)
    p = subprocess.Popen(["sleep", "0.3"])
    try:
        assert env.run("watch", "w", "--pid", str(p.pid), "--foreground") == 0
    finally:
        p.wait()
    t = env.task()
    assert t["cwd"] is None and not t["log"]


# ------------------------------------------------------------------ E-22: the shared queue dir
def test_queue_dir_created_sticky_and_usable(env):
    q = avm.Queue({})
    st = os.lstat(env.qdir)
    assert q.dir == env.qdir and stat.S_IMODE(st.st_mode) == 0o1777 and st.st_uid == os.getuid()
    q.register({"name": "x", "state": "running"})
    try:
        assert [t["name"] for t in avm.Queue({}).tickets()] == ["x"]
        mode = stat.S_IMODE(os.stat(q._path(q.me["id"], ".json")).st_mode)
        assert mode == 0o644
    finally:
        q.release()
    assert os.listdir(env.qdir) == []


def test_queue_dir_symlink_refused(env, capsys):
    real = env.tmp / "elsewhere"
    real.mkdir()
    os.symlink(real, env.qdir)
    with pytest.raises(avm.QueueUnsafe, match="symlink"):
        avm.Queue({})
    marker = env.tmp / "ran"
    assert env.run("run", "job", "--gpu", "--", "touch", str(marker)) == 2
    assert not marker.exists()
    assert "refusing queue dir" in capsys.readouterr().err
    assert env.run("queue", "status") == 2


def test_queue_dir_foreign_owner_refused(env, monkeypatch):
    os.mkdir(env.qdir)
    assert "owned by uid %d" % os.getuid() in avm._dir_problem(env.qdir, os.getuid() + 4242, final=True)
    assert avm._dir_problem(env.qdir, os.getuid(), final=True) is None
    me = os.getuid()
    monkeypatch.setattr(os, "getuid", lambda: me + 4242)   # as seen by another user: not theirs, not root's
    with pytest.raises(avm.QueueUnsafe, match="owned by uid"):
        avm.Queue({})


def test_queue_dir_writable_by_others_without_sticky_refused(env):
    os.mkdir(env.qdir)
    os.chmod(env.qdir, 0o777)
    with pytest.raises(avm.QueueUnsafe, match="not sticky"):
        avm.Queue({})
    os.chmod(env.qdir, 0o700)
    parent = env.tmp / "open"
    parent.mkdir()
    os.chmod(parent, 0o777)   # a parent anyone can write to could swap the dir itself
    env.mp.setenv("AVM_QUEUE_DIR", str(parent / "q"))
    with pytest.raises(avm.QueueUnsafe, match="not sticky"):
        avm.Queue({})


def test_queue_files_are_never_followed(env):
    q = avm.Queue({})
    victim = env.tmp / "victim"
    victim.write_text("precious")
    os.symlink(victim, os.path.join(q.dir, "admit.lock"))
    os.symlink(victim, os.path.join(q.dir, "DRAIN"))
    q.register({"name": "me", "state": "waiting"})
    try:
        with pytest.raises(avm.QueueUnsafe):
            q.try_admit()
        assert q.draining() is None   # a symlinked DRAIN is not read through
        assert env.run("queue", "drain") == 0   # replaces the link, never writes through it
        assert victim.read_text() == "precious"
        assert not os.path.islink(os.path.join(q.dir, "DRAIN")) and q.draining().startswith("drained by")
        target = os.path.join(q.dir, "planted.json")
        os.symlink(victim, target)
        avm._write_atomic(q.dir, target, b"{}")
        assert victim.read_text() == "precious" and not os.path.islink(target)
    finally:
        q.release()


def test_planted_fifo_does_not_hang_readers(env):
    q = avm.Queue({})
    os.mkfifo(os.path.join(q.dir, "evil.lock"))
    os.mkfifo(os.path.join(q.dir, "evil.json"))
    out = []
    th = threading.Thread(target=lambda: out.append(q.tickets()), daemon=True)
    th.start()
    th.join(5)
    assert out == [[]], "reading the queue hung on a FIFO"


# ------------------------------------------------------------------ E-28: avm watch --rc-file
def _watch(env, monkeypatch, rc_content, *extra):
    monkeypatch.setattr(avm, "WATCH_RC_GRACE", 0.2)
    rcf = env.tmp / "rc"
    if rc_content is not None:
        rcf.write_text(rc_content)
    p = subprocess.Popen(["sleep", "0.3"])
    try:
        assert env.run("watch", "watched", "--pid", str(p.pid), "--foreground", "--rc-file", str(rcf), *extra) == 0
    finally:
        p.wait()
    return env.task()


def test_watch_rc_file_missing_means_failed(env, monkeypatch):
    t = _watch(env, monkeypatch, None)
    assert t["status"] == "failed" and t["rc"] is None and "exit code unknown" in t["note"]


def test_watch_rc_file_garbage_means_failed(env, monkeypatch):
    t = _watch(env, monkeypatch, "")
    assert t["status"] == "failed" and "exit code unknown" in t["note"]


def test_watch_rc_file_values(env, monkeypatch):
    assert _watch(env, monkeypatch, "0\n")["status"] == "done"
    t = _watch(env, monkeypatch, "3")
    assert t["status"] == "failed" and t["rc"] == 3


def test_watch_detach_passes_note_and_rc_file(env, monkeypatch):
    seen = []

    class FakePopen:
        def __init__(self, argv, **kw):
            seen.append(argv)
    monkeypatch.setattr(avm.subprocess, "Popen", FakePopen)
    monkeypatch.chdir(env.tmp)
    assert env.run("watch", "bg", "--pid", str(os.getpid()), "--note", "-nightly, see runbook", "--rc-file", "rc") == 0
    argv = seen[0]
    assert "--note=-nightly, see runbook" in argv and "--rc-file=%s" % (env.tmp / "rc") in argv


# ------------------------------------------------------------------ E-29: signals
def test_child_killed_by_signal_gives_128_plus_n(env):
    assert env.run("run", "killed", "--", *sh("kill -TERM $$")) == 143
    t = env.task()
    assert t["rc"] == 143 and t["status"] == "failed" and "SIGTERM" in t["note"]


def test_sighup_to_avm_finishes_task_as_failed(env):
    p = env.popen("run", "hup job", "--", "sleep", "30", stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        end = time.time() + 15
        while time.time() < end:
            t = env.task_named("hup job", timeout=0.1)
            if t and t["pid"]:
                break
        assert t and t["pid"], "task never started"
        p.send_signal(signal.SIGHUP)
        rc = p.wait(15)
    finally:
        if p.poll() is None:
            p.kill()
    assert rc == 129
    t = core.db.task_get(t["id"])
    assert t["status"] == "failed" and t["rc"] == 129 and "SIGHUP" in t["note"]


# ------------------------------------------------------------------ E-31: byte-for-byte output
PROGRESS = [PY, "-c", "import sys; sys.stdout.buffer.write(b'10%\\r50%\\r100%\\ndone\\r\\n\\xc3\\xa9t\\xc3\\xa9 \\xff\\nprompt> ')"]


def test_output_passes_through_byte_for_byte(env):
    out = FakeOut()
    assert env.run("run", "bar", "--", *PROGRESS, stdout=out) == 0
    assert bytes(out.data) == b"10%\r50%\r100%\ndone\r\n\xc3\xa9t\xc3\xa9 \xff\nprompt> "
    assert env.task()["log"].split("\n") == ["100%", "done", "été �", "prompt> "]


def test_prompt_without_newline_is_shown_at_once(env):
    flag = env.tmp / "answered"
    child = textwrap.dedent("""
        import os, sys, time
        sys.stdout.write("Continue? [y/N] "); sys.stdout.flush()
        end = time.time() + 5
        while time.time() < end and not os.path.exists(%r):
            time.sleep(0.05)
        sys.exit(0 if os.path.exists(%r) else 1)
    """ % (str(flag), str(flag)))

    def on_write(data):
        if data.endswith(b"Continue? [y/N] "):
            flag.write_text("y")
    assert env.run("run", "prompt", "--", PY, "-c", child, stdout=FakeOut(on_write)) == 0


def test_tail_splitter_chunk_boundaries():
    t = avm._Tail()
    lines = t.feed(b"a\r") + t.feed(b"\nb") + t.feed(b"\xc3") + t.feed(b"\xa9\r") + t.feed(b"c")
    assert lines == ["a"] and t.partial() == "c"
    assert t.feed(b"", final=True) == ["c"]
    t = avm._Tail()
    assert t.feed(b"x" * 5000 + b"\n") == ["x" * avm._Tail.MAX]


def test_broken_stdout_in_process_keeps_running(env):
    big = sh("seq 1 200000; exit 5")
    assert env.run("run", "piped", "--", *big, stdout=FakeOut(broken=True)) == 5
    t = env.task()
    assert t["status"] == "failed" and t["rc"] == 5 and "200000" in t["log"]


def test_broken_pipe_does_not_stall(env):
    p = env.popen("run", "head job", "--", *sh("seq 1 300000; exit 6"), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        assert p.stdout.readline() == b"1\n"
        p.stdout.close()   # like `| head -1`
        rc = p.wait(30)
        err = p.stderr.read().decode()
    finally:
        if p.poll() is None:
            p.kill()
    assert rc == 6, err
    assert "Traceback" not in err and "Exception ignored" not in err
    t = env.task_named("head job")
    assert t["status"] == "failed" and t["rc"] == 6 and "300000" in t["log"]


# ------------------------------------------------------------------ E-33: what --beat means
def test_beat_help_matches_the_hub_stall_rule(env, tmp_path, capsys):
    assert env.run("run", "beat10", "--beat", "10", "--", "true") == 0
    expect = env.calls[0]["body"]["expect_beat"]
    assert expect == 20
    db = Database(str(tmp_path / "stall.db"))   # the hub's own stall rule, on a private DB
    h, _ = db.create_host("stall-host")
    tsk = db.task_create(h.id, "beat10", "run", expect, None, None, None, None, None)
    hb = tsk["heartbeat"]
    assert db.task_mark_stalled(now=hb + 3 * 10 - 1) == []
    assert [r["id"] for r in db.task_mark_stalled(now=hb + 3 * 10 + 1)] == [tsk["id"]]
    out = FakeOut()
    with pytest.raises(SystemExit):
        env.run("run", "--help", stdout=out)
    assert "stalled after 3x" in " ".join(out.data.decode().split())


# ------------------------------------------------------------------ E-40: --gpu next to --exclusive
def test_shared_gpu_job_runs_beside_an_exclusive_one(env):
    heavy = avm.Queue({})
    heavy.register({"name": "heavy", "state": "running", "gpu": "exclusive", "started_at": time.time()})
    light, other = avm.Queue({}), avm.Queue({})
    try:
        light.register({"name": "light", "state": "waiting", "gpu": "shared"})
        assert light.check()[0] is True   # documented: light --gpu jobs may share the GPU with an --exclusive one
        other.register({"name": "heavy 2", "state": "waiting", "gpu": "exclusive"})
        ok, why, _ = other.check()
        assert not ok and why == "one --exclusive GPU job at a time: 'heavy' is running"
    finally:
        for q in (heavy, light, other):
            q.release()
