"""Audit E-20/E-21/E-30: rollups with late samples, beats reviving lost tasks, one-time legacy check import."""
import math
import sqlite3

import pytest

from avalon_monitor.db import Database


@pytest.fixture
def db(tmp_path):
    d = Database(str(tmp_path / "x.db"))
    yield d
    d.close()


# ------------------------------------------------------------------ E-20
def _insert(db, host_id, ts, cpu=10.0):
    db._conn.execute("INSERT OR REPLACE INTO samples(host_id, ts, cpu) VALUES (?,?,?)", (host_id, ts, cpu))


@pytest.mark.parametrize("skew", [0, 30, 90, 119])
def test_rollup_keeps_late_samples_from_a_host_whose_clock_is_behind(db, skew):
    """Samples every 5 s from a host `skew` s behind (ts is kept: within the 120 s clamp); the rollup runs 30 s into
    every minute. Every completed minute must hold all 12 samples."""
    hid = db.create_host("behind")[0].id
    t0 = 1_800_000_000.0 - 1_800_000_000.0 % 60
    end = t0 + 20 * 60
    arrive = t0
    while arrive < end:
        _insert(db, hid, arrive - skew)
        arrive += 5
        if arrive % 60 == 30:
            db.rollup(now=arrive)
    rows = {r["ts"]: r["n"] for r in db._conn.execute("SELECT ts, n FROM samples_1m WHERE host_id=?", (hid,))}
    first = math.ceil((t0 - skew) / 60) * 60                  # first minute the host fully sampled
    complete = range(int(first), int(end) - 300 + 1, 60)        # ... up to the last one a later pass has rolled up
    assert complete and all(rows.get(float(m)) == 12 for m in complete), {m: rows.get(float(m)) for m in complete}


def test_rollup_is_idempotent_on_the_trailing_window(db):
    hid = db.create_host("h")[0].id
    t0 = 1_800_000_000.0 - 1_800_000_000.0 % 60
    for i in range(12):
        _insert(db, hid, t0 + i * 5, cpu=float(i))
    db.rollup(now=t0 + 90)
    db.rollup(now=t0 + 150)
    r = db._conn.execute("SELECT n, cpu FROM samples_1m WHERE host_id=? AND ts=?", (hid, t0)).fetchone()
    assert r["n"] == 12 and r["cpu"] == pytest.approx(5.5)


# ------------------------------------------------------------------ E-21
@pytest.mark.parametrize("gone", ["stalled", "lost"])
def test_beat_revives_a_stalled_or_lost_task(db, gone):
    hid = db.create_host("h")[0].id
    t = db.task_create(hid, "seed 3 training", "run", 30, "python train.py", "/srv", 1, "u", None)
    if gone == "lost":
        db.task_mark_lost(hid)
    else:
        db.task_mark_stalled(now=t["heartbeat"] + 1000)
    assert db.task_get(t["id"])["status"] == gone
    db.task_ack([t["id"]], by="me")
    row = db.task_update(t["id"], hid, log_lines=["step 10"])
    assert row["status"] == "running" and row["acked"] is None


def test_beat_does_not_revive_a_finished_task(db):
    hid = db.create_host("h")[0].id
    t = db.task_create(hid, "x", "run", 30, None, None, None, None, None)
    db.task_update(t["id"], hid, status="failed", rc=1)
    assert db.task_update(t["id"], hid)["status"] == "failed"


# ------------------------------------------------------------------ E-30
def _legacy_db(path, cfg):
    """A database from before host_checks: a host with AVM_WATCH_* settings and no check rows table."""
    d = Database(str(path))
    hid = d.create_host("old")[0].id
    d.set_check_config("old", cfg)
    d._conn.execute("DROP TABLE host_checks")
    d._conn.execute("DELETE FROM meta WHERE key='legacy_checks_imported'")
    d.close()
    return hid


def test_legacy_checks_are_imported_once(tmp_path):
    path = tmp_path / "legacy.db"
    hid = _legacy_db(path, {"AVM_WATCH_PROCESSES": "nginx, sshd", "AVM_MEM_AVAILABLE_WARN_GB": "8"})
    d = Database(str(path))
    assert {(r["key"], r["target"]) for r in d.checks_list(hid)} == {
        ("AVM_WATCH_PROCESSES", "nginx"), ("AVM_WATCH_PROCESSES", "sshd"), ("AVM_MEM_AVAILABLE_WARN_GB", "8")}
    for r in d.checks_list(hid):            # every check deleted, crash before the settings were re-rendered
        d.check_delete(r["id"])
    d.close()
    d = Database(str(path))
    try:
        assert d.checks_list(hid) == []     # not re-imported from the stale settings
    finally:
        d.close()


def test_existing_check_table_counts_as_imported(tmp_path):
    """An existing DB (host_checks already there, no flag yet) never re-imports a host that has zero rows."""
    path = tmp_path / "x.db"
    d = Database(str(path))
    hid = d.create_host("h")[0].id
    d.set_check_config("h", {"AVM_WATCH_PROCESSES": "stale"})
    d._conn.execute("DELETE FROM meta WHERE key='legacy_checks_imported'")
    d.close()
    d = Database(str(path))
    try:
        assert d.checks_list(hid) == []
        assert d._conn.execute("SELECT value FROM meta WHERE key='legacy_checks_imported'").fetchone()["value"] == "1"
    finally:
        d.close()


def test_migration_on_a_production_shaped_db_is_idempotent(tmp_path):
    """host_checks from before known_state/fire_log, no import flag: opening it (twice) adds the columns, keeps rows."""
    path = tmp_path / "prod.db"
    d = Database(str(path))
    hid = d.create_host("avalon")[0].id
    d.set_check_config("avalon", {"_checks_managed": "1", "AVM_WATCH_MOUNTS": "/mnt/data"})
    c = d.check_add(hid, "AVM_WATCH_MOUNTS", "/mnt/data", {"label": "data", "notify": "new"}, "test")
    d.check_update(c["id"], {"state": "failing", "fires_day": "2026-10-25", "fires_n": 2, "last_fired": 1.0})
    d._conn.execute("ALTER TABLE host_checks DROP COLUMN known_state")
    d._conn.execute("ALTER TABLE host_checks DROP COLUMN fire_log")
    d._conn.execute("DELETE FROM meta WHERE key='legacy_checks_imported'")
    d.close()
    for _ in range(2):
        d = Database(str(path))
        try:
            cols = {r["name"] for r in d._conn.execute("PRAGMA table_info(host_checks)")}
            assert {"known_state", "fire_log"} <= cols
            rows = d.checks_list(hid)
            assert len(rows) == 1 and rows[0]["label"] == "data" and rows[0]["fires_n"] == 2
        finally:
            d.close()


def test_legacy_import_is_one_transaction(tmp_path, monkeypatch):
    path = tmp_path / "legacy.db"
    hid = _legacy_db(path, {"AVM_WATCH_PROCESSES": "a,b,c"})
    import avalon_monitor.checks as ck
    monkeypatch.setattr(ck, "CHECK_KEYS", ["AVM_WATCH_PROCESSES", []])   # the second key blows up mid-import
    with pytest.raises(TypeError):
        Database(str(path))
    monkeypatch.undo()
    raw = sqlite3.connect(str(path))
    try:   # nothing half-imported, still to do: the next start imports everything
        assert raw.execute("SELECT COUNT(*) FROM host_checks").fetchone()[0] == 0
        assert raw.execute("SELECT value FROM meta WHERE key='legacy_checks_imported'").fetchone()[0] == "0"
    finally:
        raw.close()
    d = Database(str(path))
    try:
        assert {r["target"] for r in d.checks_list(hid)} == {"a", "b", "c"}
        assert d._conn.execute("SELECT value FROM meta WHERE key='legacy_checks_imported'").fetchone()["value"] == "1"
    finally:
        d.close()
