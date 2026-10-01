"""SQLite storage: host registry, raw samples, 1-minute rollups, retention.

Raw samples are kept at the agent's native interval for `retention_raw_hours`;
a background job folds completed minutes into `samples_1m`, which is kept for
`retention_1m_days`. Series queries pick the table and bucket size from the
requested range so the UI always gets a sane number of points.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

from .models import Sample

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
    status      TEXT NOT NULL,            -- running | done | failed | stalled | lost
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
    note        TEXT
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
    mode            TEXT NOT NULL,        -- plan | acceptEdits | full
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

CREATE TABLE IF NOT EXISTS claude_events (
    job_id  INTEGER NOT NULL REFERENCES claude_jobs(id) ON DELETE CASCADE,
    seq     INTEGER NOT NULL,
    ts      REAL NOT NULL,
    data    TEXT NOT NULL,
    PRIMARY KEY (job_id, seq)
) WITHOUT ROWID;

-- Per-user dashboard preferences (layout, hidden panels, card order).
CREATE TABLE IF NOT EXISTS prefs (
    user    TEXT PRIMARY KEY,
    data    TEXT NOT NULL,
    updated REAL NOT NULL
);
"""

TASK_LOG_LINES = 200
TASK_STATES = ("running", "done", "failed", "stalled", "lost")
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
        self._conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Add columns introduced after the first release (SQLite has no IF NOT EXISTS for columns)."""
        cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(hosts)")}
        for col, ddl in (("check_config", "TEXT"), ("check_rev", "INTEGER NOT NULL DEFAULT 0"), ("pending_cmd", "TEXT")):
            if col not in cols:
                self._conn.execute(f"ALTER TABLE hosts ADD COLUMN {col} {ddl}")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

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
    def rollup(self, now: float | None = None) -> int:
        """Fold complete minutes into samples_1m. Returns number of rollup rows written."""
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
                    (last, cutoff),
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
                    cwd: str | None, pid: int | None, user: str | None, meta: dict | None) -> dict:
        now = time.time()
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO tasks(host_id, name, status, source, started, updated, heartbeat, expect_beat, command, cwd, pid, user, meta)"
                " VALUES (?,?,'running',?,?,?,?,?,?,?,?,?,?)",
                (host_id, name[:200], source, now, now, now, expect_beat, command, cwd, pid, user,
                 json.dumps(meta) if meta else None),
            )
            return _row(self._conn.execute("SELECT * FROM tasks WHERE id=?", (cur.lastrowid,)).fetchone())

    def task_get(self, task_id: int) -> Optional[dict]:
        with self._lock:
            return _row(self._conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())

    def task_update(self, task_id: int, host_id: int | None = None, *, status: str | None = None, progress: float | None = None,
                    rc: int | None = None, log_lines: list[str] | None = None, note: str | None = None, beat: bool = True,
                    pid: int | None = None) -> Optional[dict]:
        """Apply a heartbeat / log / finish update. host_id (when given) must match - agents can only touch their own tasks."""
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
            elif beat and row["status"] == "stalled":
                sets.append("status='running'")
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
            vals.append(task_id)
            self._conn.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id=?", vals)
            return _row(self._conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())

    def task_list(self, host_id: int | None = None, status: str | None = None, limit: int = 200, with_log: bool = False) -> list[dict]:
        cols = "*" if with_log else "id, host_id, name, status, source, started, ended, updated, heartbeat, expect_beat, progress, rc, command, cwd, pid, user, meta, note"
        where, vals = [], []
        if host_id is not None:
            where.append("host_id=?"); vals.append(host_id)
        if status:
            where.append("status=?"); vals.append(status)
        sql = f"SELECT {cols} FROM tasks" + (" WHERE " + " AND ".join(where) if where else "") + \
              " ORDER BY (status IN ('running','stalled')) DESC, started DESC LIMIT ?"
        vals.append(limit)
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, vals).fetchall()]

    def task_delete(self, task_id: int) -> bool:
        with self._lock:
            return self._conn.execute("DELETE FROM tasks WHERE id=?", (task_id,)).rowcount > 0

    def task_mark_stalled(self, now: float | None = None) -> list[dict]:
        """Running tasks whose heartbeat is older than 1.5x their expectation -> stalled. Returns changed rows."""
        now = now or time.time()
        with self._lock:
            rows = self._conn.execute(
                "SELECT id FROM tasks WHERE status='running' AND expect_beat IS NOT NULL AND heartbeat < ? - expect_beat * 1.5", (now,)
            ).fetchall()
            ids = [r["id"] for r in rows]
            if ids:
                self._conn.execute(f"UPDATE tasks SET status='stalled', updated=? WHERE id IN ({','.join('?' * len(ids))})", [now, *ids])
            return [dict(self._conn.execute("SELECT * FROM tasks WHERE id=?", (i,)).fetchone()) for i in ids]

    def task_mark_lost(self, host_id: int, now: float | None = None) -> list[dict]:
        """A host went offline: its running/stalled tasks can no longer report."""
        now = now or time.time()
        with self._lock:
            rows = self._conn.execute("SELECT id FROM tasks WHERE host_id=? AND status IN ('running','stalled')", (host_id,)).fetchall()
            ids = [r["id"] for r in rows]
            if ids:
                self._conn.execute(f"UPDATE tasks SET status='lost', updated=? WHERE id IN ({','.join('?' * len(ids))})", [now, *ids])
            return [dict(self._conn.execute("SELECT * FROM tasks WHERE id=?", (i,)).fetchone()) for i in ids]

    def task_prune(self, days: int, now: float | None = None) -> int:
        now = now or time.time()
        with self._lock:
            return self._conn.execute("DELETE FROM tasks WHERE status IN ('done','failed','lost') AND updated < ?", (now - days * 86400,)).rowcount

    # ---------------------------------------------------------- claude jobs
    def job_create(self, host_id: int, prompt: str, cwd: str | None, mode: str, model: str | None,
                   resume_session: str | None, created_by: str | None) -> dict:
        now = time.time()
        title = prompt.strip().splitlines()[0][:80] if prompt.strip() else "(empty)"
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO claude_jobs(host_id, status, prompt, cwd, mode, model, resume_session, created, created_by, title)"
                " VALUES (?,'queued',?,?,?,?,?,?,?,?)",
                (host_id, prompt, cwd, mode, model, resume_session, now, created_by, title),
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

    def jobs_pending_for(self, host_id: int) -> tuple[list[dict], list[int]]:
        """(queued jobs to hand to the agent, ids of running jobs with cancel requested)."""
        with self._lock:
            queued = [dict(r) for r in self._conn.execute(
                "SELECT id, prompt, cwd, mode, model, resume_session FROM claude_jobs WHERE host_id=? AND status='queued' ORDER BY id", (host_id,)).fetchall()]
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
