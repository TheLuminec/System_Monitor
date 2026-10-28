"""New check: a check added while its target is already failing has no ok -> failing edge, so it never alerted (the
owner's 2026-10-28 test check: a file check on a file already older than its max age stayed red and silent). It now
alerts once it is still failing NEW_CHECK_GRACE_SEC after creation, then follows the usual rule."""
from avalon_monitor import checks as ck
from test_alert_mode import MOUNT, _res, _responders, _sample, db, h  # noqa: F401  (fixtures)


def _new(db, h):
    return db.check_add(h.id, MOUNT, "/mnt/data", {"notify": "new"}, "test")


def test_check_failing_from_the_start_alerts_after_the_grace(db, h):
    """New check: failing on every report since it was added -> no alert inside the grace, one alert after it."""
    c = _new(db, h)
    t0 = c["created"]
    assert ck.evaluate(db, h, _sample(_res("/mnt/data", False)), t0 + 5)[1] == []
    assert ck.evaluate(db, h, _sample(_res("/mnt/data", False)), t0 + ck.NEW_CHECK_GRACE_SEC - 1)[1] == []
    jobs = ck.evaluate(db, h, _sample(_res("/mnt/data", False)), t0 + ck.NEW_CHECK_GRACE_SEC + 1)[1]
    assert len(jobs) == 1 and db.check_get(c["id"])["open_job"] == jobs[0]["id"]


def test_it_alerts_once_not_on_every_report(db, h):
    """New check: after that one alert it doesn't repeat while still failing (open alert, then last_fired is set)."""
    c = _new(db, h)
    t = c["created"] + ck.NEW_CHECK_GRACE_SEC + 1
    ck.evaluate(db, h, _sample(_res("/mnt/data", False)), t)
    db.check_update(c["id"], {"open_job": None})   # the alert's job finished; the check is still failing
    for i in range(1, 4):
        assert ck.evaluate(db, h, _sample(_res("/mnt/data", False)), t + 3600 * i)[1] == []
    assert len(_responders(db, h.id)) == 1


def test_something_coming_up_inside_the_grace_never_alerts(db, h):
    """New check: added just before its process starts (failing, then ok inside the grace) -> no alert, as before."""
    c = _new(db, h)
    t0 = c["created"]
    ck.evaluate(db, h, _sample(_res("/mnt/data", False)), t0 + 5)
    ck.evaluate(db, h, _sample(_res("/mnt/data", True)), t0 + 30)
    assert ck.evaluate(db, h, _sample(_res("/mnt/data", True)), t0 + ck.NEW_CHECK_GRACE_SEC + 60)[1] == []
    assert _responders(db, h.id) == []


def test_a_check_that_was_ok_keeps_the_usual_rule(db, h):
    """New check: one that has been ok fires on ok -> failing right away (no grace), exactly as before."""
    c = _new(db, h)
    t0 = c["created"]
    ck.evaluate(db, h, _sample(_res("/mnt/data", True)), t0 + 5)
    assert len(ck.evaluate(db, h, _sample(_res("/mnt/data", False)), t0 + 10)[1]) == 1


def test_no_responder_no_alert(db, h):
    """New check: without --notify nothing fires, however long it fails."""
    c = db.check_add(h.id, MOUNT, "/mnt/data", {}, "test")
    assert ck.evaluate(db, h, _sample(_res("/mnt/data", False)), c["created"] + 10 * ck.NEW_CHECK_GRACE_SEC)[1] == []
