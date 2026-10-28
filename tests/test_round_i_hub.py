"""Audit round I, hub side: I-01 (a data-use host's task meta keeps only an allowlist: no avm note, no queue reason),
I-03 (when the data-use flag is first stored, text the hub already holds for the host is cleared; settings saves
re-read the host), I-04 (the check-settings PUT changes only the keys in its body). I-12 is in test_ui.py."""
import json

import pytest

from avalon_monitor import checks as ck
from avalon_monitor import core, datause
from avalon_monitor.db import Database
from conftest import WRITE, report_policy

FULL = {"max_mode": "plan", "actions": True, "jobs": True, "transcript": "full", "snippets": True}
SUMMARY = {**FULL, "transcript": "summary", "snippets": False}
# what avm sends (agent/avm.py: _task_body, _run_queued, _code_identity), with free text where a user's words go
AVM_META = {"hostname": "box", "resources": {"gpu": "exclusive", "vram_gb": 8.0}, "note": "SECRET note",
            "queue": {"since": 100.0, "reason": "SECRET reason", "position": 2, "max_wait": 3600},
            "code": {"sha": "abc123def456", "dirty": True, "at": 101.0}}


@pytest.fixture
def db(tmp_path):
    d = Database(str(tmp_path / "x.db"))
    yield d
    d.close()


def _flag(row):
    return (core.db.host_by_name(row.name).check_config or {}).get(datause.KEY)


def _served(agent, task_id):
    return json.dumps([core.db.task_get(task_id), agent.get("/api/v1/tasks/mine").json()])


# ------------------------------------------------------------------ I-01
def test_data_use_task_meta_keeps_only_the_allowlist(agent, host):
    """I-01: on a data-use host avm's meta.note and meta.queue.reason are neither stored nor served, at start or beat;
    resources, queue since/position/waited and the code sha stay (the task page shows them)."""
    report_policy(agent, policy=SUMMARY)
    t = agent.post("/api/v1/tasks", json={"name": "train", "source": "run", "status": "queued", "meta": AVM_META}).json()["task"]
    agent.post(f"/api/v1/tasks/{t['id']}/beat", json={"status": "queued", "meta": {"queue": {"since": 100.0, "reason": "SECRET 2", "position": 1}}})
    agent.post(f"/api/v1/tasks/{t['id']}/beat", json={"status": "running", "meta": {"note": "SECRET 3",
                                                                                   "queue": {"since": 100.0, "waited": 30.0}}})
    assert "SECRET" not in _served(agent, t["id"])
    meta = json.loads(core.db.task_get(t["id"])["meta"])
    assert meta == {"hostname": "box", "resources": {"gpu": "exclusive", "vram_gb": 8.0}, "queue": {"since": 100.0, "waited": 30.0},
                    "code": {"sha": "abc123def456", "dirty": True, "at": 101.0}}


def test_task_meta_kept_whole_elsewhere(agent, host):
    """I-01 control: an unrestricted host's meta is stored as sent."""
    report_policy(agent, policy=FULL)
    t = agent.post("/api/v1/tasks", json={"name": "train", "source": "run", "meta": AVM_META}).json()["task"]
    assert json.loads(core.db.task_get(t["id"])["meta"]) == AVM_META


def test_keep_meta():
    """I-01: queue.disabled (an error text) and unknown keys go; a None value stays (a beat's way to remove a key)."""
    assert datause.keep_meta({"queue": {"disabled": "SECRET /data/x: permission denied"}, "x": 1}) == {"queue": {}}
    assert datause.keep_meta({"queue": None, "note": "SECRET"}) == {"queue": None}
    assert datause.keep_meta({"note": "SECRET"}) is None and datause.keep_meta(None) is None
    assert datause.keep_meta({"code": {"sha": "a", "msg": "SECRET"}, "resources": {"gpu": "shared", "x": {"y": 1}}}) == \
        {"code": {"sha": "a"}, "resources": {"gpu": "shared"}}


# ------------------------------------------------------------------ I-03
def test_first_restriction_clears_stored_task_text(agent, host):
    """I-03: tasks stored while the host was unrestricted lose command, cwd, note, output lines and meta free text when
    the flag is first stored (before, they stayed on show for good)."""
    row = host[0]
    report_policy(agent, policy=FULL)
    t = agent.post("/api/v1/tasks", json={"name": "train", "source": "run", "command": "python SECRET.py", "cwd": "/data/SECRET",
                                          "meta": AVM_META}).json()["task"]
    agent.post(f"/api/v1/tasks/{t['id']}/finish", json={"status": "done", "rc": 0, "log": ["SECRET line"], "note": "SECRET done"})
    assert "SECRET" in _served(agent, t["id"])
    report_policy(agent, policy=SUMMARY)
    assert _flag(row) == "restricted"
    stored = core.db.task_get(t["id"])
    assert "SECRET" not in _served(agent, t["id"])
    assert (stored["command"], stored["cwd"], stored["note"], stored["log"], stored["rc"], stored["status"]) == (None, None, None, "", 0, "done")
    assert json.loads(stored["meta"])["code"]["sha"] == "abc123def456"


def test_first_restriction_reduces_stored_job_events_to_summary(agent, host):
    """I-03: a prompt's events stored before the restriction are cut to what AVM_JOB_TRANSCRIPT=summary would have
    sent: tool names and the final answer, no tool inputs/results or intermediate text."""
    row = host[0]
    report_policy(agent, policy=FULL)
    j = core.db.job_create(row.id, "look at it", None, "plan", None, None, "test")
    core.db.job_add_events(j["id"], row.id, [
        {"type": "system", "subtype": "init", "session_id": "s1", "model": "m"},
        {"type": "assistant", "model": "m", "parts": [{"type": "text", "text": "SECRET thinking"},
                                                      {"type": "tool_use", "name": "Read", "input": {"file_path": "/data/SECRET.csv"}}]},
        {"type": "user", "parts": [{"type": "tool_result", "content": "SECRET rows"}]},
        {"type": "result", "result": "final answer"}])
    report_policy(agent, policy=SUMMARY)
    events = core.db.job_events(j["id"])
    assert "SECRET" not in json.dumps(events)
    assert [e["type"] for e in events] == ["system", "assistant", "user", "result"]
    assert events[1]["parts"] == [{"type": "tool_use", "name": "Read", "input": ""}] and events[3]["result"] == "final answer"


def test_first_restriction_shortens_tracked_cwd(agent, host):
    """I-03: a tracked session's text is cleared and its cwd cut to the last part when the flag is first stored, and
    later reports' cwds are stored the same way (a stopped row stays listed for good)."""
    row = host[0]
    core.db.tracked_add(row.id, "bg1", {"session_id": "s1", "name": "w", "cwd": "/home/u/SECRET-cohort/proj"}, "test")
    report_policy(agent, policy=FULL, agents=[{"key": "bg1", "session_id": "s1", "live": True, "last_said": "SECRET said",
                                              "cwd": "/home/u/SECRET-cohort/proj"}])
    report_policy(agent, policy=SUMMARY)   # bg1 not in this report: the purge alone clears it
    t = core.db.tracked_find(row.id, "bg1")
    assert t["cwd"] == "proj" and t["last_said"] is None and t["name"] == "w"
    report_policy(agent, policy=SUMMARY, agents=[{"key": "bg1", "session_id": "s1", "live": True, "cwd": "C:\\Users\\u\\SECRET\\other"}])
    assert core.db.tracked_find(row.id, "bg1")["cwd"] == "other"
    assert "SECRET" not in json.dumps(agent.get("/api/v1/claude/tracked").json())


def test_tracked_cwd_kept_on_a_normal_host(agent, host):
    """I-03 control: an unrestricted host's tracked cwd stays whole (responders resume there)."""
    row = host[0]
    core.db.tracked_add(row.id, "bg1", {"session_id": "s1", "name": "w"}, "test")
    report_policy(agent, policy=FULL, agents=[{"key": "bg1", "session_id": "s1", "live": True, "cwd": "/home/u/proj"}])
    assert core.db.tracked_find(row.id, "bg1")["cwd"] == "/home/u/proj"


def test_sticky_store_rereads_the_host(agent, host, monkeypatch):
    """I-03: the report was authenticated with the host row as it was (flag unset), and an operator set
    AVM_DATA_USE=off before the hub stored the flag: off stays (it was overwritten with restricted, from the stale row)."""
    row = host[0]
    stale = core.db.host_by_name(row.name)
    ck.put_settings(core.db, stale, {datause.KEY: "off"}, "operator")
    monkeypatch.setattr(core.db, "host_by_token", lambda token: stale)
    report_policy(agent, policy=SUMMARY)
    assert _flag(row) == "off"


def test_put_settings_rereads_the_host(db):
    """I-03: a save made with a host row read before another save keeps the other save's keys (it wrote back the
    settings it had read, dropping AVM_DATA_USE=restricted stored in between)."""
    h, _ = db.create_host("h1")
    stale = db.host_by_name("h1")
    ck.put_settings(db, stale, {datause.KEY: "restricted"}, "data-use policy")
    ck.put_settings(db, stale, {"AVM_TAGS": "gpu"}, "operator")
    assert db.host_by_name("h1").check_config == {datause.KEY: "restricted", "AVM_TAGS": "gpu"}


# ------------------------------------------------------------------ I-04
def test_check_settings_put_changes_only_the_keys_sent(tunnel, agent, host):
    """I-04: a partial check-settings body leaves the settings it doesn't name alone (it erased the sticky
    AVM_DATA_USE, so the next full report un-restricted the host with no alert); "" still removes the key it names."""
    row = host[0]
    report_policy(agent, policy=SUMMARY)
    r = tunnel.put(f"/api/v1/hosts/{row.name}/check-settings", headers=WRITE, json={"AVM_PROCESSES": "12", "AVM_TAGS": "gpu"})
    assert r.status_code == 200, r.text
    r = tunnel.put(f"/api/v1/hosts/{row.name}/check-settings", headers=WRITE, json={"AVM_TAGS": ""})
    assert r.status_code == 200, r.text
    cfg = core.db.host_by_name(row.name).check_config
    assert cfg[datause.KEY] == "restricted" and cfg["AVM_PROCESSES"] == "12" and "AVM_TAGS" not in cfg
    report_policy(agent, policy=FULL)
    assert datause.restricted(core.db.host_by_name(row.name))
    t = agent.post("/api/v1/tasks", json={"name": "n", "source": "run", "command": "python SECRET.py"}).json()["task"]
    assert core.db.task_get(t["id"])["command"] is None
