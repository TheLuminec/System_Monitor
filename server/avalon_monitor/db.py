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
    notes         TEXT
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
"""


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

    @classmethod
    def from_row(cls, r: sqlite3.Row) -> "HostRow":
        return cls(
            id=r["id"], name=r["name"], display_name=r["display_name"],
            enabled=bool(r["enabled"]), created_at=r["created_at"],
            last_seen=r["last_seen"], last_ip=r["last_ip"],
            last_sample=json.loads(r["last_sample"]) if r["last_sample"] else None,
            notes=r["notes"],
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

    def stats(self) -> dict[str, Any]:
        with self._lock:
            raw = self._conn.execute("SELECT COUNT(*) AS n FROM samples").fetchone()["n"]
            r1m = self._conn.execute("SELECT COUNT(*) AS n FROM samples_1m").fetchone()["n"]
            hosts = self._conn.execute("SELECT COUNT(*) AS n FROM hosts").fetchone()["n"]
        size = Path(self.path).stat().st_size if Path(self.path).exists() else 0
        return {"hosts": hosts, "raw_rows": raw, "rollup_rows": r1m, "db_bytes": size}
