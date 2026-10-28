"""SQLite storage: host registry, raw samples, 1-minute rollups, retention.

Raw samples are kept at the agent's native interval for `retention_raw_hours`;
a background job folds completed minutes into `samples_1m`, which is kept for
`retention_1m_days`. Series queries pick the table and bucket size from the
requested range so the UI always gets a sane number of points.
"""
from __future__ import annotations

import hashlib
import json
import logging
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

from . import datause
from .models import Sample

_log = logging.getLogger("avalon")

# Scalar columns extracted from every sample. Order matters (INSERT below).
SCALAR_COLUMNS = (
    "cpu", "load1", "cpu_temp", "mem", "swap",
    "disk_read", "disk_write", "net_rx", "net_tx",
    "gpu_util", "gpu_mem", "gpu_temp", "temp_max", "disk_used",
)

# Public metric name -> SQL column. `series` API accepts these keys.
METRICS: dict[str, str] = {c: c for c in SCALAR_COLUMNS}

SCHEMA = f"""
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA temp_store=MEMORY;

CREATE TABLE IF NOT EXISTS hosts (
    id            INTEGER PRIMARY KEY,
    name          TEXT NOT NULL UNIQUE,
    display_name  TEXT,
    token_hash    TEXT NOT NULL UNIQUE,
    enabled       INTEGER NOT NULL DEFAULT 1,
    created_at    REAL NOT NULL,
    last_seen     REAL,
    last_ip       TEXT,
    last_sample   TEXT,
    notes         TEXT,
    check_config  TEXT,
    check_rev     INTEGER NOT NULL DEFAULT 0,
    pending_cmd   TEXT
);

CREATE TABLE IF NOT EXISTS samples (
    host_id   INTEGER NOT NULL REFERENCES hosts(id) ON DELETE CASCADE,
    ts        REAL NOT NULL,
    {", ".join(f"{c} REAL" for c in SCALAR_COLUMNS)},
    PRIMARY KEY (host_id, ts)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS samples_1m (
    host_id   INTEGER NOT NULL REFERENCES hosts(id) ON DELETE CASCADE,
    ts        REAL NOT NULL,
    n         INTEGER NOT NULL,
    {", ".join(f"{c} REAL" for c in SCALAR_COLUMNS)},
    PRIMARY KEY (host_id, ts)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- Tracked tasks (training runs, services, anything reported through the `avm` CLI).
CREATE TABLE IF NOT EXISTS tasks (
    id          INTEGER PRIMARY KEY,
    host_id     INTEGER NOT NULL REFERENCES hosts(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    status      TEXT NOT NULL,            -- queued | running | done | failed | stalled | lost (queued: `avm run --wait` waiting for resources)
    source      TEXT NOT NULL DEFAULT '', -- run | task | watch
    started     REAL NOT NULL,
    ended       REAL,
    updated     REAL NOT NULL,
    heartbeat   REAL NOT NULL,
    expect_beat REAL,                     -- seconds; NULL = no stall detection
    progress    REAL,
    rc          INTEGER,
    command     TEXT,
    cwd         TEXT,
    pid         INTEGER,
    user        TEXT,
    log         TEXT NOT NULL DEFAULT '',
    meta        TEXT,
    note        TEXT,
    acked       REAL,                     -- when a failed/stalled/lost task was acknowledged (stops alerting)
    acked_by    TEXT
);
CREATE INDEX IF NOT EXISTS tasks_host_status ON tasks(host_id, status);
CREATE INDEX IF NOT EXISTS tasks_started ON tasks(started);

-- Claude Code sessions launched from the dashboard.
CREATE TABLE IF NOT EXISTS claude_jobs (
    id              INTEGER PRIMARY KEY,
    host_id         INTEGER NOT NULL REFERENCES hosts(id) ON DELETE CASCADE,
    status          TEXT NOT NULL,        -- queued | running | done | failed | cancelled
    prompt          TEXT NOT NULL,
    cwd             TEXT,
    mode            TEXT NOT NULL,        -- plan | acceptEdits | auto | full
    model           TEXT,
    resume_session  TEXT,
    session_id      TEXT,
    created         REAL NOT NULL,
    started         REAL,
    ended           REAL,
    created_by      TEXT,
    result          TEXT,
    cost_usd        REAL,
    duration_ms     INTEGER,
    num_turns       INTEGER,
    error           TEXT,
    cancel          INTEGER NOT NULL DEFAULT 0,
    title           TEXT
);
CREATE INDEX IF NOT EXISTS claude_jobs_host ON claude_jobs(host_id, status);

-- Checks, one row per watched thing (the source of truth for the AVM_WATCH_* settings pushed to agents, which
-- are rendered from these rows). Each may carry a label, an owner session (`avm check add` from inside it), an
-- expiry, and a responder: on an ok -> failing transition the hub prompts a Claude agent on that host
-- (read-only, guard-railed: cooldown, cap per rolling 24 h, one open alert until the check recovers).
CREATE TABLE IF NOT EXISTS host_checks (
    id              INTEGER PRIMARY KEY,
    host_id         INTEGER NOT NULL REFERENCES hosts(id) ON DELETE CASCADE,
    key             TEXT NOT NULL,        -- AVM_WATCH_* list it renders into (or AVM_MEM_AVAILABLE_WARN_GB)
    target          TEXT NOT NULL,        -- the entry, e.g. "/srv/heartbeat:600"
    label           TEXT,
    owner           TEXT,                 -- tracked-session key of the session that added it (avm check add)
    created         REAL NOT NULL,
    created_by      TEXT,
    expires         REAL,                 -- removed after this (avm check add --ttl)
    notify          TEXT,                 -- NULL | "tracked:<claude_tracked.id>" | "new"
    notify_cwd      TEXT,                 -- working dir for a "new" responder session
    notify_log      INTEGER NOT NULL DEFAULT 0,   -- include related task log tails in the alert (off: paths/ids only)
    cooldown_min    REAL NOT NULL DEFAULT 30,
    daily_cap       INTEGER NOT NULL DEFAULT 6,   -- alerts per rolling 24 h
    state           TEXT,                 -- ok | failing | unknown (from the agent's reports)
    since           REAL,                 -- when the last known (ok/failing) state began; unknown doesn't reset it
    last_ok         REAL,
    detail          TEXT,
    last_fired      REAL,
    fires_day       TEXT,                 -- unused since the cap became a rolling window (fire_log)
    fires_n         INTEGER NOT NULL DEFAULT 0,   -- alerts in the last 24 h, as of the last one
    open_job        INTEGER,              -- responder job of the current failure (cleared when the check recovers)
    pending         INTEGER NOT NULL DEFAULT 0,   -- failure seen but no alert yet (cooldown, cap, target mid-turn,
                                                  -- no Claude to prompt): fires once it can, if still failing
    known_state     TEXT,                 -- last ok | failing reported (state may be unknown in between)
    fire_log        TEXT,                 -- JSON list of alert times in the last 24 h (the rolling cap)
    hold_reason     TEXT,                 -- why a pending alert hasn't gone out (cap, cooldown, host policy, ...)
    alert_mode      TEXT,                 -- permission mode of its responder prompt (NULL = plan; operator-set only)
    UNIQUE(host_id, key, target)
);

-- Claude Code sessions the user chose to track on the Agents page. Kept when the session stops or its host goes
-- offline, so it can still be seen and restarted. key = the background agent's id from `claude agents`
-- (stable across restarts), or the session id for an interactive session. Updated from each agent report.
CREATE TABLE IF NOT EXISTS claude_tracked (
    id              INTEGER PRIMARY KEY,
    host_id         INTEGER NOT NULL REFERENCES hosts(id) ON DELETE CASCADE,
    key             TEXT NOT NULL,
    session_id      TEXT,
    name            TEXT,
    cwd             TEXT,
    kind            TEXT,
    note            TEXT,
    added           REAL NOT NULL,
    added_by        TEXT,
    live            INTEGER NOT NULL DEFAULT 0,
    state           TEXT,                 -- working | blocked | done | ended (Claude's own task state)
    status          TEXT,                 -- busy | idle while live
    detail          TEXT,
    last_said       TEXT,
    last_activity   REAL,
    last_live       REAL,
    UNIQUE(host_id, key)
);

CREATE TABLE IF NOT EXISTS claude_events (
    job_id  INTEGER NOT NULL REFERENCES claude_jobs(id) ON DELETE CASCADE,
    seq     INTEGER NOT NULL,
    ts      REAL NOT NULL,
    data    TEXT NOT NULL,
    PRIMARY KEY (job_id, seq)
) WITHOUT ROWID;

-- Research results documents pushed by projects (`avm results push`). Every push is kept:
-- the latest one is rendered, older ones give per-metric history. Aggregates only - the hub
-- stores and renders exactly what was pushed and never fetches anything itself.
CREATE TABLE IF NOT EXISTS results (
    id           INTEGER PRIMARY KEY,
    project      TEXT NOT NULL,
    host_id      INTEGER REFERENCES hosts(id) ON DELETE SET NULL,
    received     REAL NOT NULL,
    schema       TEXT NOT NULL,
    generated_at TEXT,
    commit_ref   TEXT,
    headline     TEXT NOT NULL DEFAULT '[]', -- headline id/value/ci list extracted at push time, for cheap history
    doc          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS results_project ON results(project, received);

-- Per-user dashboard preferences (layout, hidden panels, card order).
CREATE TABLE IF NOT EXISTS prefs (
    user    TEXT PRIMARY KEY,
    data    TEXT NOT NULL,
    updated REAL NOT NULL
);
"""

TASK_LOG_LINES = 200
TASK_STATES = ("queued", "running", "done", "failed", "stalled", "lost")
JOB_STATES = ("queued", "running", "done", "failed", "cancelled")


def _row(r: sqlite3.Row) -> dict:
    return dict(r) if r is not None else None


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_token() -> str:
    return "avm_" + secrets.token_urlsafe(32)


def _max_or_none(values: Iterable[Optional[float]]) -> Optional[float]:
    vals = [v for v in values if v is not None]
    return max(vals) if vals else None


def _avg_or_none(values: Iterable[Optional[float]]) -> Optional[float]:
    vals = [v for v in values if v is not None]
    return sum(vals) / len(vals) if vals else None


def extract_scalars(s: Sample) -> dict[str, Optional[float]]:
    """Flatten a sample to the columns stored in the time-series tables."""
    gpus = s.gpus
    root = next((d for d in s.disks if d.mountpoint in ("/", "C:\\")), None)
    disk_used = root.percent if root else _max_or_none(d.percent for d in s.disks)
    return {
        "cpu": s.cpu.percent,
        "load1": s.cpu.load[0] if s.cpu.load else None,
        "cpu_temp": s.cpu.temp_c,
        "mem": s.memory.percent,
        "swap": s.memory.swap_percent,
        "disk_read": s.disk_io.read_bps,
        "disk_write": s.disk_io.write_bps,
        "net_rx": s.network.rx_bps,
        "net_tx": s.network.tx_bps,
        "gpu_util": _avg_or_none(g.util_percent for g in gpus),
        "gpu_mem": _avg_or_none(g.mem_percent for g in gpus),
        "gpu_temp": _max_or_none(g.temp_c for g in gpus),
        "temp_max": _max_or_none(t.current for t in s.temps),
        "disk_used": disk_used,
    }


@dataclass
class HostRow:
    id: int
    name: str
    display_name: Optional[str]
    enabled: bool
    created_at: float
    last_seen: Optional[float]
    last_ip: Optional[str]
    last_sample: Optional[dict]
    notes: Optional[str]
    check_config: Optional[dict] = None
    check_rev: int = 0
    pending_cmd: Optional[str] = None

    @classmethod
    def from_row(cls, r: sqlite3.Row) -> "HostRow":
        keys = r.keys()
        return cls(
            id=r["id"], name=r["name"], display_name=r["display_name"],
            enabled=bool(r["enabled"]), created_at=r["created_at"],
            last_seen=r["last_seen"], last_ip=r["last_ip"],
            last_sample=json.loads(r["last_sample"]) if r["last_sample"] else None,
            notes=r["notes"],
            check_config=json.loads(r["check_config"]) if "check_config" in keys and r["check_config"] else None,
            check_rev=r["check_rev"] if "check_rev" in keys else 0,
            pending_cmd=r["pending_cmd"] if "pending_cmd" in keys else None,
        )


class Database:
    def __init__(self, path: str):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys=ON")
        had_checks = self._conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='host_checks'").fetchone() is not None
        self._conn.executescript(SCHEMA)
        if not had_checks:   # a database from before host_checks: its legacy check settings are still to be imported
            self._conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('legacy_checks_imported', '0')")
        self._migrate()

    def _migrate(self) -> None:
        """Add columns introduced after the first release (SQLite has no IF NOT EXISTS for columns)."""
        cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(hosts)")}
        for col, ddl in (("check_config", "TEXT"), ("check_rev", "INTEGER NOT NULL DEFAULT 0"), ("pending_cmd", "TEXT")):
            if col not in cols:
                self._conn.execute(f"ALTER TABLE hosts ADD COLUMN {col} {ddl}")
        cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(tasks)")}
        for col, ddl in (("acked", "REAL"), ("acked_by", "TEXT")):
            if col not in cols:
                self._conn.execute(f"ALTER TABLE tasks ADD COLUMN {col} {ddl}")
        cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(claude_tracked)")}
        if "remote_control" not in cols:   # linked to Remote Control: reachable from sessions on other machines
            self._conn.execute("ALTER TABLE claude_tracked ADD COLUMN remote_control INTEGER")
        cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(claude_jobs)")}
        for col, ddl in (("action", "TEXT"), ("target", "TEXT"), ("fork_session", "INTEGER NOT NULL DEFAULT 0"),
                         ("acked", "REAL"), ("acked_by", "TEXT")):
            if col not in cols:   # action: restart/stop a tracked session; fork_session: prompt a copy of a live session;
                                  # acked: a failed prompt dismissed from Alerts
                self._conn.execute(f"ALTER TABLE claude_jobs ADD COLUMN {col} {ddl}")
        cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(host_checks)")}
        for col, ddl in (("known_state", "TEXT"), ("fire_log", "TEXT"), ("hold_reason", "TEXT"), ("alert_mode", "TEXT")):
            if col not in cols:   # known_state: last ok/failing across unknown gaps; fire_log: rolling 24 h alert cap;
                                  # hold_reason: why a pending alert is held; alert_mode: responder mode (NULL = plan)
                self._conn.execute(f"ALTER TABLE host_checks ADD COLUMN {col} {ddl}")
        self._backfill_known_state()
        self._import_legacy_checks()

    def _backfill_known_state(self) -> None:
        """known_state came after state: a row that was "unknown" when the column was added got NULL, so its next
        failure didn't count as ok -> failing and never alerted. Derive it from what the row still says: a current
        ok/failing state; an open or pending alert (failing); else last_ok against since (since = when the last known
        state began; last_ok stops moving once a check fails, so last_ok >= since means it was ok). Rows with neither
        never had a known state and stay NULL. One UPDATE (atomic), only on NULL rows: running it on every start is a
        no-op once done, and never touches a row evaluate has already set."""
        with self._lock:
            n = self._conn.execute("""
                UPDATE host_checks SET known_state = CASE
                    WHEN state IN ('ok', 'failing') THEN state
                    WHEN open_job IS NOT NULL OR pending THEN 'failing'
                    WHEN last_ok IS NOT NULL AND (since IS NULL OR last_ok >= since) THEN 'ok'
                    ELSE 'failing' END
                WHERE known_state IS NULL AND (state IN ('ok', 'failing') OR open_job IS NOT NULL OR pending
                                               OR last_ok IS NOT NULL OR since IS NOT NULL)""").rowcount
        if n:
            _log.info("backfilled known_state on %d check row(s)", n)

    def _import_legacy_checks(self) -> None:
        """Once per database (meta legacy_checks_imported: '0' = to do, '1' = done): hosts whose AVM_WATCH_* settings
        predate host_checks get one row per entry, in one transaction. No flag at all = host_checks came with an
        earlier release, which imported (and re-ran this on every start): it only gets the flag, so stale settings
        never come back."""
        from .checks import CHECK_KEYS   # local import: checks.py imports nothing from db
        flag = self._conn.execute("SELECT value FROM meta WHERE key='legacy_checks_imported'").fetchone()
        if flag and flag["value"] == "1":
            return
        now = time.time()
        self._conn.execute("BEGIN")
        try:
            for r in (self._conn.execute("SELECT id, check_config FROM hosts WHERE check_config IS NOT NULL").fetchall()
                      if flag else []):
                if self._conn.execute("SELECT 1 FROM host_checks WHERE host_id=? LIMIT 1", (r["id"],)).fetchone():
                    continue
                cfg = json.loads(r["check_config"] or "{}")
                for key in CHECK_KEYS:
                    entries = [cfg.get(key) or ""] if key == "AVM_MEM_AVAILABLE_WARN_GB" else (cfg.get(key) or "").split(",")
                    for e in (x.strip() for x in entries if x):
                        if e:
                            self._conn.execute("INSERT OR IGNORE INTO host_checks(host_id, key, target, created, created_by) VALUES (?,?,?,?,?)",
                                               (r["id"], key, e, now, "imported"))
            self._conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('legacy_checks_imported', '1')")
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def locked(self) -> threading.RLock:
        """`with db.locked():` - a read-modify-write across several calls (every method takes this re-entrant lock, so
        nothing else writes in between): checks.put_settings re-reads a host's settings under it."""
        return self._lock

    # ---------------------------------------------------------------- hosts
    def create_host(self, name: str, display_name: str | None = None, notes: str | None = None) -> tuple[HostRow, str]:
        token = new_token()
        with self._lock:
            self._conn.execute(
                "INSERT INTO hosts(name, display_name, token_hash, created_at, notes) VALUES (?,?,?,?,?)",
                (name, display_name, hash_token(token), time.time(), notes),
            )
            row = self._conn.execute("SELECT * FROM hosts WHERE name=?", (name,)).fetchone()
        return HostRow.from_row(row), token

    def rotate_token(self, name: str) -> str:
        token = new_token()
        with self._lock:
            cur = self._conn.execute("UPDATE hosts SET token_hash=? WHERE name=?", (hash_token(token), name))
            if cur.rowcount == 0:
                raise KeyError(name)
        return token

    def set_enabled(self, name: str, enabled: bool) -> None:
        with self._lock:
            cur = self._conn.execute("UPDATE hosts SET enabled=? WHERE name=?", (1 if enabled else 0, name))
            if cur.rowcount == 0:
                raise KeyError(name)

    def delete_host(self, name: str) -> None:
        with self._lock:
            cur = self._conn.execute("DELETE FROM hosts WHERE name=?", (name,))
            if cur.rowcount == 0:
                raise KeyError(name)

    def rename_host(self, name: str, display_name: str | None) -> None:
        with self._lock:
            cur = self._conn.execute("UPDATE hosts SET display_name=? WHERE name=?", (display_name, name))
            if cur.rowcount == 0:
                raise KeyError(name)

    def host_by_token(self, token: str) -> Optional[HostRow]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM hosts WHERE token_hash=?", (hash_token(token),)).fetchone()
        return HostRow.from_row(row) if row else None

    def host_by_name(self, name: str) -> Optional[HostRow]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM hosts WHERE name=?", (name,)).fetchone()
        return HostRow.from_row(row) if row else None

    def host_by_id(self, host_id: int) -> Optional[HostRow]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM hosts WHERE id=?", (host_id,)).fetchone()
        return HostRow.from_row(row) if row else None

    def list_hosts(self) -> list[HostRow]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM hosts ORDER BY name").fetchall()
        return [HostRow.from_row(r) for r in rows]

    # ------------------------------------------------------- remote config
    def set_check_config(self, name: str, config: dict) -> int:
        """Store the agent's remote check settings; returns the new revision."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE hosts SET check_config=?, check_rev=check_rev+1 WHERE name=?",
                (json.dumps(config, separators=(",", ":")), name),
            )
            if cur.rowcount == 0:
                raise KeyError(name)
            return self._conn.execute("SELECT check_rev FROM hosts WHERE name=?", (name,)).fetchone()["check_rev"]

    def set_command(self, name: str, cmd: Optional[str]) -> None:
        with self._lock:
            cur = self._conn.execute("UPDATE hosts SET pending_cmd=? WHERE name=?", (cmd, name))
            if cur.rowcount == 0:
                raise KeyError(name)

    def pop_command(self, host_id: int) -> Optional[str]:
        with self._lock:
            row = self._conn.execute("SELECT pending_cmd FROM hosts WHERE id=?", (host_id,)).fetchone()
            if row and row["pending_cmd"]:
                self._conn.execute("UPDATE hosts SET pending_cmd=NULL WHERE id=?", (host_id,))
                return row["pending_cmd"]
        return None

    # -------------------------------------------------------------- samples
    def record_sample(self, host: HostRow, sample: Sample, ip: str | None) -> dict[str, Optional[float]]:
        scalars = extract_scalars(sample)
        payload = sample.model_dump(mode="json")
        cols = ", ".join(SCALAR_COLUMNS)
        marks = ", ".join("?" for _ in SCALAR_COLUMNS)
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                self._conn.execute(
                    f"INSERT OR REPLACE INTO samples(host_id, ts, {cols}) VALUES (?,?,{marks})",
                    (host.id, sample.ts, *[scalars[c] for c in SCALAR_COLUMNS]),
                )
                self._conn.execute(
                    "UPDATE hosts SET last_seen=?, last_ip=?, last_sample=? WHERE id=?",
                    (time.time(), ip, json.dumps(payload, separators=(",", ":")), host.id),
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        return scalars

    def series(self, host_id: int, metrics: list[str], start: float, end: float,
               max_points: int = 600, raw_retention_sec: float = 0) -> dict[str, Any]:
        """Return {"step": s, "ts": [...], "<metric>": [...]} bucketed to <= max_points."""
        cols = [METRICS[m] for m in metrics]
        span = max(end - start, 1.0)
        step = max(1.0, span / max_points)
        now = time.time()
        use_rollup = raw_retention_sec and start < now - raw_retention_sec
        table = "samples_1m" if use_rollup else "samples"
        if use_rollup:
            step = max(step, 60.0)
        step = float(int(step)) if step >= 1 else step
        agg = ", ".join(f"AVG({c}) AS {c}" for c in cols)
        sql = (
            f"SELECT CAST(ts / ? AS INTEGER) * ? AS bucket, {agg} "
            f"FROM {table} WHERE host_id=? AND ts>=? AND ts<? "
            f"GROUP BY bucket ORDER BY bucket"
        )
        with self._lock:
            rows = self._conn.execute(sql, (step, step, host_id, start, end)).fetchall()
        out: dict[str, Any] = {"step": step, "source": table, "ts": [r["bucket"] for r in rows]}
        for c in cols:
            out[c] = [None if r[c] is None else round(r[c], 3) for r in rows]
        return out

    def latest_points(self, host_id: int, metric: str, since: float) -> list[tuple[float, Optional[float]]]:
        col = METRICS[metric]
        with self._lock:
            rows = self._conn.execute(
                f"SELECT ts, {col} AS v FROM samples WHERE host_id=? AND ts>=? ORDER BY ts", (host_id, since)
            ).fetchall()
        return [(r["ts"], r["v"]) for r in rows]

    # ------------------------------------------------------------ lifecycle
    # Agent timestamps are kept when within 120 s of the hub's clock (main.py ingest), so a sample can land in a minute
    # that was already rolled up: each pass re-aggregates this far before the previous cutoff (INSERT OR REPLACE).
    ROLLUP_REDO_SEC = 180

    def rollup(self, now: float | None = None) -> int:
        """Fold complete minutes into samples_1m, redoing the trailing ROLLUP_REDO_SEC so late samples (a host whose
        clock is behind) are counted. Returns number of rollup rows written."""
        now = now or time.time()
        cutoff = float(int(now // 60) * 60)  # start of the current (incomplete) minute
        with self._lock:
            row = self._conn.execute("SELECT value FROM meta WHERE key='rollup_ts'").fetchone()
            last = float(row["value"]) if row else 0.0
            if cutoff <= last:
                return 0
            agg = ", ".join(f"AVG({c})" for c in SCALAR_COLUMNS)
            cols = ", ".join(SCALAR_COLUMNS)
            self._conn.execute("BEGIN")
            try:
                cur = self._conn.execute(
                    f"INSERT OR REPLACE INTO samples_1m(host_id, ts, n, {cols}) "
                    f"SELECT host_id, CAST(ts/60 AS INTEGER)*60 AS m, COUNT(*), {agg} "
                    f"FROM samples WHERE ts>=? AND ts<? GROUP BY host_id, m",
                    (max(0.0, last - self.ROLLUP_REDO_SEC), cutoff),
                )
                self._conn.execute(
                    "INSERT OR REPLACE INTO meta(key, value) VALUES ('rollup_ts', ?)", (str(cutoff),)
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
            return cur.rowcount

    def prune(self, raw_hours: int, rollup_days: int, now: float | None = None) -> tuple[int, int]:
        now = now or time.time()
        with self._lock:
            a = self._conn.execute("DELETE FROM samples WHERE ts < ?", (now - raw_hours * 3600,)).rowcount
            b = self._conn.execute("DELETE FROM samples_1m WHERE ts < ?", (now - rollup_days * 86400,)).rowcount
        return a, b

    # ---------------------------------------------------------------- tasks
    def task_create(self, host_id: int, name: str, source: str, expect_beat: Optional[float], command: str | None,
                    cwd: str | None, pid: int | None, user: str | None, meta: dict | None, status: str = "running") -> dict:
        now = time.time()
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO tasks(host_id, name, status, source, started, updated, heartbeat, expect_beat, command, cwd, pid, user, meta)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (host_id, name[:200], status, source, now, now, now, expect_beat, command, cwd, pid, user,
                 json.dumps(meta) if meta else None),
            )
            return _row(self._conn.execute("SELECT * FROM tasks WHERE id=?", (cur.lastrowid,)).fetchone())

    def task_get(self, task_id: int) -> Optional[dict]:
        with self._lock:
            return _row(self._conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())

    def task_update(self, task_id: int, host_id: int | None = None, *, status: str | None = None, progress: float | None = None,
                    rc: int | None = None, log_lines: list[str] | None = None, note: str | None = None, beat: bool = True,
                    pid: int | None = None, meta: dict | None = None) -> Optional[dict]:
        """Apply a heartbeat / log / finish update. host_id (when given) must match - agents can only touch their own tasks.
        meta is merged key by key (a None value removes the key). queued -> running restarts the clock at admission;
        the time it was queued stays in meta.queue.since."""
        now = time.time()
        with self._lock:
            row = self._conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None or (host_id is not None and row["host_id"] != host_id):
                return None
            sets, vals = ["updated=?"], [now]
            if beat:
                sets.append("heartbeat=?"); vals.append(now)
            if status:
                sets.append("status=?"); vals.append(status)
                if status in ("done", "failed"):
                    sets.append("ended=?"); vals.append(now)
                if status == "running" and row["status"] == "queued":   # admitted: elapsed counts from here
                    sets.append("started=?"); vals.append(now)
                if status != row["status"]:
                    sets.append("acked=NULL, acked_by=NULL")   # a new outcome alerts afresh
            elif beat and row["status"] in ("stalled", "lost"):   # lost: its host's reports paused (agent restart, Wi-Fi blip)
                sets.append("status='running', acked=NULL, acked_by=NULL")
            if progress is not None:
                sets.append("progress=?"); vals.append(max(0.0, min(1.0, float(progress))))
            if rc is not None:
                sets.append("rc=?"); vals.append(int(rc))
            if pid is not None:
                sets.append("pid=?"); vals.append(int(pid))
            if note is not None:
                sets.append("note=?"); vals.append(note[:2000])
            if log_lines:
                merged = (row["log"].splitlines() + [l[:500] for l in log_lines])[-TASK_LOG_LINES:]
                sets.append("log=?"); vals.append("\n".join(merged))
            if meta:
                cur_meta = json.loads(row["meta"]) if row["meta"] else {}
                for k, v in meta.items():
                    if v is None:
                        cur_meta.pop(k, None)
                    else:
                        cur_meta[k] = v
                sets.append("meta=?"); vals.append(json.dumps(cur_meta) if cur_meta else None)
            vals.append(task_id)
            self._conn.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id=?", vals)
            return _row(self._conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())

    def task_list(self, host_id: int | None = None, status: str | None = None, limit: int = 200, with_log: bool = False) -> list[dict]:
        cols = "*" if with_log else "id, host_id, name, status, source, started, ended, updated, heartbeat, expect_beat, progress, rc, command, cwd, pid, user, meta, note, acked, acked_by"
        where, vals = [], []
        if host_id is not None:
            where.append("host_id=?"); vals.append(host_id)
        if status:
            where.append("status=?"); vals.append(status)
        sql = f"SELECT {cols} FROM tasks" + (" WHERE " + " AND ".join(where) if where else "") + \
              " ORDER BY CASE WHEN status IN ('running','stalled') THEN 0 WHEN status='queued' THEN 1 ELSE 2 END, started DESC LIMIT ?"
        vals.append(limit)
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, vals).fetchall()]

    def task_delete(self, task_id: int) -> bool:
        with self._lock:
            return self._conn.execute("DELETE FROM tasks WHERE id=?", (task_id,)).rowcount > 0

    def task_ack(self, task_ids: list[int] | None = None, host_id: int | None = None, by: str | None = None,
                 undo: bool = False, now: float | None = None) -> list[dict]:
        """Acknowledge (or un-acknowledge) failed/stalled/lost tasks so they stop alerting. Nothing is deleted.
        task_ids=None means every matching problem task (optionally on one host). Returns changed rows."""
        now = now or time.time()
        where, vals = ["status IN ('failed','stalled','lost')", "acked IS NOT NULL" if undo else "acked IS NULL"], []
        if task_ids is not None:
            if not task_ids:
                return []
            where.append(f"id IN ({','.join('?' * len(task_ids))})"); vals += task_ids
        if host_id is not None:
            where.append("host_id=?"); vals.append(host_id)
        with self._lock:
            ids = [r["id"] for r in self._conn.execute(f"SELECT id FROM tasks WHERE {' AND '.join(where)}", vals).fetchall()]
            if ids:
                marks = ",".join("?" * len(ids))
                if undo:
                    self._conn.execute(f"UPDATE tasks SET acked=NULL, acked_by=NULL WHERE id IN ({marks})", ids)
                else:
                    self._conn.execute(f"UPDATE tasks SET acked=?, acked_by=? WHERE id IN ({marks})", [now, by, *ids])
            return [dict(self._conn.execute("SELECT * FROM tasks WHERE id=?", (i,)).fetchone()) for i in ids]

    def task_mark_stalled(self, now: float | None = None) -> list[dict]:
        """Running tasks whose heartbeat is older than 1.5x their expectation -> stalled. Returns changed rows."""
        now = now or time.time()
        with self._lock:
            rows = self._conn.execute(
                "SELECT id FROM tasks WHERE status IN ('running','queued') AND expect_beat IS NOT NULL AND heartbeat < ? - expect_beat * 1.5", (now,)
            ).fetchall()
            ids = [r["id"] for r in rows]
            if ids:
                self._conn.execute(f"UPDATE tasks SET status='stalled', updated=?, acked=NULL, acked_by=NULL WHERE id IN ({','.join('?' * len(ids))})", [now, *ids])
            return [dict(self._conn.execute("SELECT * FROM tasks WHERE id=?", (i,)).fetchone()) for i in ids]

    def task_mark_lost(self, host_id: int, now: float | None = None) -> list[dict]:
        """A host went offline: its running/stalled tasks can no longer report."""
        now = now or time.time()
        with self._lock:
            rows = self._conn.execute("SELECT id FROM tasks WHERE host_id=? AND status IN ('running','stalled','queued')", (host_id,)).fetchall()
            ids = [r["id"] for r in rows]
            if ids:
                self._conn.execute(f"UPDATE tasks SET status='lost', updated=?, acked=NULL, acked_by=NULL WHERE id IN ({','.join('?' * len(ids))})", [now, *ids])
            return [dict(self._conn.execute("SELECT * FROM tasks WHERE id=?", (i,)).fetchone()) for i in ids]

    def task_prune(self, days: int, now: float | None = None) -> int:
        now = now or time.time()
        with self._lock:
            return self._conn.execute("DELETE FROM tasks WHERE status IN ('done','failed','lost') AND updated < ?", (now - days * 86400,)).rowcount

    # -------------------------------------------------------------- results
    def results_add(self, project: str, host_id: int | None, schema: str, generated_at: str | None, commit_ref: str | None,
                    headline: list[dict], doc: str) -> dict:
        now = time.time()
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO results(project, host_id, received, schema, generated_at, commit_ref, headline, doc) VALUES (?,?,?,?,?,?,?,?)",
                (project, host_id, now, schema, generated_at, commit_ref, json.dumps(headline), doc))
            return {"id": cur.lastrowid, "project": project, "received": now, "generated_at": generated_at, "commit": commit_ref}

    def results_projects(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT r.project, COUNT(*) AS pushes, MAX(r.received) AS last_received,"
                " (SELECT commit_ref FROM results x WHERE x.project=r.project ORDER BY received DESC LIMIT 1) AS last_commit"
                " FROM results r GROUP BY r.project ORDER BY last_received DESC").fetchall()
            return [dict(r) for r in rows]

    def results_get(self, project: str, push_id: int | None = None) -> Optional[dict]:
        """One push (the latest when push_id is None), with its document."""
        with self._lock:
            if push_id is None:
                r = self._conn.execute("SELECT * FROM results WHERE project=? ORDER BY received DESC, id DESC LIMIT 1", (project,)).fetchone()
            else:
                r = self._conn.execute("SELECT * FROM results WHERE project=? AND id=?", (project, push_id)).fetchone()
            return dict(r) if r else None

    def results_history(self, project: str, limit: int = 500) -> list[dict]:
        """Oldest -> newest pushes without their documents (headline values only)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, received, generated_at, commit_ref, host_id, headline FROM results WHERE project=?"
                " ORDER BY received DESC, id DESC LIMIT ?", (project, limit)).fetchall()
        out = [dict(r) for r in reversed(rows)]
        for r in out:
            r["headline"] = json.loads(r["headline"] or "[]")
        return out

    # ---------------------------------------------------------- claude jobs
    def job_create(self, host_id: int, prompt: str, cwd: str | None, mode: str, model: str | None,
                   resume_session: str | None, created_by: str | None, action: str | None = None,
                   target: dict | None = None, title: str | None = None, fork_session: bool = False) -> dict:
        now = time.time()
        title = title or (prompt.strip().splitlines()[0][:80] if prompt.strip() else "(empty)")
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO claude_jobs(host_id, status, prompt, cwd, mode, model, resume_session, created, created_by, title, action, target, fork_session)"
                " VALUES (?,'queued',?,?,?,?,?,?,?,?,?,?,?)",
                (host_id, prompt, cwd, mode, model, resume_session, now, created_by, title, action,
                 json.dumps(target) if target is not None else None, 1 if fork_session else 0),
            )
            return _row(self._conn.execute("SELECT * FROM claude_jobs WHERE id=?", (cur.lastrowid,)).fetchone())

    def job_get(self, job_id: int) -> Optional[dict]:
        with self._lock:
            return _row(self._conn.execute("SELECT * FROM claude_jobs WHERE id=?", (job_id,)).fetchone())

    def job_list(self, host_id: int | None = None, limit: int = 100) -> list[dict]:
        sql = "SELECT * FROM claude_jobs" + (" WHERE host_id=?" if host_id is not None else "") + \
              " ORDER BY (status IN ('queued','running')) DESC, created DESC LIMIT ?"
        vals = ([host_id] if host_id is not None else []) + [limit]
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, vals).fetchall()]

    def jobs_pending_for(self, host_id: int, actions_ok: bool = False) -> tuple[list[dict], list[int]]:
        """(queued jobs to hand to the agent, ids of running jobs with cancel requested).
        Action jobs (restart/stop) wait until the agent says it understands them: an older agent would run them as prompts."""
        with self._lock:
            queued = []
            for r in self._conn.execute(
                    "SELECT id, prompt, cwd, mode, model, resume_session, action, target, fork_session FROM claude_jobs"
                    " WHERE host_id=? AND status='queued' ORDER BY id", (host_id,)).fetchall():
                j = dict(r)
                if j["action"] and not actions_ok:
                    continue
                if not j["fork_session"]:
                    del j["fork_session"]
                if not j["action"]:
                    del j["action"], j["target"]   # prompt jobs look exactly as they did for older agents
                else:
                    j["target"] = json.loads(j["target"] or "{}")
                queued.append(j)
            cancel = [r["id"] for r in self._conn.execute(
                "SELECT id FROM claude_jobs WHERE host_id=? AND status IN ('queued','running') AND cancel=1", (host_id,)).fetchall()]
            return queued, cancel

    def job_update(self, job_id: int, host_id: int | None = None, **fields) -> Optional[dict]:
        allowed = {"status", "started", "ended", "session_id", "result", "cost_usd", "duration_ms", "num_turns", "error", "cancel", "title"}
        sets, vals = [], []
        for k, v in fields.items():
            if k in allowed:
                sets.append(f"{k}=?"); vals.append(v)
        if not sets:
            return self.job_get(job_id)
        with self._lock:
            row = self._conn.execute("SELECT host_id FROM claude_jobs WHERE id=?", (job_id,)).fetchone()
            if row is None or (host_id is not None and row["host_id"] != host_id):
                return None
            vals.append(job_id)
            self._conn.execute(f"UPDATE claude_jobs SET {', '.join(sets)} WHERE id=?", vals)
            return _row(self._conn.execute("SELECT * FROM claude_jobs WHERE id=?", (job_id,)).fetchone())

    def job_ack(self, job_ids: list[int] | None = None, by: str | None = None, undo: bool = False,
                now: float | None = None) -> list[dict]:
        """Acknowledge (or un-acknowledge) failed prompts so they stop showing in Alerts. Nothing is deleted.
        job_ids=None means every failed prompt. Returns changed rows."""
        now = now or time.time()
        where, vals = ["status='failed'", "acked IS NOT NULL" if undo else "acked IS NULL"], []
        if job_ids is not None:
            if not job_ids:
                return []
            where.append(f"id IN ({','.join('?' * len(job_ids))})"); vals += job_ids
        with self._lock:
            ids = [r["id"] for r in self._conn.execute(f"SELECT id FROM claude_jobs WHERE {' AND '.join(where)}", vals).fetchall()]
            if ids:
                marks = ",".join("?" * len(ids))
                if undo:
                    self._conn.execute(f"UPDATE claude_jobs SET acked=NULL, acked_by=NULL WHERE id IN ({marks})", ids)
                else:
                    self._conn.execute(f"UPDATE claude_jobs SET acked=?, acked_by=? WHERE id IN ({marks})", [now, by, *ids])
            return [_row(self._conn.execute("SELECT * FROM claude_jobs WHERE id=?", (i,)).fetchone()) for i in ids]

    def job_add_events(self, job_id: int, host_id: int, events: list[dict]) -> list[tuple[int, dict]]:
        """Append events; returns [(seq, event)] as stored."""
        now = time.time()
        out = []
        with self._lock:
            row = self._conn.execute("SELECT host_id FROM claude_jobs WHERE id=?", (job_id,)).fetchone()
            if row is None or row["host_id"] != host_id:
                return out
            seq = self._conn.execute("SELECT COALESCE(MAX(seq), 0) FROM claude_events WHERE job_id=?", (job_id,)).fetchone()[0]
            self._conn.execute("BEGIN")
            try:
                for ev in events:
                    seq += 1
                    self._conn.execute("INSERT INTO claude_events(job_id, seq, ts, data) VALUES (?,?,?,?)",
                                       (job_id, seq, now, json.dumps(ev, separators=(",", ":"))))
                    out.append((seq, ev))
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        return out

    def job_events(self, job_id: int, after: int = 0, limit: int = 5000) -> list[dict]:
        with self._lock:
            rows = self._conn.execute("SELECT seq, ts, data FROM claude_events WHERE job_id=? AND seq>? ORDER BY seq LIMIT ?",
                                      (job_id, after, limit)).fetchall()
        return [{"seq": r["seq"], "ts": r["ts"], **json.loads(r["data"])} for r in rows]

    def job_prune(self, days: int, now: float | None = None) -> int:
        now = now or time.time()
        with self._lock:
            return self._conn.execute("DELETE FROM claude_jobs WHERE status IN ('done','failed','cancelled') AND created < ?", (now - days * 86400,)).rowcount

    # ------------------------------------------------------------- checks
    CHECK_EDITABLE = ("label", "expires", "notify", "notify_cwd", "notify_log", "cooldown_min", "daily_cap", "target", "owner",
                      "alert_mode")
    CHECK_STATE = ("state", "since", "last_ok", "detail", "last_fired", "fires_day", "fires_n", "open_job", "pending",
                   "known_state", "fire_log", "hold_reason")

    def checks_list(self, host_id: int | None = None) -> list[dict]:
        sql = "SELECT * FROM host_checks" + (" WHERE host_id=?" if host_id is not None else "") + " ORDER BY host_id, key, target"
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, [host_id] if host_id is not None else []).fetchall()]

    def check_get(self, cid: int) -> Optional[dict]:
        with self._lock:
            return _row(self._conn.execute("SELECT * FROM host_checks WHERE id=?", (cid,)).fetchone())

    def check_add(self, host_id: int, key: str, target: str, fields: dict, created_by: str | None) -> dict:
        """Add a check (or, if this host already has key+target, update its editable fields)."""
        fields = {k: v for k, v in fields.items() if k in self.CHECK_EDITABLE and k != "target"}
        with self._lock:
            row = self._conn.execute("SELECT id FROM host_checks WHERE host_id=? AND key=? AND target=?", (host_id, key, target)).fetchone()
            if row is None:
                cols = ["host_id", "key", "target", "created", "created_by"] + list(fields)
                cur = self._conn.execute(f"INSERT INTO host_checks({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                                         [host_id, key, target, time.time(), created_by, *fields.values()])
                cid = cur.lastrowid
            else:
                cid = row["id"]
                if fields:
                    self._conn.execute(f"UPDATE host_checks SET {', '.join(f'{k}=?' for k in fields)} WHERE id=?", [*fields.values(), cid])
            return _row(self._conn.execute("SELECT * FROM host_checks WHERE id=?", (cid,)).fetchone())

    def check_update(self, cid: int, fields: dict, host_id: int | None = None) -> Optional[dict]:
        fields = {k: v for k, v in fields.items() if k in self.CHECK_EDITABLE + self.CHECK_STATE}
        with self._lock:
            row = self._conn.execute("SELECT host_id FROM host_checks WHERE id=?", (cid,)).fetchone()
            if row is None or (host_id is not None and row["host_id"] != host_id):
                return None
            if fields:
                self._conn.execute(f"UPDATE host_checks SET {', '.join(f'{k}=?' for k in fields)} WHERE id=?", [*fields.values(), cid])
            return _row(self._conn.execute("SELECT * FROM host_checks WHERE id=?", (cid,)).fetchone())

    def check_fire(self, cid: int, fields: dict, **job) -> Optional[dict]:
        """Queue a check's responder job (job_create kwargs) and claim the check for it, atomically: only if the check
        has no open job, so two reports for one host evaluated at once can't both fire. Also stores `fields` (fire
        bookkeeping). Returns the job, or None when another report got there first."""
        fields = {k: v for k, v in fields.items() if k in self.CHECK_STATE and k != "open_job"}
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                j = self.job_create(**job)
                cur = self._conn.execute(
                    f"UPDATE host_checks SET {', '.join(['open_job=?'] + [f'{k}=?' for k in fields])} WHERE id=? AND open_job IS NULL",
                    [j["id"], *fields.values(), cid])
                if cur.rowcount != 1:
                    self._conn.execute("ROLLBACK")
                    return None
                self._conn.execute("COMMIT")
                return j
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def check_delete(self, cid: int, host_id: int | None = None) -> Optional[dict]:
        with self._lock:
            row = _row(self._conn.execute("SELECT * FROM host_checks WHERE id=?", (cid,)).fetchone())
            if row is None or (host_id is not None and row["host_id"] != host_id):
                return None
            self._conn.execute("DELETE FROM host_checks WHERE id=?", (cid,))
            return row

    def checks_expired(self, now: float | None = None) -> list[dict]:
        now = now or time.time()
        with self._lock:
            return [dict(r) for r in self._conn.execute("SELECT * FROM host_checks WHERE expires IS NOT NULL AND expires < ?", (now,)).fetchall()]

    # ------------------------------------------------------- tracked sessions
    TRACK_SYNC = ("session_id", "name", "cwd", "kind", "live", "state", "status", "detail", "last_said", "last_activity", "remote_control")

    def tracked_list(self, host_id: int | None = None) -> list[dict]:
        sql = "SELECT * FROM claude_tracked" + (" WHERE host_id=?" if host_id is not None else "") + " ORDER BY live DESC, name COLLATE NOCASE"
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, [host_id] if host_id is not None else []).fetchall()]

    def tracked_get(self, tid: int) -> Optional[dict]:
        with self._lock:
            return _row(self._conn.execute("SELECT * FROM claude_tracked WHERE id=?", (tid,)).fetchone())

    def tracked_add(self, host_id: int, key: str, info: dict, added_by: str | None) -> dict:
        """Track a session (idempotent: re-tracking refreshes the given fields)."""
        now = time.time()
        fields = {k: info.get(k) for k in ("session_id", "name", "cwd", "kind", "note") if info.get(k) is not None}
        with self._lock:
            row = self._conn.execute("SELECT id FROM claude_tracked WHERE host_id=? AND key=?", (host_id, key)).fetchone()
            if row is None:
                cols = ["host_id", "key", "added", "added_by"] + list(fields)
                cur = self._conn.execute(f"INSERT INTO claude_tracked({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
                                         [host_id, key, now, added_by] + list(fields.values()))
                tid = cur.lastrowid
            else:
                tid = row["id"]
                if fields:
                    self._conn.execute(f"UPDATE claude_tracked SET {', '.join(f'{k}=?' for k in fields)} WHERE id=?", [*fields.values(), tid])
            return _row(self._conn.execute("SELECT * FROM claude_tracked WHERE id=?", (tid,)).fetchone())

    def tracked_delete(self, tid: int, host_id: int | None = None) -> bool:
        with self._lock:
            sql, vals = "DELETE FROM claude_tracked WHERE id=?", [tid]
            if host_id is not None:
                sql += " AND host_id=?"; vals.append(host_id)
            return self._conn.execute(sql, vals).rowcount > 0

    def tracked_find(self, host_id: int, key: str) -> Optional[dict]:
        with self._lock:
            return _row(self._conn.execute("SELECT * FROM claude_tracked WHERE host_id=? AND (key=? OR session_id=?)",
                                           (host_id, key, key)).fetchone())

    def tracked_rekey(self, host_id: int, old_key: str, new_key: str) -> Optional[dict]:
        """A restart that came back as a new background agent: follow it (unless the new key is already tracked)."""
        with self._lock:
            if self._conn.execute("SELECT 1 FROM claude_tracked WHERE host_id=? AND key=?", (host_id, new_key)).fetchone():
                return None
            self._conn.execute("UPDATE claude_tracked SET key=?, kind='background' WHERE host_id=? AND key=?", (new_key, host_id, old_key))
            return _row(self._conn.execute("SELECT * FROM claude_tracked WHERE host_id=? AND key=?", (host_id, new_key)).fetchone())

    TRACK_TEXT = ("detail", "last_said")   # conversation text a tracked row keeps between reports

    def tracked_sync(self, host_id: int, agents: list, now: float | None = None, scrub: bool = False) -> list[dict]:
        """Refresh tracked rows from an agent report's `claude.agents`; returns the rows that changed.
        scrub (a data-use host, datause.restricted) or a row the agent marks "redacted" (its snippets stay on the host:
        AVM_SESSION_SNIPPETS off, or it works in an AVM_TASK_LOG_OFF_DIRS dir): the row's stored text is cleared, not
        kept - otherwise what was stored before the restriction stays on show for good."""
        now = now or time.time()
        with self._lock:
            rows = [dict(r) for r in self._conn.execute("SELECT * FROM claude_tracked WHERE host_id=?", (host_id,)).fetchall()]
            if not rows:
                return []
            by_key = {a.get("key"): a for a in agents if isinstance(a, dict) and a.get("key")}
            by_sid = {a.get("session_id"): a for a in agents if isinstance(a, dict) and a.get("session_id")}
            changed = []
            for r in rows:
                a = by_key.get(r["key"]) or (by_sid.get(r["session_id"]) if r["session_id"] else None)
                forget = scrub or bool(a and a.get("redacted"))
                if a is None:
                    upd = {"live": 0} if r["live"] else {}   # no longer reported at all: certainly not running
                else:
                    upd = {}
                    for k in self.TRACK_SYNC:
                        if forget and k in self.TRACK_TEXT:
                            continue   # cleared below
                        v = a.get(k)
                        if k in ("live", "remote_control"):
                            if k == "remote_control" and v is None:
                                continue   # agent < 1.7.2: unknown, keep what we knew
                            v = 1 if v else 0
                        elif isinstance(v, str):
                            v = (datause.last_part(v) if forget and k == "cwd" else v)[:500]
                        if v is None and k in ("name", "cwd", "session_id", "kind", "detail", "last_said"):
                            continue   # keep what we knew
                        if v != r[k]:
                            upd[k] = v
                    if a.get("live") and (not r["last_live"] or now - r["last_live"] > 60):
                        upd["last_live"] = now   # throttled: a heartbeat, not a change worth broadcasting
                if forget:
                    upd.update({k: None for k in self.TRACK_TEXT if r[k] is not None})
                    if "cwd" not in upd and r["cwd"] and datause.last_part(r["cwd"]) != r["cwd"]:
                        upd["cwd"] = datause.last_part(r["cwd"])   # the last part tells rows apart; the path can name the data
                if upd:
                    self._conn.execute(f"UPDATE claude_tracked SET {', '.join(f'{k}=?' for k in upd)} WHERE id=?", [*upd.values(), r["id"]])
                    if set(upd) - {"last_live", "last_activity"}:
                        changed.append({**r, **upd})
            return changed

    def datause_purge(self, host_id: int) -> tuple[list[dict], list[dict]]:
        """A host just became a data-use host (AVM_DATA_USE=restricted first stored, main.py ingest): clear what the hub
        holds from before, as if it had been restricted all along (datause.py). Its tasks lose command, cwd, note and
        output lines and keep only datause.META_KEEP of their meta; its prompts' stored events are reduced to what
        AVM_JOB_TRANSCRIPT=summary sends (datause.scrub_event); tracked rows lose their text and all but the last part of
        their cwd. One transaction. Returns the (tasks, tracked rows) that changed, for broadcasting."""
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                tasks = []
                for r in self._conn.execute("SELECT * FROM tasks WHERE host_id=?", (host_id,)).fetchall():
                    meta = json.loads(r["meta"]) if r["meta"] else None
                    kept = datause.keep_meta(meta)
                    if r["command"] is None and r["cwd"] is None and r["note"] is None and not r["log"] and kept == meta:
                        continue
                    self._conn.execute("UPDATE tasks SET command=NULL, cwd=NULL, note=NULL, log='', meta=? WHERE id=?",
                                       (json.dumps(kept) if kept else None, r["id"]))
                    tasks.append(_row(self._conn.execute("SELECT * FROM tasks WHERE id=?", (r["id"],)).fetchone()))
                for r in self._conn.execute("SELECT e.job_id, e.seq, e.data FROM claude_events e JOIN claude_jobs j ON j.id=e.job_id"
                                            " WHERE j.host_id=?", (host_id,)).fetchall():
                    ev = json.loads(r["data"])
                    small = datause.scrub_event(ev)
                    if small != ev:
                        self._conn.execute("UPDATE claude_events SET data=? WHERE job_id=? AND seq=?",
                                           (json.dumps(small, separators=(",", ":")), r["job_id"], r["seq"]))
                tracked = []
                for r in self._conn.execute("SELECT * FROM claude_tracked WHERE host_id=?", (host_id,)).fetchall():
                    upd = {k: None for k in self.TRACK_TEXT if r[k] is not None}
                    if r["cwd"] and datause.last_part(r["cwd"]) != r["cwd"]:
                        upd["cwd"] = datause.last_part(r["cwd"])
                    if upd:
                        self._conn.execute(f"UPDATE claude_tracked SET {', '.join(f'{k}=?' for k in upd)} WHERE id=?", [*upd.values(), r["id"]])
                        tracked.append({**dict(r), **upd})
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        return tasks, tracked

    # ---------------------------------------------------------------- prefs
    def prefs_get(self, user: str) -> dict:
        with self._lock:
            row = self._conn.execute("SELECT data FROM prefs WHERE user=?", (user,)).fetchone()
        return json.loads(row["data"]) if row else {}

    def prefs_set(self, user: str, data: dict) -> None:
        with self._lock:
            self._conn.execute("INSERT OR REPLACE INTO prefs(user, data, updated) VALUES (?,?,?)",
                               (user, json.dumps(data, separators=(",", ":")), time.time()))

    def stats(self) -> dict[str, Any]:
        with self._lock:
            raw = self._conn.execute("SELECT COUNT(*) AS n FROM samples").fetchone()["n"]
            r1m = self._conn.execute("SELECT COUNT(*) AS n FROM samples_1m").fetchone()["n"]
            hosts = self._conn.execute("SELECT COUNT(*) AS n FROM hosts").fetchone()["n"]
            tasks = self._conn.execute("SELECT COUNT(*) AS n FROM tasks").fetchone()["n"]
            jobs = self._conn.execute("SELECT COUNT(*) AS n FROM claude_jobs").fetchone()["n"]
        size = Path(self.path).stat().st_size if Path(self.path).exists() else 0
        return {"hosts": hosts, "raw_rows": raw, "rollup_rows": r1m, "tasks": tasks, "claude_jobs": jobs, "db_bytes": size}
