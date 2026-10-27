#!/usr/bin/python3 -I
"""Nightly restic backup that reports its state to Avalon Monitor.

Runs as root from a systemd timer (installed by install-restic-backup.sh, wrapped in `avm run` so each run is a task on
the dashboard). Reads /etc/avalon-backup/config.json, then:
  1. writes status "running" to the status file
  2. takes consistent copies of SQLite databases listed in "sqlite" (the live files should be excluded)
  3. restic backup (JSON output, turned into a readable progress line every 30 s for the dashboard log)
  4. restic forget --prune with the "keep" policy; on "check_weekday" also restic check --read-data-subset
  5. writes the status file (read by the agent, shown as the host's Backups card) and touches the success stamp

The status file is world-readable JSON - schema in docs/HOSTS.md ("Backups"). It never holds the password or file
contents: only counts, sizes, times, the repository location and the backed-up paths.
"""
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from collections import deque

CONFIG = os.environ.get("AVM_BACKUP_CONFIG", "/etc/avalon-backup/config.json")
DEFAULTS = {
    "name": "nightly backup", "tag": "nightly", "one_file_system": True, "exclude": [], "sqlite": [],
    "keep": {"daily": 7, "weekly": 4, "monthly": 6}, "check_weekday": 7, "check_subset": "5%",
    "max_age_sec": 93600, "status_file": "/var/lib/avalon-backup/status.json",
    "stamp_file": "/var/lib/avalon-backup/last-success", "cache_dir": "/var/cache/restic", "timer": None, "require_mount": None,
}


def say(msg):
    print(msg, flush=True)


def write_json(path, data):
    os.makedirs(os.path.dirname(path), mode=0o755, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1)
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def gib(n):
    return "%.1f GiB" % ((n or 0) / 2**30)


def hms(sec):
    sec = int(sec or 0)
    return "%d:%02d:%02d" % (sec // 3600, sec % 3600 // 60, sec % 60)


def err_text(m):
    """restic 0.16 often serialises the Go error as {} - fall back to what it was doing."""
    e = m.get("error")
    msg = e.get("message") if isinstance(e, dict) else e
    return msg or "could not be read (during %s)" % (m.get("during") or "backup")


def dir_bytes(path):
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                pass
    return total


def next_run(timer):
    if not timer:
        return None
    try:
        out = subprocess.run(["systemctl", "list-timers", timer, "--output=json", "--no-pager"], capture_output=True, text=True, timeout=10).stdout
        nxt = (json.loads(out) or [{}])[0].get("next")
        return nxt / 1e6 if nxt else None
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return None


class Restic:
    def __init__(self, cfg):
        self.env = dict(os.environ, RESTIC_REPOSITORY=cfg["repository"], RESTIC_PASSWORD_FILE=cfg["password_file"],
                        RESTIC_CACHE_DIR=cfg["cache_dir"], RESTIC_PROGRESS_FPS="0.2")
        self.stderr_tail = deque(maxlen=5)
        self.errors = []      # restic --json "error" messages (unreadable files), from stdout or stderr

    def run(self, args, on_json=None):
        """Run restic; stream stdout (JSON lines to on_json when given, else echoed), echo stderr and keep its tail."""
        p = subprocess.Popen(["restic", *args], env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)

        def pump_err():
            for line in p.stderr:
                line = line.rstrip()
                if not line:
                    continue
                try:
                    m = json.loads(line)
                except ValueError:
                    m = None
                if isinstance(m, dict) and m.get("message_type") == "error":
                    self.errors.append(m)
                    line = "warning: %s: %s" % (m.get("item", "?"), err_text(m))
                self.stderr_tail.append(line)
                print(line, file=sys.stderr, flush=True)
        t = threading.Thread(target=pump_err, daemon=True)
        t.start()
        for line in p.stdout:
            line = line.rstrip()
            if on_json is not None:
                try:
                    on_json(json.loads(line))
                    continue
                except ValueError:
                    pass
            if line:
                say(line)
        p.wait()
        t.join(5)
        return p.returncode

    def json(self, args, timeout=600):
        try:
            out = subprocess.run(["restic", *args, "--json"], env=self.env, capture_output=True, text=True, timeout=timeout)
            return json.loads(out.stdout) if out.returncode == 0 and out.stdout.strip() else None
        except (subprocess.SubprocessError, ValueError):
            return None


def snapshot_time(s):
    """restic's RFC 3339 time with nanoseconds -> epoch (fromisoformat takes at most microseconds)."""
    import datetime
    import re
    t = re.sub(r"(\.\d{6})\d+", r"\1", s.get("time", ""))
    try:
        return datetime.datetime.fromisoformat(t).timestamp()
    except ValueError:
        return None


def seed(cfg):
    """Write the status file from what the repository already holds (no backup): for a repository that existed before
    this runner, so the dashboard card has data before the next scheduled run. Quick (snapshot list + repository size);
    --count also counts the latest snapshot's files, which reads every tree in it (minutes for ~1M files)."""
    if cfg["require_mount"] and not os.path.ismount(cfg["require_mount"]):
        say(f"{cfg['require_mount']} is not mounted"); return 2
    restic = Restic(cfg)
    say("reading the snapshot list ...")
    snaps = restic.json(["snapshots", "--tag", cfg["tag"]], timeout=300) or []
    if not snaps:
        say("no snapshots with tag %r yet (or the repository could not be read) - nothing to seed" % cfg["tag"]); return 1
    latest = max(snaps, key=lambda s: s.get("time", ""))
    stats = {}
    if "--count" in sys.argv[1:]:
        say("counting the files in snapshot %s (can take several minutes) ..." % latest.get("short_id"))
        stats = restic.json(["stats", "latest", "--tag", cfg["tag"]], timeout=3600) or {}
    say("measuring the repository ...")
    when = snapshot_time(latest)
    try:
        when = os.stat(cfg["stamp_file"]).st_mtime   # the end of the last successful run, when there is one
    except OSError:
        pass
    status = {"v": 1, "name": cfg["name"], "tool": "restic", "repository": cfg["repository"], "paths": cfg["paths"],
              "keep": cfg["keep"], "max_age_sec": cfg["max_age_sec"], "state": "ok", "finished": when, "last_success": when,
              "snapshot": latest.get("short_id") or latest.get("id", "")[:8], "files": stats.get("total_file_count"),
              "bytes_scanned": stats.get("total_size"), "snapshots": len(snaps), "error": None,
              "repo_bytes": dir_bytes(cfg["repository"]), "target_free_bytes": shutil.disk_usage(cfg["repository"]).free,
              "next_run": next_run(cfg["timer"]), "seeded": True}
    write_json(cfg["status_file"], status)
    say("status seeded from snapshot %s (%d snapshots, repository %s) - the card shows it within a few seconds"
        % (status["snapshot"], len(snaps), gib(status["repo_bytes"])))
    return 0


def main():
    with open(CONFIG) as f:
        cfg = {**DEFAULTS, **json.load(f)}
    if "--seed" in sys.argv[1:]:
        return seed(cfg)
    try:
        with open(cfg["status_file"]) as f:
            prev = json.load(f)
    except (OSError, ValueError):
        prev = {}
    started = time.time()
    status = {"v": 1, "name": cfg["name"], "tool": "restic", "repository": cfg["repository"], "paths": cfg["paths"],
              "keep": cfg["keep"], "max_age_sec": cfg["max_age_sec"], "state": "running", "started": started,
              "last_success": prev.get("last_success"), "next_run": next_run(cfg["timer"])}
    for k in ("finished", "duration_sec", "snapshot", "files", "bytes_scanned", "bytes_added", "files_new", "files_changed",
              "repo_bytes", "target_free_bytes", "snapshots", "warnings"):
        if k in prev:
            status[k] = prev[k]   # keep the last run's numbers visible while this one runs
    write_json(cfg["status_file"], status)
    restic = Restic(cfg)

    def fail(msg, rc):
        say("backup FAILED: " + msg)
        status.update(state="failed", finished=time.time(), duration_sec=round(time.time() - started),
                      error=(msg + (" | " + restic.stderr_tail[-1] if restic.stderr_tail else ""))[:400], next_run=next_run(cfg["timer"]))
        write_json(cfg["status_file"], status)
        return rc

    if cfg["require_mount"] and not os.path.ismount(cfg["require_mount"]):
        return fail(f"{cfg['require_mount']} is not mounted - nothing backed up", 2)

    for src, dst in cfg["sqlite"]:
        try:
            import sqlite3
            os.makedirs(os.path.dirname(dst), mode=0o700, exist_ok=True)
            s = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
            d = sqlite3.connect(dst)
            s.backup(d)
            d.close(); s.close()
        except Exception as e:   # a missing DB must not stop the backup of everything else
            say(f"warning: snapshot of {src} failed: {e}")

    summary, last_print, errors = {}, [0.0], restic.errors

    def on_backup(m):
        kind = m.get("message_type")
        if kind == "status":
            if time.time() - last_print[0] >= 30:
                last_print[0] = time.time()
                say("[%s] %.1f%%  %s/%s files  %s of %s  ETA %s" % (
                    hms(m.get("seconds_elapsed")), 100 * (m.get("percent_done") or 0), m.get("files_done", 0), m.get("total_files", 0),
                    gib(m.get("bytes_done")), gib(m.get("total_bytes")), hms(m.get("seconds_remaining"))))
        elif kind == "error":
            errors.append(m)
            say("warning: %s: %s" % (m.get("item", "?"), err_text(m)))
        elif kind == "summary":
            summary.update(m)

    args = ["backup", "--json", "--exclude-caches", "--tag", cfg["tag"]]
    if cfg["one_file_system"]:
        args.append("--one-file-system")
    for x in cfg["exclude"]:
        args += ["--exclude", x]
    backup_rc = restic.run(args + list(cfg["paths"]), on_json=on_backup)
    if backup_rc not in (0, 3) or not summary.get("snapshot_id"):
        return fail(f"restic backup exited {backup_rc}", backup_rc or 1)
    say("snapshot %s saved: %d files, %s scanned, %s new (%d new, %d changed files) in %s%s" % (
        summary["snapshot_id"][:8], summary.get("total_files_processed", 0), gib(summary.get("total_bytes_processed")),
        gib(summary.get("data_added")), summary.get("files_new", 0), summary.get("files_changed", 0), hms(summary.get("total_duration")),
        f", {len(errors) or 'some'} files unreadable" if backup_rc == 3 or errors else ""))

    keep = cfg["keep"]
    rc = restic.run(["forget", "--tag", cfg["tag"], "--prune", "--keep-daily", str(keep.get("daily", 7)),
                     "--keep-weekly", str(keep.get("weekly", 4)), "--keep-monthly", str(keep.get("monthly", 6))])
    if rc != 0:
        return fail(f"restic forget/prune exited {rc}", rc)
    if cfg["check_weekday"] and time.localtime().tm_wday + 1 == int(cfg["check_weekday"]):
        rc = restic.run(["check", "--read-data-subset=" + cfg["check_subset"]])
        if rc != 0:
            return fail(f"restic check exited {rc} - the repository may be damaged", rc)
        status["checked"] = time.time()

    snaps = restic.json(["snapshots", "--tag", cfg["tag"]]) or []
    now = time.time()
    status.update(state="ok", finished=now, duration_sec=round(now - started), last_success=now, error=None,
                  snapshot=summary["snapshot_id"][:8], files=summary.get("total_files_processed"),
                  bytes_scanned=summary.get("total_bytes_processed"), bytes_added=summary.get("data_added"),
                  files_new=summary.get("files_new"), files_changed=summary.get("files_changed"),
                  warnings=len(errors) or (1 if backup_rc == 3 else 0), snapshots=len(snaps),
                  repo_bytes=dir_bytes(cfg["repository"]) if os.path.isdir(cfg["repository"]) else None,
                  target_free_bytes=shutil.disk_usage(cfg["repository"]).free if os.path.isdir(cfg["repository"]) else None,
                  next_run=next_run(cfg["timer"]))
    write_json(cfg["status_file"], status)
    with open(cfg["stamp_file"], "w") as f:
        f.write(time.strftime("%Y-%m-%dT%H:%M:%S%z\n"))
    os.chmod(cfg["stamp_file"], 0o644)
    say("backup OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
