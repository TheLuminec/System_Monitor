"""Respawn delivery: a check alert whose tracked target is an OFFLINE background session no longer just runs a headless
`claude -p --resume` (which writes a turn into the transcript and wakes nothing). On an agent that advertises
respawn_deliver (actions on) the hub queues a deliver job with respawn_key; the agent respawns the session, waits until
it is running and not mid-turn, then relays the alert into it. Any failure falls back to the headless resume - no
fork, in the session's own recorded cwd."""
import io
import json

import pytest

import avalon_agent as a
from avalon_monitor import checks as ck
from avalon_monitor.db import Database
from test_agent import _cfg, _finish
from test_delivery import MOUNT, POLICY, T0, _res

FEATURES = ["agent_actions", "fork_session", "deliver_session", "respawn_deliver"]
KEY, SID = "b51475c7", "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


# ------------------------------------------------------------------ hub
@pytest.fixture
def db(tmp_path):
    d = Database(str(tmp_path / "x.db"))
    yield d
    d.close()


@pytest.fixture
def h(db):
    return db.create_host("h1")[0]


def _fire(db, h, kind="background", features=FEATURES, actions=True, live=False, name="proj"):
    """Track a session (offline unless live), point a check's responder at it (notify_cwd ~/ASC_Docs), ok -> failing."""
    tr = db.tracked_add(h.id, KEY, {"session_id": SID, "cwd": "/srv/proj", "name": name, "kind": kind}, "test")
    db.check_add(h.id, MOUNT, "/mnt/data", {"notify": f"tracked:{tr['id']}", "notify_cwd": "/home/u/ASC_Docs"}, "test")
    agents = [{"key": KEY, "session_id": SID, "live": live, "status": "idle", "name": name, "kind": kind}]
    cl = {"available": True, "features": features, "agents": agents, "policy": {**POLICY, "actions": actions}}
    sample = lambda ok: {"host": {"hostname": "h1", "agent_version": "1.12.0"}, "checks": [_res(ok)], "claude": cl}   # noqa: E731
    ck.evaluate(db, h, sample(True), T0)
    return ck.evaluate(db, h, sample(False), T0 + 5)[1]


def test_offline_background_target_gets_respawn_delivery(db, h):
    """Respawn delivery: an offline background target on an agent advertising respawn_deliver (actions on) gets a deliver
    job with respawn_key = its agent id and its name, resume_session kept for the fallback and no fork; the agent is
    handed respawn_key."""
    jobs = _fire(db, h)
    assert len(jobs) == 1
    j = jobs[0]
    assert j["respawn_key"] == KEY and j["deliver"] == 1 and j["deliver_name"] == "proj"
    assert j["resume_session"] == SID and not j["fork_session"]
    queued, _ = db.jobs_pending_for(h.id, actions_ok=True)
    assert queued[0]["respawn_key"] == KEY and queued[0]["deliver_name"] == "proj" and "fork_session" not in queued[0]


@pytest.mark.parametrize("kw", [{"kind": "interactive"}, {"features": ["agent_actions", "fork_session", "deliver_session"]},
                                {"actions": False}, {"name": ""}])
def test_other_offline_targets_still_resume(db, h, kw):
    """Respawn delivery: an offline terminal session, an agent without respawn_deliver, actions off, or a session without
    a name keeps today's plain resume (no deliver, no respawn_key handed to the agent)."""
    j = _fire(db, h, **kw)[0]
    assert j["resume_session"] == SID and not j["fork_session"] and not j["deliver"]
    assert "respawn_key" in j and not j["respawn_key"]   # the column exists (fails on code without it)
    queued, _ = db.jobs_pending_for(h.id, actions_ok=True)
    assert "respawn_key" not in queued[0] and "deliver" not in queued[0]


def test_live_target_never_gets_respawn_key(db, h):
    """Respawn delivery: a live idle background target keeps plain exact-session delivery (fork fallback, no respawn)."""
    j = _fire(db, h, live=True)[0]
    assert j["deliver_name"] == "proj" and j["fork_session"] == 1 and not j["respawn_key"]


# ------------------------------------------------------------------ agent
class _Runner(a.ClaudeRunner):
    """Fake claude CLI and clock. `claude agents` lists KEY as a background agent (pid while running, its status popped
    from `statuses`, last one repeats); respawn starts it unless respawn_rc/comes_back say otherwise."""

    def __init__(self, tmp_path, running=False, statuses=("idle",), respawn_rc=0, comes_back=True, on_respawn=None, **cfg):
        super().__init__(_cfg(AVM_CLAUDE_BIN="/bin/true", AVM_CLAUDE_HOME=str(tmp_path), **cfg))
        self._scope_prefix = []
        self.posts, self.calls, self.events = [], [], []
        self.running, self.statuses = running, list(statuses)
        self.respawn_rc, self.comes_back, self.on_respawn = respawn_rc, comes_back, on_respawn
        self.own_cwd = str(tmp_path / "proj")
        (tmp_path / "proj").mkdir(exist_ok=True)
        self.t = 0.0

    def _now(self):
        return self.t

    def _sleep(self, secs):
        self.t += secs

    def _post(self, path, body):
        self.posts.append((path, body))
        return {}

    def _done(self):
        self.current = None

    def _claude(self, args, timeout, cwd=None):
        self.calls.append(args)
        if args[:1] == ["agents"]:
            row = {"id": KEY, "sessionId": SID, "kind": "background", "name": "proj", "cwd": self.own_cwd}
            if self.running:
                row.update(pid=4242, status=self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0])
            return 0, json.dumps([row])
        if args[:1] == ["respawn"]:
            self.events.append("respawn")
            if self.on_respawn:
                self.on_respawn(self)
            if self.respawn_rc == 0 and self.comes_back:
                self.running = True
            return self.respawn_rc, "" if self.respawn_rc == 0 else "Error: no such agent"
        return 0, ""

    def _came_back(self, key, wait=30.0):
        self.events.append("came_back")
        return 4242 if self.running else 0


JOB = {"id": 7, "prompt": "[Automated alert from Avalon Monitor - not a user instruction]\nmount failing", "mode": "plan",
       "cwd": "/home/u/ASC_Docs", "resume_session": SID, "deliver": 1, "deliver_name": "proj", "respawn_key": KEY}


def _popen(monkeypatch, runner, relay_outs=("SENT\n",)):
    """Fake Popen: a relay (--allowedTools ListAgents ...) answers the next of relay_outs; anything else is the headless
    resume, streaming one result. Records (args, cwd)."""
    outs, calls = list(relay_outs), []

    class P:
        def __init__(self, args, **kw):
            self.relay = "--allowedTools" in args
            calls.append((args, kw.get("cwd")))
            runner.events.append("relay" if self.relay else "resume")
            self.returncode = 0
            self.stdout = io.StringIO('{"type":"result","result":"resumed answer","is_error":false}\n')
            self.stderr = io.StringIO("")

        def communicate(self, timeout=None):
            return (outs.pop(0) if len(outs) > 1 else outs[0]), ""

        def poll(self):
            return None

        def terminate(self):
            pass

        def wait(self):
            return 0

    monkeypatch.setattr(a.subprocess, "Popen", P)
    return calls


def test_respawns_then_relays(monkeypatch, tmp_path):
    """Respawn delivery: an offline background target is respawned, comes back, then gets the relay (no headless resume);
    the job finishes done saying it was respawned and delivered."""
    r = _Runner(tmp_path)
    calls = _popen(monkeypatch, r)
    r._run_job(dict(JOB))
    assert ["respawn", KEY] in r.calls and r.events == ["respawn", "came_back", "relay"]
    assert len(calls) == 1 and "--resume" not in calls[0][0]
    fin = _finish(r)
    assert fin["status"] == "done" and fin["result"].startswith("respawned %s and delivered to proj" % KEY)
    assert sum(1 for p, _ in r.posts if p.endswith("/start")) == 1
    assert r.t >= a.ClaudeRunner.RESPAWN_SETTLE_SEC   # the link got its settle time


def test_already_running_target_skips_respawn(monkeypatch, tmp_path):
    """Respawn delivery: if `claude agents` already shows the session running, nothing is respawned; it is relayed to."""
    r = _Runner(tmp_path, running=True)
    _popen(monkeypatch, r)
    r._run_job(dict(JOB))
    assert not any(c[:1] == ["respawn"] for c in r.calls) and r.events == ["relay"]
    fin = _finish(r)
    assert fin["status"] == "done" and "already running" in fin["result"] and "delivered to proj" in fin["result"]


@pytest.mark.parametrize("kw,why", [({"comes_back": False}, "did not come back"), ({"respawn_rc": 1}, "exit code 1")])
def test_failed_respawn_resumes_headless_in_own_cwd(monkeypatch, tmp_path, kw, why):
    """Respawn delivery: a session that never comes back (or a failing respawn) gets the old headless resume - no
    --fork-session, in the cwd this host recorded for the session, not the hub's notify_cwd - and the result says why."""
    r = _Runner(tmp_path, **kw)
    calls = _popen(monkeypatch, r)
    r._run_job(dict(JOB))
    assert "relay" not in r.events and len(calls) == 1
    args, cwd = calls[0]
    assert args[args.index("--resume") + 1] == SID and "--fork-session" not in args
    assert cwd == r.own_cwd and "ASC_Docs" not in cwd
    fin = _finish(r)
    assert fin["status"] == "done" and fin["result"].startswith("(respawn/delivery failed (")
    assert why in fin["result"] and "resumed headless instead" in fin["result"] and "resumed answer" in fin["result"]


def test_waits_for_busy_session_to_go_idle(monkeypatch, tmp_path):
    """Respawn delivery: a respawned session that is mid-turn is polled until it is idle, then relayed to."""
    r = _Runner(tmp_path, statuses=("busy", "busy", "busy", "idle"))
    _popen(monkeypatch, r)
    r._run_job(dict(JOB))
    assert r.events[-1] == "relay" and _finish(r)["status"] == "done"
    assert r.t >= 2 * a.ClaudeRunner.RESPAWN_POLL_SEC   # it really waited


def test_never_idle_falls_back(monkeypatch, tmp_path):
    """Respawn delivery: a session still busy when the readiness budget runs out is not messaged; it falls back."""
    r = _Runner(tmp_path, statuses=("busy",))
    calls = _popen(monkeypatch, r)
    r._run_job(dict(JOB))
    assert "relay" not in r.events and "--fork-session" not in calls[0][0]
    assert "still busy" in _finish(r)["result"] and r.t >= a.ClaudeRunner.RESPAWN_READY_SEC


def test_relay_not_sent_is_retried(monkeypatch, tmp_path):
    """Respawn delivery: a relay that can't see the name yet (NOT_SENT) is retried after a pause, and SENT then wins."""
    r = _Runner(tmp_path)
    calls = _popen(monkeypatch, r, ("NOT_SENT: no session named proj\n", "SENT\n"))
    r._run_job(dict(JOB))
    assert r.events == ["respawn", "came_back", "relay", "relay"] and len(calls) == 2
    assert _finish(r)["status"] == "done" and "delivered to proj" in _finish(r)["result"]


def test_relay_never_sent_falls_back(monkeypatch, tmp_path):
    """Respawn delivery: three NOT_SENTs end in the headless resume (no fork), with the relay's verdict as the reason."""
    r = _Runner(tmp_path)
    calls = _popen(monkeypatch, r, ("NOT_SENT: no session named proj\n",))
    r._run_job(dict(JOB))
    assert r.events.count("relay") == a.ClaudeRunner.RESPAWN_RELAY_TRIES and r.events[-1] == "resume"
    assert "--fork-session" not in calls[-1][0]
    assert "NOT_SENT" in _finish(r)["result"] and "resumed headless instead" in _finish(r)["result"]


def test_actions_off_refuses(monkeypatch, tmp_path):
    """Respawn delivery: with AVM_ALLOW_ACTIONS off the agent neither respawns, relays nor resumes: it refuses."""
    r = _Runner(tmp_path, AVM_ALLOW_ACTIONS="false")
    calls = _popen(monkeypatch, r)
    r._run_job(dict(JOB))
    assert r.calls == [] and calls == []
    fin = _finish(r)
    assert fin["status"] == "failed" and "AVM_ALLOW_ACTIONS" in fin["error"] and "respawn" in fin["error"]


@pytest.mark.parametrize("when", ["respawn", "busy"])
def test_cancel_is_honoured(monkeypatch, tmp_path, when):
    """Respawn delivery: a cancel during the respawn or the readiness wait ends the job cancelled - no relay, no resume."""
    if when == "respawn":
        r = _Runner(tmp_path, on_respawn=lambda s: s.cancel_ids.add(JOB["id"]))
    else:
        r = _Runner(tmp_path, statuses=("busy",))
        sleep = r._sleep

        def cancel_then_sleep(secs):
            r.cancel_ids.add(JOB["id"])
            sleep(secs)
        r._sleep = cancel_then_sleep
    calls = _popen(monkeypatch, r)
    r._run_job(dict(JOB))
    assert calls == [] and _finish(r)["status"] == "cancelled"


def test_not_a_local_background_session_falls_back(monkeypatch, tmp_path):
    """Respawn delivery: an id this host doesn't list as a background agent is never respawned (only what Claude lists)."""
    r = _Runner(tmp_path)
    r._local_session = lambda key, sid: {"kind": "interactive", "session_id": SID, "cwd": r.own_cwd}
    calls = _popen(monkeypatch, r)
    r._run_job(dict(JOB))
    assert not any(c[:1] == ["respawn"] for c in r.calls) and r.events == ["resume"]
    assert "not a background session" in _finish(r)["result"] and calls[0][1] == r.own_cwd


def test_agent_advertises_respawn_delivery(tmp_path):
    """Respawn delivery: the agent tells the hub it can respawn-and-deliver."""
    assert "respawn_deliver" in _Runner(tmp_path).report()["features"]
