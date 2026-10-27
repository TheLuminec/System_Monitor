"""Audit E-15/E-24/E-25/E-26/E-27: check state across agent restarts, partial settings edits, adoption, responder
claims and the responder guardrails (rolling cap, pending instead of dropped, fork fallback)."""
import threading
import time

import pytest

import manage
from avalon_monitor import checks as ck
from avalon_monitor import core
from avalon_monitor.db import Database
from conftest import WRITE

MOUNT, PROC = "AVM_WATCH_MOUNTS", "AVM_WATCH_PROCESSES"
POLICY = {"max_mode": "plan", "actions": True, "jobs": True, "transcript": "full", "snippets": True}   # agent 1.9+ (G-04)
CLAUDE = {"available": True, "features": ["agent_actions", "fork_session"], "agents": [], "policy": POLICY}


@pytest.fixture
def db(tmp_path):
    d = Database(str(tmp_path / "x.db"))
    yield d
    d.close()


@pytest.fixture
def h(db):
    return db.create_host("h1")[0]


def _res(target, ok, key=MOUNT):
    return {"name": target, "kind": ck.KIND_OF_KEY[key], "ok": ok, "key": key, "src": target, "detail": "ok" if ok else "NOT mounted"}


def _sample(*results, claude=CLAUDE):
    return {"host": {"hostname": "h1", "agent_version": "1.8.1"}, "checks": list(results), "claude": claude}


def _watch(db, h, target="/mnt/data", **fields):
    return db.check_add(h.id, MOUNT, target, {"notify": "new", **fields}, "test")


def _responders(db, h):
    return [j for j in db.job_list(h.id) if j["created_by"] == "check responder"]


# ------------------------------------------------------------------ E-15
def test_responder_fires_when_a_check_fails_after_an_agent_restart(db, h):
    """ok -> (restart: first report lacks the hub-added check) unknown -> failing must still alert."""
    c = _watch(db, h)
    t = 1_800_000_000.0
    ck.evaluate(db, h, _sample(_res("/mnt/data", True)), t)
    ck.evaluate(db, h, _sample(), t + 5)                       # restarted agent, hub config not applied yet
    assert db.check_get(c["id"])["state"] == "unknown"
    assert db.check_get(c["id"])["since"] == t                 # unknown does not restart the clock
    _, jobs = ck.evaluate(db, h, _sample(_res("/mnt/data", False)), t + 10)
    assert len(jobs) == 1 and len(_responders(db, h)) == 1
    row = db.check_get(c["id"])
    assert row["state"] == "failing" and row["since"] == t + 10 and row["open_job"] == jobs[0]["id"]


def test_unknown_gap_keeps_a_failure_and_its_open_alert(db, h):
    c = _watch(db, h)
    t = 1_800_000_000.0
    ck.evaluate(db, h, _sample(_res("/mnt/data", True)), t)
    ck.evaluate(db, h, _sample(_res("/mnt/data", False)), t + 5)
    ck.evaluate(db, h, _sample(), t + 10)
    ck.evaluate(db, h, _sample(_res("/mnt/data", False)), t + 15)
    assert len(_responders(db, h)) == 1                        # same failure: no second alert
    row = db.check_get(c["id"])
    assert row["since"] == t + 5 and row["open_job"]


def test_responder_fires_through_ingest_after_a_restart_gap(agent):
    """The same E-15 path end to end through POST /api/v1/ingest."""
    hrow = agent.host_row
    core.db.check_add(hrow.id, MOUNT, "/mnt/e15", {"notify": "new"}, "test")

    def push(*results):
        body = {"ts": time.time(), "host": {"hostname": hrow.name, "agent_version": "1.8.1"}, "checks": list(results), "claude": CLAUDE}
        assert agent.post("/api/v1/ingest", json=body).status_code == 200

    push(_res("/mnt/e15", True))
    push()
    push(_res("/mnt/e15", False))
    assert len(_responders(core.db, hrow)) == 1


# ------------------------------------------------------------------ E-26
def test_two_concurrent_reports_queue_one_responder(db, h):
    """Two ingests in flight for one host, really concurrent: two threads run evaluate on the same failing report, and
    a barrier right after each one reads the check rows makes both hold the pre-fire rows (open_job NULL) before
    either fires - the race. Only check_fire's atomic claim can stop the second job."""
    _watch(db, h)
    t = 1_800_000_000.0
    ck.evaluate(db, h, _sample(_res("/mnt/data", True)), t)
    gate, real = threading.Barrier(2, timeout=10), db.checks_list

    def listed(host_id=None):
        rows = real(host_id)
        gate.wait()   # both reports have read the rows; now both go on to fire
        return rows

    db.checks_list = listed
    out, errors = [], []

    def report():
        try:
            out.append(ck.evaluate(db, h, _sample(_res("/mnt/data", False)), t + 5)[1])
        except Exception as e:   # surfaced below: an assertion in a thread would be lost
            errors.append(e)

    threads = [threading.Thread(target=report) for _ in range(2)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(15)
    db.checks_list = real
    assert not errors and len(out) == 2
    assert sorted(len(j) for j in out) == [0, 1] and len(_responders(db, h)) == 1


# ------------------------------------------------------------------ E-27
def _cycle(db, h, t):
    """ok at t, failing at t+1: one ok -> failing transition."""
    ck.evaluate(db, h, _sample(_res("/mnt/data", True)), t)
    return ck.evaluate(db, h, _sample(_res("/mnt/data", False)), t + 1)[1]


def test_cap_is_a_rolling_24h_window_not_a_calendar_day(db, h):
    c = _watch(db, h, daily_cap=2, cooldown_min=5)
    midnight = time.mktime(time.strptime(time.strftime("%Y-%m-%d", time.localtime(1_800_000_000)), "%Y-%m-%d")) + 86400
    assert len(_cycle(db, h, midnight - 1200)) == 1
    assert len(_cycle(db, h, midnight - 600)) == 1
    assert _cycle(db, h, midnight + 600) == []                 # a new calendar day, but 2 alerts in the last 24 h
    assert db.check_get(c["id"])["pending"] == 1               # held, not dropped
    _, jobs = ck.evaluate(db, h, _sample(_res("/mnt/data", False)), midnight + 3600)
    assert jobs == []
    _, jobs = ck.evaluate(db, h, _sample(_res("/mnt/data", False)), midnight - 1200 + 86400 + 2)
    assert len(jobs) == 1                                      # the oldest alert left the window: fires now


def test_cannot_prompt_keeps_the_alert_pending_until_it_can(db, h):
    c = _watch(db, h)
    t = 1_800_000_000.0
    off = {**CLAUDE, "available": False}
    ck.evaluate(db, h, _sample(_res("/mnt/data", True), claude=off), t)
    _, jobs = ck.evaluate(db, h, _sample(_res("/mnt/data", False), claude=off), t + 5)
    assert jobs == [] and db.check_get(c["id"])["pending"] == 1
    _, jobs = ck.evaluate(db, h, _sample(_res("/mnt/data", False)), t + 10)
    assert len(jobs) == 1 and db.check_get(c["id"])["pending"] == 0


def test_pending_hold_is_not_rebroadcast_every_report(db, h):
    _watch(db, h)
    t = 1_800_000_000.0
    off = {**CLAUDE, "available": False}
    ck.evaluate(db, h, _sample(_res("/mnt/data", True), claude=off), t)
    ck.evaluate(db, h, _sample(_res("/mnt/data", False), claude=off), t + 5)
    changed, _ = ck.evaluate(db, h, _sample(_res("/mnt/data", False), claude=off), t + 10)
    assert changed == []


def _tracked(db, h, status):
    tr = db.tracked_add(h.id, "agent-1", {"session_id": "sess-1", "cwd": "/srv/proj", "name": "proj"}, "test")
    agents = [{"key": "agent-1", "session_id": "sess-1", "live": True, "status": status}]
    return tr, agents


def test_live_idle_target_without_fork_gets_a_fresh_session(db, h):
    tr, agents = _tracked(db, h, "idle")
    _watch(db, h, notify=f"tracked:{tr['id']}")
    t = 1_800_000_000.0
    no_fork = {**CLAUDE, "features": ["agent_actions"], "agents": agents}
    ck.evaluate(db, h, _sample(_res("/mnt/data", True), claude=no_fork), t)
    _, jobs = ck.evaluate(db, h, _sample(_res("/mnt/data", False), claude=no_fork), t + 5)
    assert len(jobs) == 1
    j = jobs[0]
    assert j["resume_session"] is None and not j["fork_session"] and j["cwd"] == "/srv/proj" and j["mode"] == "plan"


def test_live_idle_target_with_fork_gets_a_fork(db, h):
    tr, agents = _tracked(db, h, "idle")
    _watch(db, h, notify=f"tracked:{tr['id']}")
    t = 1_800_000_000.0
    cl = {**CLAUDE, "agents": agents}
    ck.evaluate(db, h, _sample(_res("/mnt/data", True), claude=cl), t)
    _, jobs = ck.evaluate(db, h, _sample(_res("/mnt/data", False), claude=cl), t + 5)
    assert jobs[0]["resume_session"] == "sess-1" and jobs[0]["fork_session"] == 1


def test_busy_target_is_held_then_fires_when_idle(db, h):
    tr, agents = _tracked(db, h, "busy")
    c = _watch(db, h, notify=f"tracked:{tr['id']}")
    t = 1_800_000_000.0
    ck.evaluate(db, h, _sample(_res("/mnt/data", True), claude={**CLAUDE, "agents": agents}), t)
    _, jobs = ck.evaluate(db, h, _sample(_res("/mnt/data", False), claude={**CLAUDE, "agents": agents}), t + 5)
    assert jobs == [] and db.check_get(c["id"])["pending"] == 1
    idle = [{**agents[0], "status": "idle"}]
    _, jobs = ck.evaluate(db, h, _sample(_res("/mnt/data", False), claude={**CLAUDE, "agents": idle}), t + 10)
    assert len(jobs) == 1 and jobs[0]["fork_session"] == 1


# ------------------------------------------------------------------ E-25
def test_adopt_waits_for_a_non_empty_check_list(db, h):
    """collect_checks raising on the agent sends checks=[]: that must not make the host managed with no rows."""
    assert ck.adopt(db, h, _sample()) is None
    h2 = db.host_by_id(h.id)
    assert not (h2.check_config or {}).get(ck.MANAGED) and db.checks_list(h.id) == []
    rev = ck.adopt(db, h2, _sample(_res("/mnt/data", True), _res("nginx", True, PROC)))
    assert rev is not None
    h3 = db.host_by_id(h.id)
    assert h3.check_config[ck.MANAGED] == "1"
    assert {(r["key"], r["target"]) for r in db.checks_list(h.id)} == {(MOUNT, "/mnt/data"), (PROC, "nginx")}
    assert h3.check_config[MOUNT] == "/mnt/data" and h3.check_config[PROC] == "nginx"


# ------------------------------------------------------------------ E-24
def _managed_host(host):
    row, _ = host
    core.db.set_check_config(row.name, {ck.MANAGED: "1", "AVM_PROCESSES": "5", "AVM_TAGS": "gpu"})
    a = core.db.check_add(row.id, PROC, "trainer", {"label": "R4 chain", "notify": "new"}, "test")
    core.db.check_add(row.id, PROC, "old", {}, "test")
    m = core.db.check_add(row.id, MOUNT, "/mnt/data", {"label": "data"}, "test")
    ck.sync_config(core.db, row.id)
    return row, a, m


def test_put_with_a_partial_body_keeps_other_kinds_and_settings(tunnel, host):
    row, a, m = _managed_host(host)
    r = tunnel.put(f"/api/v1/hosts/{row.name}/checks", headers=WRITE, json={"AVM_TAGS": "gpu,lab"})
    assert r.status_code == 200, r.text
    rows = {(x["key"], x["target"]): x for x in core.db.checks_list(row.id)}
    assert set(rows) == {(PROC, "trainer"), (PROC, "old"), (MOUNT, "/mnt/data")}
    assert rows[(PROC, "trainer")]["label"] == "R4 chain" and rows[(PROC, "trainer")]["notify"] == "new"
    cfg = core.db.host_by_id(row.id).check_config
    assert cfg["AVM_TAGS"] == "gpu,lab" and cfg["AVM_PROCESSES"] == "5" and cfg[ck.MANAGED] == "1"
    assert cfg[PROC] == "old,trainer" and cfg[MOUNT] == "/mnt/data"


def test_put_reconciles_only_the_kinds_in_the_body(tunnel, host):
    row, a, m = _managed_host(host)
    r = tunnel.put(f"/api/v1/hosts/{row.name}/checks", headers=WRITE, json={PROC: "trainer, new"})
    assert r.status_code == 200, r.text
    rows = {(x["key"], x["target"]): x for x in core.db.checks_list(row.id)}
    assert set(rows) == {(PROC, "trainer"), (PROC, "new"), (MOUNT, "/mnt/data")}
    assert rows[(PROC, "trainer")]["id"] == a["id"] and rows[(PROC, "trainer")]["label"] == "R4 chain"
    assert rows[(MOUNT, "/mnt/data")]["label"] == "data"
    cfg = core.db.host_by_id(row.id).check_config
    assert cfg["AVM_TAGS"] == "gpu" and cfg[ck.MANAGED] == "1"


def test_manage_checks_set_goes_through_the_rows(host, capsys):
    row, a, m = _managed_host(host)
    assert manage.main(["checks", row.name, "--set", f"{MOUNT}=/mnt/data,/mnt/scratch"]) == 0
    rows = {(x["key"], x["target"]) for x in core.db.checks_list(row.id)}
    assert rows == {(PROC, "trainer"), (PROC, "old"), (MOUNT, "/mnt/data"), (MOUNT, "/mnt/scratch")}
    assert ck.sync_config(core.db, row.id) is None             # the next re-render doesn't undo it
    cfg = core.db.host_by_id(row.id).check_config
    assert cfg[MOUNT] == "/mnt/data,/mnt/scratch" and cfg[ck.MANAGED] == "1"


def test_manage_checks_clear_keeps_the_host_managed(host, capsys):
    row, a, m = _managed_host(host)
    assert manage.main(["checks", row.name, "--clear"]) == 0
    assert core.db.checks_list(row.id) == []
    cfg = core.db.host_by_id(row.id).check_config
    assert cfg[ck.MANAGED] == "1" and "AVM_TAGS" not in cfg
    assert all(cfg[k] == "" for k in ck.CHECK_KEYS)            # managed: every list sent empty, no agent.conf fallback
