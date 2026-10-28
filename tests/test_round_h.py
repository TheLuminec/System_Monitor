"""Audit round H: F-12 (release signatures bound to the version, no downgrade), F-19 (unset sensor limits), G-02
(AVM_TASK_LOG_OFF_DIRS keeps session snippets and full transcripts on the host), G-03 (a resumed session runs in the
directory this host recorded, never one the hub names). The signer side of F-12 and G-05 is in test_sign_release.py."""
import base64
import collections
import hashlib
import json
import os

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import avalon_agent as a
from avalon_monitor import core
from avalon_monitor import main as hub_main
from test_agent import EV_ASSIST, EV_FINAL, _cfg, _finish, _pub, _Resp, _Runner
from test_round_f import _ActionRunner


# ------------------------------------------------------------------ F-12: version-bound signatures
@pytest.fixture
def key():
    return Ed25519PrivateKey.generate()


def _agent_src(version, body="X = 1"):
    return ('AGENT_VERSION = "%s"\n%s\n' % (version, body)).encode()


def _sig2(key, name, data, version=None):
    return base64.b64encode(key.sign(a.release_message(name, version or a.release_version(name, data), data))).decode()


def _sig1(key, name, data):
    """What agents <= 1.9.x verify (deploy/sign-release.py's release_message_v1)."""
    msg = ("avalon-release:v1:%s:%s" % (name, hashlib.sha256(data.replace(b"\r\n", b"\n")).hexdigest())).encode()
    return base64.b64encode(key.sign(msg)).decode()


def _update(monkeypatch, tmp_path, cfg, data, **offer):
    """self_update against a fake hub serving `data`: (installed?, file content, error, companion calls)."""
    target = tmp_path / "avalon_agent.py"
    target.write_bytes(b"OLD = 1\n")
    companion = []
    monkeypatch.setattr(a, "_SELF_PATH", str(target))
    monkeypatch.setitem(a._SELF_SHA, "value", None)
    monkeypatch.setitem(a._UPDATE_BACKOFF, "until", 0.0)
    monkeypatch.setitem(a._UPDATE_STATE, "error", None)
    monkeypatch.setattr(a, "_refresh_companion", lambda *x: companion.append(x))
    monkeypatch.setattr(a.urllib.request, "urlopen", lambda req, timeout=0: _Resp(data))
    ok = a.self_update(cfg, dict({"sha256": hashlib.sha256(data).hexdigest(), "version": "?"}, **offer))
    return ok, target.read_bytes(), a._UPDATE_STATE["error"], companion


def _local(key, **over):
    return _cfg(AVM_UPDATE_PUBKEYS=_pub(key).hex(), AVM_AUTO_UPDATE="true", **over)


def test_version_numbers_compare_numerically():
    assert a.version_key("1.10.0") > a.version_key("1.9.1") > a.version_key("1.9") == a.version_key("1.9.0")
    assert a.version_key("2") > a.version_key("1.99.99")
    for bad in ("", "1.9.1-rc1", "v1.9", "1..2", "1.9 "):
        assert a.version_key(bad) is None
    assert a.release_version("avalon_agent.py", b'AGENT_VERSION = "1.2"\r\nx = 1\r\n') == "1.2"
    assert a.release_version("avalon_agent.py", b'AGENT_VERSION = "1.2"\nAGENT_VERSION = "9.9"\n') is None   # twice
    assert a.release_version("avm.py", b'AGENT_VERSION = "1.2"\n') is None   # avm.py declares AVM_VERSION
    assert a.release_version("other.py", b'AGENT_VERSION = "1.2"\n') is None


def test_v1_only_offer_is_refused(monkeypatch, tmp_path, key):
    """F-12: a v1 signature (name + hash, no version) - all that releases <= 1.9.1 carry - is not enough any more."""
    data = _agent_src("99.0.0")
    ok, content, err, companion = _update(monkeypatch, tmp_path, _local(key), data, sig=_sig1(key, "avalon_agent.py", data),
                                          file_sigs={"avm.py": "x"})
    assert not ok and content == b"OLD = 1\n" and "refusing: release not version-signed" in err and companion == []


def test_signature_is_bound_to_the_declared_version(monkeypatch, tmp_path, key):
    """The agent builds the message from the version in the bytes it downloaded: a signature made for another version
    of the same content (or the same version of other content) does not verify."""
    data = _agent_src("99.0.0")
    ok, _, err, _ = _update(monkeypatch, tmp_path, _local(key), data, sig2=_sig2(key, "avalon_agent.py", data, "98.0.0"))
    assert not ok and "bad signature" in err


def test_downgrade_is_refused(monkeypatch, tmp_path, key):
    """F-12: whoever controls the hub can't replay an older signed release - e.g. one with a since-fixed hole."""
    data = _agent_src("0.0.1")
    ok, content, err, _ = _update(monkeypatch, tmp_path, _local(key), data, sig2=_sig2(key, "avalon_agent.py", data))
    assert not ok and content == b"OLD = 1\n"
    assert "refusing downgrade of avalon_agent.py from %s to 0.0.1" % a.AGENT_VERSION in err and "AVM_ALLOW_DOWNGRADE" in err


@pytest.mark.parametrize("version", [a.AGENT_VERSION, a.AGENT_VERSION + ".1", "99.0.0"])
def test_equal_or_higher_version_installs(monkeypatch, tmp_path, key, version):
    data = _agent_src(version, "NEW = 2")
    s2 = {"avm.py": "AVM-V2"}
    ok, content, err, companion = _update(monkeypatch, tmp_path, _local(key), data, sig="v1-ignored",
                                          sig2=_sig2(key, "avalon_agent.py", data), file_sigs={"avm.py": "AVM-V1"},
                                          file_sigs2=s2)
    assert ok and content == data and err is None
    assert companion and companion[0][1:] == ("avm.py", "AVM-V2")   # the companion gets its v2 signature, not v1


def test_allow_downgrade_is_local_only(monkeypatch, tmp_path, key):
    """AVM_ALLOW_DOWNGRADE=1 in this host's agent.conf allows a deliberate rollback; the hub can't set it."""
    data = _agent_src("0.0.1")
    ok, content, err, _ = _update(monkeypatch, tmp_path, _local(key, AVM_ALLOW_DOWNGRADE="1"), data,
                                  sig2=_sig2(key, "avalon_agent.py", data))
    assert ok and content == data and err is None
    assert "AVM_ALLOW_DOWNGRADE" not in a.REMOTE_KEYS
    remote = a.RemoteConfig(_local(key))
    remote.apply({"config": {"rev": 1, "settings": {"AVM_ALLOW_DOWNGRADE": "1", "AVM_TASK_LOG_OFF_DIRS": ""}}})
    eff = remote.effective()
    assert eff["AVM_ALLOW_DOWNGRADE"] == "false"
    ok, content, err, _ = _update(monkeypatch, tmp_path, eff, data, sig2=_sig2(key, "avalon_agent.py", data))
    assert not ok and content == b"OLD = 1\n" and "downgrade" in err


def test_companion_is_version_signed_and_never_downgraded(monkeypatch, tmp_path, key):
    monkeypatch.setattr(a, "_SELF_PATH", str(tmp_path / "avalon_agent.py"))
    avm = tmp_path / "avm.py"
    served = {}
    monkeypatch.setattr(a.urllib.request, "urlopen", lambda req, timeout=0: _Resp(served["data"]))

    def refresh(data, sig, **cfg):
        served["data"] = data
        a._refresh_companion(_local(key, **cfg), "avm.py", sig)
        return avm.read_bytes() if avm.exists() else None

    v1 = b'AVM_VERSION = "1.0.0"\nprint(1)\n'
    assert refresh(v1, _sig1(key, "avm.py", v1)) is None   # v1 signature passed where v2 belongs: refused
    assert refresh(v1, _sig2(key, "avm.py", v1)) == v1      # nothing on disk yet: any signed version installs
    v2 = b'AVM_VERSION = "2.0.0"\nprint(2)\n'
    assert refresh(v2, _sig2(key, "avm.py", v2)) == v2
    assert refresh(v1, _sig2(key, "avm.py", v1)) == v2      # older than the avm.py on disk: refused
    same = b'AVM_VERSION = "2.0.0"\nprint("2 again")\n'
    assert refresh(same, _sig2(key, "avm.py", same)) == same   # equal version is fine
    assert refresh(v1, _sig2(key, "avm.py", v1), AVM_ALLOW_DOWNGRADE="1") == v1


def test_sync_companions_passes_the_v2_signature(monkeypatch, tmp_path):
    monkeypatch.setattr(a, "_SELF_PATH", str(tmp_path / "avalon_agent.py"))
    monkeypatch.setattr(a, "_COMPANION_TRIED", {})
    seen = []
    monkeypatch.setattr(a, "_refresh_companion", lambda cfg, name, sig: seen.append((name, sig)))
    a.sync_companions(_cfg(AVM_AUTO_UPDATE="true"), {"files": {"avm.py": "f" * 64}, "file_sigs": {"avm.py": "V1"},
                                                      "file_sigs2": {"avm.py": "V2"}})
    assert seen == [("avm.py", "V2")]


def test_hub_forwards_v1_and_v2_signatures(monkeypatch, tmp_path, agent):
    """Transition: the hub offers .sig (v1, for agents <= 1.9.x) as sig/file_sigs and .sig2 (v2) as sig2/file_sigs2."""
    for name, text in (("avalon_agent.py", 'AGENT_VERSION = "1.10.0"\n'), ("avalon_agent.py.sig", "AG-V1\n"),
                       ("avalon_agent.py.sig2", "AG-V2\n"), ("avm.py", 'AVM_VERSION = "1.10.0"\n'),
                       ("avm.py.sig", "AVM-V1\n"), ("avm.py.sig2", "AVM-V2\n")):
        (tmp_path / name).write_text(text)
    monkeypatch.setattr(hub_main, "agent_bundle", core.AgentBundle(tmp_path / "avalon_agent.py"))
    reply = agent.post("/api/v1/ingest", json={"ts": 0, "interval": 5, "host": {"hostname": "x", "agent_version": "1.9.1"},
                                                "agent_sha256": "old"}).json()
    offer = reply["agent"]
    assert offer["version"] == "1.10.0"
    assert (offer["sig"], offer["file_sigs"]) == ("AG-V1", {"avm.py": "AVM-V1"})
    assert (offer["sig2"], offer["file_sigs2"]) == ("AG-V2", {"avm.py": "AVM-V2"})


# ------------------------------------------------------------------ F-19: unset temperature limits
def test_unset_sensor_limits_are_none(monkeypatch):
    """NVMe drives report an unset high/critical limit as 0xFFFF Kelvin: psutil shows 65261.85 degC."""
    T = collections.namedtuple("shwtemp", "label current high critical")
    monkeypatch.setattr(a.psutil, "sensors_temperatures", lambda: {"nvme": [
        T("Composite", 38.85, 65261.85, 65261.85), T("Sensor 1", 40.0, 84.85, 89.85), T("Sensor 2", 41.0, 0.0, None),
        T("Sensor 3", 42.0, 200.0, 199.0)]}, raising=False)
    got = {s["label"]: s for s in a.collect_temps()["sensors"]}
    assert (got["nvme Composite"]["high"], got["nvme Composite"]["critical"]) == (None, None)
    assert (got["nvme Sensor 1"]["high"], got["nvme Sensor 1"]["critical"]) == (84.85, 89.85)
    assert (got["nvme Sensor 2"]["high"], got["nvme Sensor 2"]["critical"]) == (None, None)
    assert (got["nvme Sensor 3"]["high"], got["nvme Sensor 3"]["critical"]) == (None, 199.0)


# ------------------------------------------------------------------ G-02: AVM_TASK_LOG_OFF_DIRS
@pytest.fixture
def dirs(tmp_path):
    off, near = tmp_path / "dua", tmp_path / "duality"   # "duality" shares a prefix, not a path component
    (off / "proj").mkdir(parents=True)
    near.mkdir()
    os.symlink(off / "proj", tmp_path / "link")
    return {"off": off, "in": off / "proj", "near": near, "link": tmp_path / "link"}


def test_in_off_dir_matches_real_paths_by_component(dirs, tmp_path):
    cfg = _cfg(AVM_TASK_LOG_OFF_DIRS="/nonexistent/x, %s" % dirs["off"])
    assert a.in_off_dir(cfg, str(dirs["off"])) and a.in_off_dir(cfg, str(dirs["in"]))
    assert a.in_off_dir(cfg, str(dirs["link"]))                       # symlink into an off dir
    assert not a.in_off_dir(cfg, str(dirs["near"])) and not a.in_off_dir(cfg, str(tmp_path))
    assert not a.in_off_dir(cfg, "") and not a.in_off_dir(_cfg(), str(dirs["in"]))
    os.symlink(dirs["off"], tmp_path / "offlink")                     # the listed dir itself a symlink
    assert a.in_off_dir(_cfg(AVM_TASK_LOG_OFF_DIRS=str(tmp_path / "offlink")), str(dirs["in"]))


def test_off_dir_sessions_report_no_snippets(dirs):
    """G-02a: sessions working in an off dir report no first/last lines even where snippets are on."""
    rows = {"sessions": [{"session_id": "a", "cwd": str(dirs["in"]), "first_prompt": "SECRET-1", "last_user": "SECRET-2"},
                         {"session_id": "b", "cwd": str(dirs["link"]), "last_assistant": "SECRET-3"},
                         {"session_id": "c", "cwd": str(dirs["near"]), "first_prompt": "kept-near"}],
            "agents": [{"key": "k", "name": "worker", "cwd": str(dirs["off"]), "last_said": "SECRET-4", "detail": "SECRET-5"}],
            "processes": [{"pid": 1, "cwd": str(dirs["in"]), "args": "-p SECRET-6"}, {"pid": 2, "cwd": "", "args": "kept-args"}]}
    r = _ActionRunner(AVM_TASK_LOG_OFF_DIRS=str(dirs["off"]), AVM_SESSION_SNIPPETS="on")
    r.sessions, r.agents, r.processes = (lambda: rows["sessions"]), (lambda: rows["agents"]), (lambda: rows["processes"])
    rep = r.report()
    sent = json.dumps(rep)
    assert "SECRET" not in sent and "kept-near" in sent and "kept-args" in sent and rep["policy"]["snippets"] is True
    assert rep["agents"][0]["name"] == "worker" and rep["sessions"][0]["cwd"] == str(dirs["in"])   # ids, names, cwd stay
    # marked, so the hub also forgets text it stored for the row before (a tracked session's last line)
    assert [s.get("redacted") for s in rep["sessions"]] == [True, True, None] and rep["agents"][0]["redacted"] is True


def test_snippets_off_marks_every_row_redacted():
    """AVM_SESSION_SNIPPETS=off: every row is stripped and marked, so the hub clears what it stored earlier."""
    r = _ActionRunner(AVM_SESSION_SNIPPETS="off")
    r.sessions, r.agents, r.processes = (lambda: [{"session_id": "a", "last_user": "SECRET"}]), (lambda: [{"key": "k", "detail": "SECRET"}]), (lambda: [])
    rep = r.report()
    assert "SECRET" not in json.dumps(rep) and rep["sessions"][0]["redacted"] is True and rep["agents"][0]["redacted"] is True


class _P:
    lines = [json.dumps(EV_ASSIST), json.dumps(EV_FINAL)]

    def __init__(self, args, **kw):
        import io
        self.stdout, self.stderr = io.StringIO("\n".join(self.lines) + "\n"), io.StringIO("")

    def wait(self):
        return 0


@pytest.mark.parametrize("where,configured,secret,answer", [
    ("in", "full", False, True),       # capped to summary: final answer only
    ("link", "full", False, True),
    ("near", "full", True, True),      # not in an off dir: unchanged
    ("in", "off", False, False),       # min(off, summary) = off
])
def test_off_dir_prompt_sends_at_most_a_summary(monkeypatch, dirs, where, configured, secret, answer):
    """G-02b: a dashboard prompt run inside an off dir sends at most a summary transcript (never tool inputs/results)."""
    monkeypatch.setattr(a.subprocess, "Popen", _P)
    r = _Runner(AVM_TASK_LOG_OFF_DIRS=str(dirs["off"]), AVM_JOB_TRANSCRIPT=configured)
    r._run_job({"id": 5, "prompt": "p", "mode": "plan", "cwd": str(dirs[where])})
    sent = json.dumps(r.posts)
    assert ("SECRET" in sent) is secret and ("FINAL-ANSWER" in sent) is answer and _finish(r)["status"] == "done"


# ------------------------------------------------------------------ G-03: resume cwd is local
class _CwdRunner(_ActionRunner):
    def __init__(self, rows, **cfg):
        super().__init__(**cfg)
        self.rows, self.bg_cwd = rows, "unset"

    def _claude(self, args, timeout, cwd=None):
        self.calls.append(args)
        if args[:1] == ["agents"]:
            return 0, json.dumps(self.rows)
        if args[:1] == ["respawn"]:
            return 1, "no"
        if args[:1] == ["--bg"]:
            self.bg_cwd = cwd
        return 0, "backgrounded · abcdef12"


SID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def _resume(r, hub_cwd="/hub/chosen/dir"):
    r._run_action({"id": 9, "action": "restart", "target": {"session_id": SID, "cwd": hub_cwd}})
    assert [c for c in r.calls if c[:1] == ["--bg"]], r.posts
    return r.bg_cwd


def test_resume_cwd_comes_from_the_local_record(tmp_path):
    """G-03: the hub's target cwd is ignored; the session runs where Claude on this host recorded it."""
    listed = [{"id": None, "sessionId": SID, "kind": "interactive", "pid": None, "cwd": "/srv/local/proj"}]
    assert _resume(_CwdRunner(listed, AVM_CLAUDE_HOME=str(tmp_path))) == "/srv/local/proj"
    # listed without a cwd and no transcript: the agent's default (None -> its home), never the hub's
    assert _resume(_CwdRunner([{"id": None, "sessionId": SID, "kind": "interactive"}], AVM_CLAUDE_HOME=str(tmp_path))) is None
    # not listed, known from its transcript on this host: the transcript's cwd
    proj = tmp_path / ".claude" / "projects" / "slug"
    proj.mkdir(parents=True)
    (proj / (SID + ".jsonl")).write_text(json.dumps({"type": "user", "cwd": "/home/u/work", "message": {"content": "hi"}}) + "\n")
    assert _resume(_CwdRunner([], AVM_CLAUDE_HOME=str(tmp_path))) == "/home/u/work"
