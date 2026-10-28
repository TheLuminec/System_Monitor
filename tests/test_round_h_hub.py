"""Audit round H, hub side: G-01 (a data-use restriction reported by the agent is sticky), G-04 (check responders go
through the host's policy refusal), G-06 (task notes stay off the hub on data-use hosts), F-15 (the Host allowlist
pins this hub's tailnet), F-21 (known_state backfill; adoption keeps the hub's declared lists)."""
import json
import logging
import subprocess
import time

import pytest

from avalon_monitor import checks as ck
from avalon_monitor import core, datause
from avalon_monitor.db import Database
from avalon_monitor.main import host_public
from conftest import WRITE, report_policy

MOUNT, UNITS, MEM = "AVM_WATCH_MOUNTS", "AVM_WATCH_UNITS", "AVM_MEM_AVAILABLE_WARN_GB"
FULL = {"max_mode": "plan", "actions": True, "jobs": True, "transcript": "full", "snippets": True}
SUMMARY = {**FULL, "transcript": "summary", "snippets": False}
UI_SETTINGS = ("AVM_COMMIT_WARN_PCT", "AVM_TAILSCALE_MIN", "AVM_DATA_USE", "AVM_PROCESSES", "AVM_TAGS")   # app.js SETTING_LABELS


def _flag(host_row):
    return (core.db.host_by_name(host_row.name).check_config or {}).get(datause.KEY)


def _last(host_row):
    return core.db.host_by_name(host_row.name).last_sample


def _rule(host_row, name="data-use policy"):
    return [c for c in _last(host_row).get("checks") or [] if c.get("name") == name]


def _task_command(agent):
    t = agent.post("/api/v1/tasks", json={"name": "n", "source": "run", "command": "python SECRET.py", "cwd": "/SECRET"}).json()["task"]
    return core.db.task_get(t["id"])["command"]


# ------------------------------------------------------------------ G-01
def test_restricted_report_stores_the_flag_and_it_sticks(agent, host):
    """The agent reports summary/no snippets and the host has no AVM_DATA_USE: the hub stores restricted. When the
    agent.conf later loses its lines (transcript=full, snippets on), the host stays restricted."""
    row = host[0]
    report_policy(agent, policy=SUMMARY)
    assert _flag(row) == "restricted"
    reply = report_policy(agent, policy=FULL, sessions=[{"session_id": "s", "first_prompt": "SECRET"}])
    assert _flag(row) == "restricted" and reply["config"]["settings"][datause.KEY] == "restricted"
    assert datause.restricted(core.db.host_by_name(row.name))
    assert "SECRET" not in json.dumps(_last(row))               # snippets still scrubbed
    assert _task_command(agent) is None                          # and task commands dropped


def test_mismatch_raises_a_data_use_alert_until_it_matches_again(agent, host, caplog):
    row = host[0]
    report_policy(agent, policy=SUMMARY)
    assert _rule(row) == []
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="avalon"):
        report_policy(agent, policy=FULL)
        report_policy(agent, policy=FULL)
    alert = _rule(row)
    assert len(alert) == 1 and alert[0]["ok"] is False and "AVM_DATA_USE" in alert[0]["detail"]
    assert sum(row.name in m and "now reports transcript=full" in m for m in caplog.messages) == 1   # once per episode
    assert host_public(core.db.host_by_name(row.name))["summary"]["checks_failing"] >= 1   # red on the dashboard
    assert _flag(row) == "restricted" and "SECRET" not in json.dumps(_task_command(agent))
    report_policy(agent, policy=SUMMARY)                         # agent.conf fixed: alert gone
    assert _rule(row) == []


def test_explicit_off_is_never_overwritten(tunnel, agent, host):
    """An operator lifted it (AVM_DATA_USE=off, through the host settings panel): a restricted report doesn't set it
    back - the host is still restricted through the agent's own policy - and a full report un-restricts it, no alert."""
    row = host[0]
    body = {k: "" for k in UI_SETTINGS}
    r = tunnel.put(f"/api/v1/hosts/{row.name}/check-settings", headers=WRITE, json={**body, datause.KEY: "off"})
    assert r.status_code == 200, r.text
    report_policy(agent, policy=SUMMARY)
    assert _flag(row) == "off"
    assert _task_command(agent) is None                          # restricted via the policy branch
    report_policy(agent, policy=FULL)
    assert _flag(row) == "off" and _rule(row) == []
    assert _task_command(agent) == "python SECRET.py"


def test_no_policy_or_full_policy_sets_nothing(agent, host):
    row = host[0]
    report_policy(agent, policy=False)                           # agent older than 1.9: no policy, nothing to go on
    report_policy(agent, policy=FULL)
    assert _flag(row) is None and _rule(row) == []


@pytest.mark.parametrize("flag,reported,persist,alert", [
    ("", SUMMARY, True, False), ("garbage", SUMMARY, True, False), ("off", SUMMARY, False, False),
    ("None", SUMMARY, False, False), ("restricted", SUMMARY, False, False), ("restricted", FULL, False, True),
    ("on", FULL, False, True), ("off", FULL, False, False), ("", FULL, False, False), ("", None, False, False),
])
def test_sticky_decision(flag, reported, persist, alert):
    class H:
        check_config = {datause.KEY: flag}
    got = datause.sticky(H, {"policy": reported} if reported else {})
    assert got[0] is persist and bool(got[1]) is alert


def test_settings_panel_reads_and_writes_the_data_use_flag(tunnel, agent, host):
    """The panel sends every SETTING_LABELS key: AVM_DATA_USE was refused (422 for the whole save), and the checklist
    didn't show it - so saving the other settings would have erased a sticky flag."""
    row = host[0]
    report_policy(agent, policy=SUMMARY)
    shown = tunnel.get(f"/api/v1/hosts/{row.name}/checklist").json()["settings"]
    assert shown[datause.KEY] == "restricted"
    r = tunnel.put(f"/api/v1/hosts/{row.name}/check-settings", headers=WRITE, json={**shown, "AVM_TAGS": "gpu"})
    assert r.status_code == 200, r.text
    assert _flag(row) == "restricted" and core.db.host_by_name(row.name).check_config["AVM_TAGS"] == "gpu"


# ------------------------------------------------------------------ G-04
@pytest.fixture
def db(tmp_path):
    d = Database(str(tmp_path / "x.db"))
    yield d
    d.close()


def _res(target, ok, key=MOUNT):
    return {"name": target, "kind": ck.KIND_OF_KEY[key], "ok": ok, "key": key, "src": target, "detail": "ok" if ok else "NOT mounted"}


def _sample(*results, policy=FULL, version="1.9.0"):
    claude = {"available": True, "features": ["agent_actions", "fork_session"], "agents": []}
    if policy is not None:
        claude["policy"] = policy
    return {"host": {"hostname": "h1", "agent_version": version}, "checks": list(results), "claude": claude}


@pytest.mark.parametrize("policy,why", [(None, "no local policy"), ({**FULL, "jobs": False}, "root/SYSTEM"),
                                        ({**FULL, "max_mode": "bogus"}, None)])
def test_responder_goes_through_the_hosts_policy(db, policy, why):
    """An agent with no policy (no local cap) or running as root gets no alert prompt: the alert stays pending with
    the reason, and goes out once the agent reports a policy that takes a plan prompt."""
    h = db.create_host("h1")[0]
    c = db.check_add(h.id, MOUNT, "/mnt/data", {"notify": "new"}, "test")
    t = 1_800_000_000.0
    ck.evaluate(db, h, _sample(_res("/mnt/data", True), policy=policy), t)
    _, jobs = ck.evaluate(db, h, _sample(_res("/mnt/data", False), policy=policy), t + 5)
    row = db.check_get(c["id"])
    if why is None:   # an unknown max_mode counts as plan: allowed
        assert len(jobs) == 1 and row["hold_reason"] is None
        return
    assert jobs == [] and db.job_list(h.id) == []
    assert row["state"] == "failing" and row["pending"] == 1 and why in row["hold_reason"]
    _, jobs = ck.evaluate(db, h, _sample(_res("/mnt/data", False)), t + 10)
    row = db.check_get(c["id"])
    assert len(jobs) == 1 and jobs[0]["mode"] == "plan" and row["pending"] == 0 and row["hold_reason"] is None


def test_responder_refused_through_ingest(agent, host):
    row = host[0]
    c = core.db.check_add(row.id, MOUNT, "/mnt/g04", {"notify": "new"}, "test")

    def push(ok):
        body = {"ts": time.time(), "host": {"hostname": "x", "agent_version": "1.8.1"}, "checks": [_res("/mnt/g04", ok)],
                "claude": {"available": True, "features": ["agent_actions"], "agents": []}}   # 1.8: no policy
        assert agent.post("/api/v1/ingest", json=body).status_code == 200

    push(True)
    push(False)
    assert [j for j in core.db.job_list(row.id) if j["created_by"] == "check responder"] == []
    got = core.db.check_get(c["id"])
    assert got["pending"] == 1 and "no local policy" in got["hold_reason"]


def test_hold_reason_names_cooldown_and_cap(db):
    h = db.create_host("h1")[0]
    c = db.check_add(h.id, MOUNT, "/mnt/data", {"notify": "new", "daily_cap": 1}, "test")
    t = 1_800_000_000.0
    ck.evaluate(db, h, _sample(_res("/mnt/data", True)), t)
    assert len(ck.evaluate(db, h, _sample(_res("/mnt/data", False)), t + 1)[1]) == 1
    ck.evaluate(db, h, _sample(_res("/mnt/data", True)), t + 2)
    ck.evaluate(db, h, _sample(_res("/mnt/data", False)), t + 3)
    assert "daily cap" in db.check_get(c["id"])["hold_reason"]


# ------------------------------------------------------------------ G-06
def test_task_notes_dropped_on_data_use_hosts(agent, host):
    report_policy(agent, policy=SUMMARY)
    t = agent.post("/api/v1/tasks", json={"name": "train", "source": "task"}).json()["task"]
    agent.post(f"/api/v1/tasks/{t['id']}/beat", json={"note": "SECRET epoch 3", "progress": 0.5})
    assert core.db.task_get(t["id"])["note"] is None
    agent.post(f"/api/v1/tasks/{t['id']}/finish", json={"status": "failed", "rc": 2, "note": "SECRET row 7 bad"})
    stored = core.db.task_get(t["id"])
    assert stored["note"] is None and stored["rc"] == 2 and stored["status"] == "failed" and stored["progress"] == 0.5


def test_task_notes_kept_elsewhere(agent, host):
    report_policy(agent, policy=FULL)
    t = agent.post("/api/v1/tasks", json={"name": "train", "source": "task"}).json()["task"]
    agent.post(f"/api/v1/tasks/{t['id']}/finish", json={"status": "failed", "rc": 2, "note": "OOM at step 40"})
    assert core.db.task_get(t["id"])["note"] == "OOM at step 40"


# ------------------------------------------------------------------ F-15
@pytest.mark.parametrize("name,ok", [("{h}.tail1234.ts.net", True), ("{h}.tail1234.ts.net:8787", True),
                                     ("{h}.other-tailnet.ts.net", False), ("{h}.tail1234.ts.net.evil.example", False),
                                     ("attacker.tail1234.ts.net", False), ("testserver", True), ("100.64.1.2:8787", True)])
def test_pinned_tailnet(client, monkeypatch, name, ok):
    monkeypatch.setattr(core, "_tailnet", "tail1234.ts.net")
    r = client.get("/api/v1/config", headers={"host": name.format(h=core._HOSTNAME)})
    assert (r.status_code != 421) is ok, (name, r.status_code)


def test_unpinned_tailnet_keeps_the_old_rule(client, monkeypatch):
    monkeypatch.setattr(core, "_tailnet", "")
    assert client.get("/api/v1/config", headers={"host": f"{core._HOSTNAME}.other-tailnet.ts.net"}).status_code != 421


def test_tailnet_setting_is_read_from_the_environment(monkeypatch):
    from avalon_monitor import config
    monkeypatch.setenv("AVM_TAILNET", " .Tail1234.TS.net. ")
    assert config.load_settings().tailnet == "tail1234.ts.net"
    monkeypatch.delenv("AVM_TAILNET")
    assert config.load_settings().tailnet == ""


def test_tailnet_setting_wins_over_detection(monkeypatch):
    monkeypatch.setattr(core, "_tailnet", None)
    monkeypatch.setattr(core.settings, "tailnet", "tail1234.ts.net")
    monkeypatch.setattr(core, "_detect_tailnet", lambda: pytest.fail("detection must not run when AVM_TAILNET is set"))
    assert core.tailnet() == "tail1234.ts.net"


def test_tailnet_detected_once_then_warns_once_when_unknown(monkeypatch, caplog):
    calls = []
    monkeypatch.setattr(core.settings, "tailnet", "")
    monkeypatch.setattr(core, "_tailnet", None)
    monkeypatch.setattr(core, "_detect_tailnet", lambda: calls.append(1) or "tail9.ts.net")
    assert core.tailnet() == "tail9.ts.net" and core.tailnet() == "tail9.ts.net" and len(calls) == 1
    monkeypatch.setattr(core, "_tailnet", None)
    monkeypatch.setattr(core, "_detect_tailnet", lambda: "")
    with caplog.at_level(logging.WARNING, logger="avalon"):
        assert core.tailnet() == "" and core.tailnet() == ""
    assert sum("AVM_TAILNET" in m for m in caplog.messages) == 1


@pytest.mark.parametrize("run,want", [
    (lambda *a, **k: subprocess.CompletedProcess(a, 0, b'{"MagicDNSSuffix": "Tail42.ts.net."}', b""), "tail42.ts.net"),
    (lambda *a, **k: subprocess.CompletedProcess(a, 1, b"", b"not running"), ""),
    (lambda *a, **k: subprocess.CompletedProcess(a, 0, b"not json", b""), ""),
    (lambda *a, **k: subprocess.CompletedProcess(a, 0, b"[]", b""), ""),
])
def test_detect_tailnet(monkeypatch, run, want):
    monkeypatch.setattr(core.subprocess, "run", run)
    assert core._detect_tailnet() == want


@pytest.mark.parametrize("exc", [FileNotFoundError("tailscale"), subprocess.TimeoutExpired("tailscale", 3)])
def test_detect_tailnet_failures(monkeypatch, exc):
    def boom(*a, **k):
        raise exc
    monkeypatch.setattr(core.subprocess, "run", boom)
    assert core._detect_tailnet() == ""


# ------------------------------------------------------------------ F-21
def test_known_state_backfilled_from_the_rows_history(tmp_path):
    """Rows that were "unknown" when known_state was added got NULL, so their next failure never alerted."""
    path = str(tmp_path / "old.db")
    d = Database(path)
    h = d.create_host("h1")[0]
    t = 1_800_000_000.0
    ids = {}
    for name, state in (("was_ok", dict(state="unknown", last_ok=t, since=t - 100)),
                        ("was_failing", dict(state="unknown", last_ok=t - 100, since=t)),
                        ("never_ok", dict(state="unknown", since=t)),
                        ("pending", dict(state="unknown", last_ok=t, since=t - 100, pending=1)),
                        ("current", dict(state="ok", since=t)),
                        ("never_known", dict(state="unknown"))):
        ids[name] = d.check_add(h.id, MOUNT, f"/mnt/{name}", {"notify": "new"}, "test")["id"]
        d.check_update(ids[name], {**state, "known_state": None})
    d.close()
    d = Database(path)   # startup migration
    got = {n: d.check_get(i)["known_state"] for n, i in ids.items()}
    assert got == {"was_ok": "ok", "was_failing": "failing", "never_ok": "failing", "pending": "failing",
                   "current": "ok", "never_known": None}
    _, jobs = ck.evaluate(d, h, _sample(_res("/mnt/was_ok", False)), t + 500)
    assert [j["title"] for j in jobs] == ["alert · /mnt/was_ok"]   # its first failure now alerts
    d.check_update(ids["current"], {"known_state": "failing"})
    d.close()
    d = Database(path)   # idempotent: set rows (by evaluate or a previous start) are never touched again
    assert d.check_get(ids["current"])["known_state"] == "failing" and d.check_get(ids["never_known"])["known_state"] is None
    d.close()


def test_adopt_keeps_the_hubs_declared_lists(db):
    """The one report adoption uses lacks a kind the hub declares (no row yet, e.g. skipped by the legacy import):
    it must become a row instead of being rendered away as an empty list."""
    h = db.create_host("h1")[0]
    db.set_check_config(h.name, {UNITS: "user:train.service", MEM: "8", "AVM_TAGS": "gpu"})
    h = db.host_by_id(h.id)
    rev = ck.adopt(db, h, _sample(_res("/mnt/data", True), {"key": MEM, "src": "16", "kind": "memory", "ok": True}, version="1.8.1"))
    assert rev is not None
    rows = {(r["key"], r["target"]) for r in db.checks_list(h.id)}
    assert rows == {(UNITS, "user:train.service"), (MOUNT, "/mnt/data"), (MEM, "8")}   # one memory floor: the hub's
    cfg = db.host_by_id(h.id).check_config
    assert cfg[UNITS] == "user:train.service" and cfg[MOUNT] == "/mnt/data" and cfg[MEM] == "8"
    assert cfg[ck.MANAGED] == "1" and cfg["AVM_TAGS"] == "gpu"


# ------------------------------------------------------------------ G-02 / F-04 follow-up: stored tracked-session text
def _tracked(row, key="bg1"):
    return core.db.tracked_find(row.id, key)


def test_tracked_text_kept_on_a_normal_host(agent, host):
    """Control: on an unrestricted host a tracked session's last line is stored, and kept while a report lacks it
    (a session with no transcript yet), as before."""
    row = host[0]
    core.db.tracked_add(row.id, "bg1", {"session_id": "s1", "name": "w"}, "test")
    report_policy(agent, policy=FULL, agents=[{"key": "bg1", "session_id": "s1", "live": True, "last_said": "hello", "detail": "busy"}])
    report_policy(agent, policy=FULL, agents=[{"key": "bg1", "session_id": "s1", "live": True}])
    assert (_tracked(row)["last_said"], _tracked(row)["detail"]) == ("hello", "busy")


def test_redacted_row_clears_text_stored_before(agent, host):
    """A row the agent marks redacted (its session works in an AVM_TASK_LOG_OFF_DIRS dir, or snippets are off) clears
    the text the hub stored for it earlier - an absent key alone would keep it on show for good."""
    row = host[0]
    core.db.tracked_add(row.id, "bg1", {"session_id": "s1", "name": "w"}, "test")
    report_policy(agent, policy=FULL, agents=[{"key": "bg1", "session_id": "s1", "live": True, "last_said": "SECRET", "detail": "SECRET"}])
    report_policy(agent, policy=FULL, agents=[{"key": "bg1", "session_id": "s1", "live": True, "redacted": True}])
    t = _tracked(row)
    assert t["last_said"] is None and t["detail"] is None and t["name"] == "w"
    assert _flag(row) in (None, "")   # a per-session redaction does not restrict the host


def test_data_use_host_clears_all_stored_tracked_text(agent, host):
    """A restricted host: every tracked row's stored text is cleared on its next report, whatever the row says."""
    row = host[0]
    core.db.tracked_add(row.id, "bg1", {"session_id": "s1", "name": "w"}, "test")
    core.db.tracked_add(row.id, "bg2", {"session_id": "s2", "name": "gone"}, "test")
    report_policy(agent, policy=FULL, agents=[{"key": "bg1", "session_id": "s1", "live": True, "last_said": "SECRET-1"},
                                              {"key": "bg2", "session_id": "s2", "live": True, "detail": "SECRET-2"}])
    report_policy(agent, policy=SUMMARY, agents=[{"key": "bg1", "session_id": "s1", "live": True}])   # bg2 not listed
    assert "SECRET" not in json.dumps([_tracked(row, "bg1"), _tracked(row, "bg2")])
    assert "SECRET" not in json.dumps(agent.get("/api/v1/claude/tracked").json())
