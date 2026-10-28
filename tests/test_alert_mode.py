"""Alert mode: a check's responder prompt runs in the check's alert_mode (NULL = plan), which only an operator route can
raise; the host's reported AVM_MAX_JOB_MODE still caps it (held with the reason, never sent lower); the agent token
(avm check add/set) can't raise it, doesn't reset it, and can't retarget a raised check's responder."""
import time
from pathlib import Path

import pytest

from avalon_monitor import checks as ck
from avalon_monitor import core
from avalon_monitor.db import Database
from conftest import WRITE, report_policy

STATIC = Path(__file__).resolve().parents[1] / "server" / "avalon_monitor" / "static"
MOUNT = "AVM_WATCH_MOUNTS"
POLICY = {"max_mode": "plan", "actions": True, "jobs": True, "transcript": "full", "snippets": True}


def _claude(max_mode="plan"):
    return {"available": True, "features": ["agent_actions", "fork_session"], "agents": [], "policy": {**POLICY, "max_mode": max_mode}}


@pytest.fixture
def db(tmp_path):
    d = Database(str(tmp_path / "x.db"))
    yield d
    d.close()


@pytest.fixture
def h(db):
    return db.create_host("h1")[0]


def _res(target, ok):
    return {"name": target, "kind": "mount", "ok": ok, "key": MOUNT, "src": target, "detail": "ok" if ok else "NOT mounted"}


def _sample(*results, max_mode="plan"):
    return {"host": {"hostname": "h1", "agent_version": "1.10.2"}, "checks": list(results), "claude": _claude(max_mode)}


def _fail(db, h, max_mode="plan", target="/mnt/data", t=1_800_000_000.0):
    """ok then failing: one ok -> failing transition, the responder's trigger. Returns the jobs it created."""
    ck.evaluate(db, h, _sample(_res(target, True), max_mode=max_mode), t)
    return ck.evaluate(db, h, _sample(_res(target, False), max_mode=max_mode), t + 5)[1]


def _responders(d, hid):
    return [j for j in d.job_list(hid) if j["created_by"] == "check responder"]


# ------------------------------------------------------------------ the responder
def test_default_alert_mode_is_plan(db, h):
    """Alert mode: a check with no alert_mode (every existing row, every avm-added one) fires a plan prompt, even on
    a host that would allow more; its JSON says plan."""
    c = db.check_add(h.id, MOUNT, "/mnt/data", {"notify": "new"}, "test")
    assert c["alert_mode"] is None and ck.public(c)["alert_mode"] == "plan"
    jobs = _fail(db, h, max_mode="full")
    assert len(jobs) == 1 and jobs[0]["mode"] == "plan"


def test_raised_alert_mode_fires_in_that_mode(db, h):
    """Alert mode: a check raised to acceptEdits on a host whose cap allows it fires an acceptEdits prompt (before: always plan)."""
    c = db.check_add(h.id, MOUNT, "/mnt/data", {"notify": "new", "alert_mode": "acceptEdits"}, "test")
    jobs = _fail(db, h, max_mode="acceptEdits")
    assert len(jobs) == 1 and jobs[0]["mode"] == "acceptEdits"
    assert db.check_get(c["id"])["open_job"] == jobs[0]["id"]


def test_host_cap_below_alert_mode_holds_with_reason(db, h):
    """Alert mode: a host whose reported AVM_MAX_JOB_MODE is below the check's mode gets no job at all (not a plan one):
    the alert is held with a reason naming the mode and the cap, and fires once the host allows it."""
    c = db.check_add(h.id, MOUNT, "/mnt/data", {"notify": "new", "alert_mode": "full"}, "test")
    assert _fail(db, h, max_mode="acceptEdits") == []
    assert _responders(db, h.id) == []
    row = db.check_get(c["id"])
    assert row["pending"] == 1 and row["open_job"] is None
    assert "alert mode full" in row["hold_reason"] and "allows prompts up to acceptEdits" in row["hold_reason"]
    _, jobs = ck.evaluate(db, h, _sample(_res("/mnt/data", False), max_mode="full"), 1_800_000_100.0)
    assert len(jobs) == 1 and jobs[0]["mode"] == "full"
    assert db.check_get(c["id"])["hold_reason"] is None


def test_unknown_stored_mode_is_held_not_guessed(db, h):
    """Alert mode: a value no agent knows (a hand-edited row) holds the alert rather than sending it in some mode."""
    c = db.check_add(h.id, MOUNT, "/mnt/data", {"notify": "new", "alert_mode": "bypassPermissions"}, "test")
    assert _fail(db, h, max_mode="full") == []
    assert "not one of plan, acceptEdits, auto, full" in db.check_get(c["id"])["hold_reason"]


@pytest.mark.parametrize("cap,mode,fires", [("auto", "auto", True), ("auto", "full", False), ("acceptEdits", "auto", False),
                                            ("full", "auto", True), ("auto", "acceptEdits", True)])
def test_auto_ranks_between_accept_edits_and_full_for_alerts(db, h, cap, mode, fires):
    """Alert mode: plan < acceptEdits < auto < full - a host capped at auto takes an auto alert and holds a full one; a
    host capped at acceptEdits holds an auto one (never sent lower)."""
    c = db.check_add(h.id, MOUNT, "/mnt/data", {"notify": "new", "alert_mode": mode}, "test")
    jobs = _fail(db, h, max_mode=cap)
    assert [j["mode"] for j in jobs] == ([mode] if fires else [])
    if not fires:
        assert f"alert mode {mode}" in db.check_get(c["id"])["hold_reason"] and f"up to {cap}" in db.check_get(c["id"])["hold_reason"]


def test_auto_ranks_between_accept_edits_and_full_for_prompts(tunnel, agent):
    """Alert mode: the dashboard-prompt check (host_policy_refusal) ranks auto the same way: a host reporting max_mode
    auto accepts an auto prompt and refuses full (409); one reporting acceptEdits refuses auto."""
    name = agent.host_row.name
    job = lambda mode, **x: tunnel.post("/api/v1/claude/jobs", headers=WRITE, json={"host": name, "prompt": "x", "mode": mode, **x})  # noqa: E731
    report_policy(agent, policy={**POLICY, "max_mode": "auto"})
    r = job("auto")
    assert r.status_code == 200 and r.json()["job"]["mode"] == "auto", r.text
    r = job("full", confirm_full=True)
    assert r.status_code == 409 and "up to auto" in r.json()["detail"]
    report_policy(agent, policy={**POLICY, "max_mode": "acceptEdits"})
    r = job("auto")
    assert r.status_code == 409 and "up to acceptEdits" in r.json()["detail"]


def test_operator_sets_auto_alert_mode(tunnel, agent):
    """Alert mode: auto is a valid alert mode (no confirm needed, unlike full)."""
    c = _agent_add(agent, "/mnt/am9")
    r = tunnel.patch(f"/api/v1/checklist/{c['id']}", json={"alert_mode": "auto"}, headers=WRITE)
    assert r.status_code == 200 and r.json()["check"]["alert_mode"] == "auto"


def test_migration_adds_alert_mode_column(tmp_path):
    """Alert mode: a database from before the column gets it on open; old rows read as plan."""
    path = tmp_path / "old.db"
    d = Database(str(path))
    hid = d.create_host("avalon")[0].id
    d.check_add(hid, MOUNT, "/mnt/data", {"notify": "new"}, "test")
    d._conn.execute("ALTER TABLE host_checks DROP COLUMN alert_mode")
    d.close()
    d = Database(str(path))
    try:
        row = d.checks_list(hid)[0]
        assert "alert_mode" in row and ck.public(row)["alert_mode"] == "plan"
    finally:
        d.close()


# ------------------------------------------------------------------ the API
def _agent_add(agent, target="/mnt/am", **extra):
    r = agent.post("/api/v1/agent/checks", json={"kind": "mount", "target": target, "notify": "new", **extra})
    assert r.status_code == 200, r.text
    return r.json()["check"]


def test_operator_sets_alert_mode_and_ingest_fires_in_it(tunnel, agent):
    """Alert mode: PATCH /api/v1/checklist/<id> {"alert_mode": "acceptEdits"} from an operator is stored and shown in
    the checklist and in avm's list, and the alert queued through ingest runs in that mode."""
    hrow = agent.host_row
    c = _agent_add(agent, "/mnt/am1")
    assert c["alert_mode"] == "plan"
    r = tunnel.patch(f"/api/v1/checklist/{c['id']}", json={"alert_mode": "acceptEdits"}, headers=WRITE)
    assert r.status_code == 200, r.text
    assert r.json()["check"]["alert_mode"] == "acceptEdits"
    shown = tunnel.get(f"/api/v1/hosts/{hrow.name}/checklist").json()["checks"]
    assert [x["alert_mode"] for x in shown if x["id"] == c["id"]] == ["acceptEdits"]
    assert [x["alert_mode"] for x in agent.get("/api/v1/agent/checks").json()["checks"] if x["id"] == c["id"]] == ["acceptEdits"]

    def push(*results):
        body = {"ts": time.time(), "host": {"hostname": hrow.name, "agent_version": "1.10.2"}, "checks": list(results),
                "claude": _claude("acceptEdits")}
        assert agent.post("/api/v1/ingest", json=body).status_code == 200

    push(_res("/mnt/am1", True))
    push(_res("/mnt/am1", False))
    jobs = _responders(core.db, hrow.id)
    assert len(jobs) == 1 and jobs[0]["mode"] == "acceptEdits"


def test_operator_add_with_alert_mode_and_back_to_plan(tunnel, host):
    """Alert mode: the dashboard's add route takes it too; plan is stored as NULL; a save without it keeps it."""
    row = host[0]
    r = tunnel.post(f"/api/v1/hosts/{row.name}/checklist", json={"kind": "mount", "target": "/mnt/am2", "notify": "new",
                                                                "alert_mode": "acceptEdits"}, headers=WRITE)
    assert r.status_code == 200, r.text
    cid = r.json()["check"]["id"]
    assert core.db.check_get(cid)["alert_mode"] == "acceptEdits"
    assert tunnel.patch(f"/api/v1/checklist/{cid}", json={"label": "x"}, headers=WRITE).json()["check"]["alert_mode"] == "acceptEdits"
    assert tunnel.patch(f"/api/v1/checklist/{cid}", json={"alert_mode": "plan"}, headers=WRITE).status_code == 200
    assert core.db.check_get(cid)["alert_mode"] is None


def test_full_needs_confirm(tunnel, agent):
    """Alert mode: full needs confirm_full, as a dashboard prompt does."""
    c = _agent_add(agent, "/mnt/am3")
    assert tunnel.patch(f"/api/v1/checklist/{c['id']}", json={"alert_mode": "full"}, headers=WRITE).status_code == 422
    assert core.db.check_get(c["id"])["alert_mode"] is None
    r = tunnel.patch(f"/api/v1/checklist/{c['id']}", json={"alert_mode": "full", "confirm_full": True}, headers=WRITE)
    assert r.status_code == 200 and core.db.check_get(c["id"])["alert_mode"] == "full"


@pytest.mark.parametrize("mode", ["bypassPermissions", "AUTO", "PLAN", "", "plan;full"])
def test_invalid_alert_mode_is_422(tunnel, agent, mode):
    """Alert mode: only the modes agents know (routes_ops.JOB_MODES); anything else is 422 and nothing is stored."""
    c = _agent_add(agent, "/mnt/am4")
    r = tunnel.patch(f"/api/v1/checklist/{c['id']}", json={"alert_mode": mode}, headers=WRITE)
    assert r.status_code == 422, r.text
    r = tunnel.post(f"/api/v1/hosts/{agent.host_row.name}/checklist", json={"kind": "mount", "target": "/mnt/am4b", "alert_mode": mode},
                    headers=WRITE)
    assert r.status_code == 422, r.text
    assert core.db.check_get(c["id"])["alert_mode"] is None


def test_viewer_cannot_set_alert_mode(client, agent):
    """Alert mode: a read-only viewer (loopback, no Access login) can't change it."""
    c = _agent_add(agent, "/mnt/am5")
    r = client.patch(f"/api/v1/checklist/{c['id']}", json={"alert_mode": "acceptEdits"}, headers=WRITE)
    assert r.status_code == 403
    assert core.db.check_get(c["id"])["alert_mode"] is None


def test_agent_route_cannot_raise_alert_mode(agent):
    """Alert mode: avm check add/set (agent token) can't raise it above plan: 403, nothing stored."""
    r = agent.post("/api/v1/agent/checks", json={"kind": "mount", "target": "/mnt/am6", "alert_mode": "acceptEdits"})
    assert r.status_code == 403
    assert not [x for x in core.db.checks_list(agent.host_row.id) if x["target"] == "/mnt/am6"]
    c = _agent_add(agent, "/mnt/am6")
    assert agent.patch(f"/api/v1/agent/checks/{c['id']}", json={"alert_mode": "full", "confirm_full": True}).status_code == 403
    assert core.db.check_get(c["id"])["alert_mode"] is None


def test_agent_update_keeps_operator_mode(tunnel, agent):
    """Alert mode: an agent-side set or re-add that doesn't send alert_mode leaves an operator's mode as it is; only
    an explicit alert_mode=plan lowers it."""
    c = _agent_add(agent, "/mnt/am7")
    assert tunnel.patch(f"/api/v1/checklist/{c['id']}", json={"alert_mode": "acceptEdits"}, headers=WRITE).status_code == 200
    r = agent.patch(f"/api/v1/agent/checks/{c['id']}", json={"label": "nightly log", "cooldown_min": 60})
    assert r.status_code == 200 and r.json()["check"]["alert_mode"] == "acceptEdits"
    r = agent.post("/api/v1/agent/checks", json={"kind": "mount", "target": "/mnt/am7", "label": "again"})
    assert r.status_code == 200 and r.json()["check"]["alert_mode"] == "acceptEdits"
    assert core.db.check_get(c["id"])["label"] == "again"
    r = agent.patch(f"/api/v1/agent/checks/{c['id']}", json={"alert_mode": "plan"})
    assert r.status_code == 200 and r.json()["check"]["alert_mode"] == "plan"
    assert core.db.check_get(c["id"])["alert_mode"] is None


def test_agent_cannot_retarget_a_raised_check(tunnel, agent):
    """Alert mode: on a check an operator raised, the agent token can't change who is woken, where, or what is watched
    (a plan-mode session could otherwise point the raised alert at itself); unchanged values and other keys are fine."""
    c = _agent_add(agent, "/mnt/am8", notify_cwd="/srv/a")
    assert tunnel.patch(f"/api/v1/checklist/{c['id']}", json={"alert_mode": "acceptEdits"}, headers=WRITE).status_code == 200
    for body in ({"notify": "none"}, {"notify_cwd": "/home/x"}, {"target": "/mnt/other"},
                 {"notify": "self", "session": {"key": "k1", "session_id": "s1", "kind": "interactive", "cwd": "/x"}}):
        assert agent.patch(f"/api/v1/agent/checks/{c['id']}", json=body).status_code == 403, body
    assert agent.post("/api/v1/agent/checks", json={"kind": "mount", "target": "/mnt/am8", "notify_cwd": "/home/x"}).status_code == 403
    row = core.db.check_get(c["id"])
    assert (row["target"], row["notify"], row["notify_cwd"], row["alert_mode"]) == ("/mnt/am8", "new", "/srv/a", "acceptEdits")
    assert agent.patch(f"/api/v1/agent/checks/{c['id']}", json={"notify": "new", "notify_cwd": "/srv/a", "label": "y"}).status_code == 200


def test_agent_cannot_replace_a_raised_memory_floor(tunnel, agent):
    """Alert mode: avm check add memory <other> replaces the host's one memory floor; on a raised one that is a retarget."""
    r = tunnel.post(f"/api/v1/hosts/{agent.host_row.name}/checklist", json={"kind": "memory", "target": "8", "notify": "new",
                                                                           "alert_mode": "acceptEdits"}, headers=WRITE)
    assert r.status_code == 200, r.text
    cid = r.json()["check"]["id"]
    assert agent.post("/api/v1/agent/checks", json={"kind": "memory", "target": "4"}).status_code == 403
    assert core.db.check_get(cid)["target"] == "8"


# ------------------------------------------------------------------ the dashboard
def test_alert_mode_select_is_guarded_and_sent():
    """Alert mode: the check form has an "Alert mode" select (plan by default), wrapped in guard() so a read-only
    viewer sees it disabled, and its value goes in the saved body."""
    js = (STATIC / "app.js").read_text()
    at = js.index("const alertMode = ")
    assert js[at:at + 80].startswith('const alertMode = guard(h("select"'), js[at:at + 80]
    assert 'alertMode.value = c?.alert_mode || "plan"' in js
    assert "alert_mode: alertMode.value" in js
    assert 'h("label", {}, "Alert mode ", alertMode)' in js
    assert "...Object.keys(MODE_RANK).map((m) => h(\"option\"" in js   # plan, acceptEdits, auto, full (core.js order)


def test_ui_mode_order_matches_the_hub():
    """Alert mode: core.js MODE_RANK and the prompt form's MODE_LABEL list the hub's JOB_MODES, in its order."""
    import re
    from avalon_monitor.routes_ops import JOB_MODES, MODE_RANK
    assert JOB_MODES == ("plan", "acceptEdits", "auto", "full") and list(MODE_RANK) == list(JOB_MODES)
    core_js = (STATIC / "core.js").read_text()
    rank = re.search(r"export const MODE_RANK = \{([^}]*)\}", core_js).group(1)
    assert [k.split(":")[0].strip() for k in rank.split(",")] == list(JOB_MODES)
    assert [int(k.split(":")[1]) for k in rank.split(",")] == [0, 1, 2, 3]
    label = re.search(r"const MODE_LABEL = \{([^}]*)\}", (STATIC / "views.js").read_text()).group(1)
    assert [k.split(":")[0].strip() for k in label.split(",")] == list(JOB_MODES)
