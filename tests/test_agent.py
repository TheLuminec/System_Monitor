"""Agent-side audit fixes: E-04 (local caps, signed self-update), E-05 (content roots), E-06 (transcript level),
E-15 (remote config survives restarts), E-17 (no prompts as root), E-18 (prompt after --), plus the 1.8.1 scope."""
import base64
import io
import json
import os
import subprocess

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import avalon_agent as a


def _cfg(**over):
    return dict(a.DEFAULTS, AVM_SERVER_URL="http://hub.test", AVM_TOKEN="t", **over)


def _pub(key):
    return key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


# ---------------------------------------------------------------- Ed25519
def test_pure_python_ed25519_matches_cryptography():
    for i in range(25):
        key = Ed25519PrivateKey.generate()
        msg = os.urandom(i * 7)
        sig = key.sign(msg)
        assert a.ed25519_verify(_pub(key), msg, sig)
        assert not a.ed25519_verify(_pub(key), msg + b"x", sig)
        bad = bytearray(sig)
        bad[i % 64] ^= 1
        assert not a.ed25519_verify(_pub(key), msg, bytes(bad))
        assert not a.ed25519_verify(_pub(Ed25519PrivateKey.generate()), msg, sig)
    assert not a.ed25519_verify(b"\x00" * 31, b"m", b"\x00" * 64)
    assert not a.ed25519_verify(_pub(key), b"m", b"short")


def test_rfc8032_test_vector_1():
    pub = bytes.fromhex("d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a")
    sig = bytes.fromhex("e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b")
    assert a.ed25519_verify(pub, b"", sig)


@pytest.fixture
def release_key():
    return Ed25519PrivateKey.generate()


def _sign(key, name, data):
    return base64.b64encode(key.sign(a.release_message(name, data))).decode()


def test_signature_policy(release_key):
    cfg = _cfg(AVM_UPDATE_PUBKEYS=_pub(release_key).hex())
    data = b"print('new agent')\n"
    assert a.signature_ok(cfg, "avalon_agent.py", data, _sign(release_key, "avalon_agent.py", data)) is None
    assert "no signature" in a.signature_ok(cfg, "avalon_agent.py", data, None)
    assert "bad signature" in a.signature_ok(cfg, "avalon_agent.py", data + b"#", _sign(release_key, "avalon_agent.py", data))
    # a signature for avm.py can't be replayed onto the agent script, and CRLF copies verify the same
    assert "bad signature" in a.signature_ok(cfg, "avalon_agent.py", data, _sign(release_key, "avm.py", data))
    assert a.signature_ok(cfg, "avalon_agent.py", data.replace(b"\n", b"\r\n"), _sign(release_key, "avalon_agent.py", data)) is None
    # no key anywhere: refuse, never fall back to "unsigned is fine"
    assert "no release key" in a.signature_ok(_cfg(), "avalon_agent.py", data, None) or a.RELEASE_PUBKEYS


class _Resp(io.BytesIO):
    def __init__(self, data, headers=None):
        super().__init__(data)
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _self_update(monkeypatch, tmp_path, key, data, sig):
    """Run self_update against a fake hub serving `data`; returns (installed?, file content, error)."""
    target = tmp_path / "avalon_agent.py"
    target.write_bytes(b"OLD = 1\n")
    monkeypatch.setattr(a, "_SELF_PATH", str(target))
    monkeypatch.setitem(a._SELF_SHA, "value", None)
    monkeypatch.setitem(a._UPDATE_BACKOFF, "until", 0.0)
    monkeypatch.setattr(a, "_refresh_companion", lambda *x: None)
    monkeypatch.setattr(a.urllib.request, "urlopen", lambda req, timeout=0: _Resp(data))
    import hashlib
    info = {"sha256": hashlib.sha256(data).hexdigest(), "version": "9.9", "sig": sig}
    cfg = _cfg(AVM_UPDATE_PUBKEYS=_pub(key).hex(), AVM_AUTO_UPDATE="true")
    return a.self_update(cfg, info), target.read_bytes(), a._UPDATE_STATE["error"]


def test_self_update_installs_signed_code(monkeypatch, tmp_path, release_key):
    data = b"NEW = 2\n"
    ok, content, err = _self_update(monkeypatch, tmp_path, release_key, data, _sign(release_key, "avalon_agent.py", data))
    assert ok and content == data and err is None


@pytest.mark.parametrize("why", ["unsigned", "wrong key", "tampered"])
def test_self_update_refuses_unsigned_or_forged_code(monkeypatch, tmp_path, release_key, why):
    """E-04: the hub (or whoever controls it) cannot push code - the hash it sends proves nothing on its own."""
    data = b"EVIL = 1\n"
    sig = {"unsigned": None, "wrong key": _sign(Ed25519PrivateKey.generate(), "avalon_agent.py", data),
           "tampered": _sign(release_key, "avalon_agent.py", b"GOOD = 1\n")}[why]
    ok, content, err = _self_update(monkeypatch, tmp_path, release_key, data, sig)
    assert not ok and content == b"OLD = 1\n" and ("signature" in err)


def test_companion_refresh_refuses_unsigned(monkeypatch, tmp_path, release_key):
    monkeypatch.setattr(a, "_SELF_PATH", str(tmp_path / "avalon_agent.py"))
    data = b"AVM = 1\n"
    monkeypatch.setattr(a.urllib.request, "urlopen", lambda req, timeout=0: _Resp(data))
    cfg = _cfg(AVM_UPDATE_PUBKEYS=_pub(release_key).hex())
    a._refresh_companion(cfg, "avm.py", None)
    assert not (tmp_path / "avm.py").exists()
    a._refresh_companion(cfg, "avm.py", _sign(release_key, "avm.py", data))
    assert (tmp_path / "avm.py").read_bytes() == data


# ------------------------------------------------------------ job policy
class _Runner(a.ClaudeRunner):
    def __init__(self, **cfg):
        super().__init__(_cfg(AVM_CLAUDE_BIN="/bin/true", **cfg))
        self.posts = []

    def _post(self, path, body):
        self.posts.append((path, body))
        return {}

    def _done(self):
        self.current = None


def _finish(r):
    return [b for p, b in r.posts if p.endswith("/finish")][-1]


@pytest.mark.parametrize("cap,mode,allowed", [
    ("plan", "plan", True), ("plan", "acceptEdits", False), ("plan", "full", False), ("acceptEdits", "acceptEdits", True),
    ("acceptEdits", "full", False), ("full", "full", True), ("bogus", "acceptEdits", False), ("plan", "weird", False),
])
def test_max_job_mode_cap(cap, mode, allowed):
    """E-04: the host, not the hub, decides how much a dashboard prompt may do."""
    r = _Runner(AVM_MAX_JOB_MODE=cap)
    why = r._refusal({"id": 1, "prompt": "x", "mode": mode})
    assert (why is None) is allowed, why


def test_default_cap_is_plan():
    assert _Runner().policy()["max_mode"] == "plan"


def test_refused_job_never_starts_claude(monkeypatch):
    r = _Runner()
    started = []
    monkeypatch.setattr(a.subprocess, "Popen", lambda *x, **k: started.append(x))
    r._run_job({"id": 7, "prompt": "x", "mode": "full"})
    assert not started and _finish(r)["status"] == "failed" and "AVM_MAX_JOB_MODE" in _finish(r)["error"]


def test_actions_switch():
    assert _Runner()._refusal({"id": 1, "action": "restart"}) is None
    assert "AVM_ALLOW_ACTIONS" in _Runner(AVM_ALLOW_ACTIONS="false")._refusal({"id": 1, "action": "restart"})


def test_root_agent_refuses_prompts_and_actions(monkeypatch):
    """E-17: from a root/SYSTEM agent a prompt would run unconfined with full privileges."""
    monkeypatch.setattr(a, "_running_as_root", lambda: True)
    r = _Runner(AVM_MAX_JOB_MODE="full")
    assert "AVM_ALLOW_ROOT_JOBS" in r._refusal({"id": 1, "prompt": "x", "mode": "plan"})
    assert "AVM_ALLOW_ROOT_JOBS" in r._refusal({"id": 1, "action": "stop"})
    assert _Runner(AVM_ALLOW_ROOT_JOBS="true")._refusal({"id": 1, "prompt": "x", "mode": "plan"}) is None


def test_policy_reported_to_hub():
    rep = _Runner(AVM_MAX_JOB_MODE="acceptEdits", AVM_JOB_TRANSCRIPT="summary").report()
    assert rep["policy"] == {"max_mode": "acceptEdits", "actions": True, "jobs": True, "transcript": "summary"}


def test_prompt_is_passed_after_double_dash(monkeypatch, tmp_path):
    """E-18: a prompt that looks like a flag ("--dangerously-skip-permissions") must stay a prompt."""
    seen = {}

    class P:
        def __init__(self, args, **kw):
            seen["args"] = args
            self.stdout, self.stderr = io.StringIO(""), io.StringIO("")

        def wait(self):
            return 0

    monkeypatch.setattr(a.subprocess, "Popen", P)
    r = _Runner()
    r._run_job({"id": 3, "prompt": "--dangerously-skip-permissions hi", "mode": "plan", "cwd": str(tmp_path)})
    args = seen["args"]
    assert args[-2:] == ["--", "--dangerously-skip-permissions hi"]
    assert "--dangerously-skip-permissions" not in args[:-1] and args[args.index("--permission-mode") + 1] == "plan"


# ------------------------------------------------------------ transcripts
EV_ASSIST = {"type": "assistant", "message": {"model": "m", "content": [
    {"type": "text", "text": "the file says SECRET"}, {"type": "thinking", "thinking": "SECRET"},
    {"type": "tool_use", "name": "Read", "input": {"file_path": "/data/SECRET.csv"}}]}}
EV_RESULT = {"type": "user", "message": {"content": [{"type": "tool_result", "content": "SECRET,1,2", "is_error": False}]}}
EV_FINAL = {"type": "result", "result": "FINAL-ANSWER", "is_error": False, "num_turns": 2}


def test_transcript_full_unchanged():
    assert "SECRET" in json.dumps(a._compact_event(EV_ASSIST)) and "SECRET" in json.dumps(a._compact_event(EV_RESULT))


def test_transcript_summary_drops_file_contents():
    """E-06: on data-use-agreement hosts nothing a tool read or wrote leaves the machine."""
    s = a._compact_event(EV_ASSIST, "summary")
    assert s["parts"] == [{"type": "tool_use", "name": "Read", "input": ""}]
    assert a._compact_event(EV_RESULT, "summary") is None
    assert a._compact_event({"type": "raw", "text": "SECRET"}, "summary") == {"type": "raw"}
    assert a._compact_event(EV_FINAL, "summary")["result"] == "FINAL-ANSWER"


def test_transcript_off_keeps_only_metadata():
    assert a._compact_event(EV_ASSIST, "off") is None and a._compact_event(EV_RESULT, "off") is None
    assert "result" not in a._compact_event(EV_FINAL, "off")


def test_transcript_off_withholds_answer(monkeypatch, tmp_path):
    lines = [json.dumps(EV_ASSIST), json.dumps(EV_FINAL)]

    class P:
        def __init__(self, args, **kw):
            self.stdout, self.stderr = io.StringIO("\n".join(lines) + "\n"), io.StringIO("")

        def wait(self):
            return 0

    monkeypatch.setattr(a.subprocess, "Popen", P)
    r = _Runner(AVM_JOB_TRANSCRIPT="off")
    r._run_job({"id": 4, "prompt": "p", "mode": "plan", "cwd": str(tmp_path)})
    sent = json.dumps(r.posts)
    assert "SECRET" not in sent and "FINAL-ANSWER" not in sent and _finish(r)["status"] == "done"


# ---------------------------------------------------------- content roots
def test_content_checks_limited_to_local_list_and_roots(tmp_path):
    """E-05: the hub can add content checks, but only for files this host agreed to expose."""
    root = tmp_path / "status"
    root.mkdir()
    (root / "job.txt").write_text("epoch 3\n")
    secret = tmp_path / "secret.txt"
    secret.write_text("TOKEN\n")
    os.symlink(secret, root / "escape.txt")
    local = str(tmp_path / "mine.txt")
    (tmp_path / "mine.txt").write_text("local ok\n")
    remote = a.RemoteConfig(_cfg(AVM_WATCH_CONTENT=local, AVM_CONTENT_ROOTS=str(root)))
    remote.apply({"config": {"rev": 1, "settings": {"AVM_WATCH_CONTENT": ",".join([local, str(root / "job.txt"), str(secret), str(root / "escape.txt")])}}})
    got = {c["src"]: c for c in a.collect_checks(remote.effective()) if c["kind"] == "content"}
    assert got[local]["detail"] == "local ok" and got[str(root / "job.txt")]["detail"] == "epoch 3"
    for refused in (str(secret), str(root / "escape.txt")):
        assert not got[refused]["ok"] and got[refused]["detail"].startswith("refused") and "TOKEN" not in json.dumps(got)


# ---------------------------------------------------------- remote config
def test_remote_config_survives_restart(tmp_path):
    """E-15: the first sample after a restart must already carry hub-added checks."""
    path = str(tmp_path / "remote-config.json")
    rc = a.RemoteConfig(_cfg(), path)
    assert rc.apply({"config": {"rev": 5, "settings": {"AVM_WATCH_MOUNTS": "/mnt/x", "AVM_TOKEN": "nope"}}})
    again = a.RemoteConfig(_cfg(), path)
    assert again.rev == 5 and again.effective()["AVM_WATCH_MOUNTS"] == "/mnt/x"
    assert "AVM_TOKEN" not in again.overrides                     # only REMOTE_KEYS, even from the file
    assert not again.apply({"config": {"rev": 5, "settings": {}}})  # same rev: nothing to do
    again.apply({})                                                # hub cleared them
    assert a.RemoteConfig(_cfg(), path).rev is None


def test_policy_keys_cannot_be_set_remotely():
    for k in ("AVM_MAX_JOB_MODE", "AVM_ALLOW_ACTIONS", "AVM_ALLOW_ROOT_JOBS", "AVM_JOB_TRANSCRIPT", "AVM_CONTENT_ROOTS",
              "AVM_UPDATE_PUBKEYS", "AVM_AUTO_UPDATE"):
        assert k not in a.REMOTE_KEYS
    rc = a.RemoteConfig(_cfg())
    rc.apply({"config": {"rev": 1, "settings": {"AVM_MAX_JOB_MODE": "full", "AVM_UPDATE_PUBKEYS": "00" * 32}}})
    assert rc.effective()["AVM_MAX_JOB_MODE"] == "plan" and rc.effective()["AVM_UPDATE_PUBKEYS"] == ""


# --------------------------------------------------- scope (1.8.1 regression)
def test_claude_runs_in_its_own_scope(monkeypatch):
    """Restarting the agent service once killed every Claude session: they lived in its cgroup."""
    if not (a.IS_LINUX and os.path.exists("/run/systemd/system")):
        pytest.skip("needs systemd")
    monkeypatch.setenv("INVOCATION_ID", "test")
    r = _Runner()
    argv = r._own_scope(["sh", "-c", "cat /proc/self/cgroup"])
    assert "--scope" in argv
    cg = subprocess.run(argv, capture_output=True, text=True).stdout.strip()
    assert cg.endswith(".scope")
    monkeypatch.delenv("INVOCATION_ID")
    assert _Runner()._own_scope(["x"]) == ["x"]


# ----------------------------------------------------- E-25 (agent side)
def test_marker_removed_mid_scan_drops_nothing(tmp_path, monkeypatch):
    """A marker deleted between glob and stat used to raise out of collect_checks: _safe() then reported NO checks,
    and the hub's first adoption marked the host managed with zero rows."""
    for n in ("a.failed", "b.failed"):
        (tmp_path / n).write_text("x")
    real_stat = os.stat

    def flaky(p, *k, **kw):
        if str(p).endswith("a.failed"):
            raise FileNotFoundError(p)
        return real_stat(p, *k, **kw)

    monkeypatch.setattr(a.os, "stat", flaky)
    checks = a.collect_checks(_cfg(AVM_WATCH_MARKERS=str(tmp_path / "*.failed"), AVM_WATCH_MOUNTS="/"))
    marker = [c for c in checks if c["kind"] == "marker"][0]
    assert not marker["ok"] and marker["value"] == 1.0 and "b.failed" in marker["detail"]
    assert any(c["kind"] == "mount" for c in checks)
