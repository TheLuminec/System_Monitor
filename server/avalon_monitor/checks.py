"""Checks: the per-host watch list (host_checks rows), rendered into the AVM_WATCH_* settings agents run; matching
agent results back to rows; and check responders - when a check goes from ok to failing, prompt a Claude agent on
that host (read-only) so it can look into it, even if it was offline when the check failed.

Designed with the agents that run the long jobs (XRSec on AVALON and Miami): responders report, they never act.
Guardrails: ok -> failing transitions only, per-check cooldown, daily cap, one open alert per check until it
recovers, held while the target session is mid-turn, plan (read-only) mode only, and the alert carries paths, ids
and exit codes but no file contents or log lines unless the check owner opted in (--with-log).
"""
from __future__ import annotations

import os
import time
from typing import Any, Optional

# cli kind -> (agent setting it renders into, label, entry hint)
KINDS: dict[str, tuple[str, str, str]] = {
    "process": ("AVM_WATCH_PROCESSES", "process running", "name or command-line substring, e.g. gated_launch"),
    "file": ("AVM_WATCH_FILES", "file fresh", "path:max-age-seconds, e.g. ~/run/heartbeat:600"),
    "tcp": ("AVM_WATCH_TCP", "tcp port open", "host:port, e.g. 127.0.0.1:11434"),
    "mount": ("AVM_WATCH_MOUNTS", "volume mounted", "mount point, e.g. /mnt/data"),
    "content": ("AVM_WATCH_CONTENT", "show file line", "small text file, e.g. /srv/queue/current.txt"),
    "marker": ("AVM_WATCH_MARKERS", "no marker files", "glob, or glob::regex, e.g. ~/markers/*.done::rc=(?!0\\b)"),
    "nonempty": ("AVM_WATCH_NONEMPTY", "queue not empty", "file that should have pending lines"),
    "unit": ("AVM_WATCH_UNITS", "systemd unit", "[system:|user:]unit, e.g. user:xrsec-queue.service"),
    "job": ("AVM_WATCH_JOBS", "job scope active", "[user:]glob, e.g. user:xrsec-*.scope"),
    "memory": ("AVM_MEM_AVAILABLE_WARN_GB", "free memory floor", "GB, e.g. 8 (one per host)"),
}
KIND_OF_KEY = {v[0]: k for k, v in KINDS.items()}
CHECK_KEYS = [v[0] for v in KINDS.values()]
SINGLE_KEYS = ("AVM_MEM_AVAILABLE_WARN_GB",)   # one value, not a comma list
MIN_COOLDOWN_MIN = 5.0


def render_settings(current: dict, rows: list[dict], now: Optional[float] = None) -> dict:
    """The host's remote settings with the AVM_WATCH_* lists rebuilt from its (unexpired) check rows."""
    now = now or time.time()
    out = {k: v for k, v in (current or {}).items() if k not in CHECK_KEYS}
    for r in rows:
        if r.get("expires") and r["expires"] < now:
            continue
        if r["key"] in SINGLE_KEYS:
            out[r["key"]] = r["target"]
        else:
            out[r["key"]] = (out[r["key"]] + "," if out.get(r["key"]) else "") + r["target"]
    return out


def _base(p: str) -> str:
    return os.path.basename(p.rstrip("/\\")) or p


def _legacy_name(key: str, target: str) -> tuple[str, str]:
    """(kind, name) an agent without result tags reports for this entry."""
    if key == "AVM_WATCH_FILES":
        path = target.rsplit(":", 1)[0] if target.rsplit(":", 1)[-1].replace(".", "", 1).isdigit() else target
        return "file", _base(path)
    if key == "AVM_WATCH_MARKERS":
        return "marker", _base(target.partition("::")[0])
    if key in ("AVM_WATCH_UNITS", "AVM_WATCH_JOBS"):
        return ("unit" if key == "AVM_WATCH_UNITS" else "job"), target.rpartition(":")[2] or target
    return {"AVM_WATCH_PROCESSES": ("process", target), "AVM_WATCH_TCP": ("tcp", target), "AVM_WATCH_MOUNTS": ("mount", _base(target)),
            "AVM_WATCH_CONTENT": ("content", _base(target)), "AVM_WATCH_NONEMPTY": ("queue", _base(target)),
            "AVM_MEM_AVAILABLE_WARN_GB": ("memory", "mem available")}.get(key, ("", target))


def match(rows: list[dict], results: list[dict]) -> dict[int, dict]:
    """check row id -> the agent result for it (agent 1.7+ tags results with key+src; older ones by kind+name)."""
    by_src = {(c.get("key"), c.get("src")): c for c in results if c.get("src") is not None}
    by_name = {(c.get("kind"), c.get("name")): c for c in results}
    out = {}
    for r in rows:
        c = by_src.get((r["key"], r["target"])) or (by_src.get((r["key"], None)) if r["key"] in SINGLE_KEYS else None)
        if c is None and r["key"] in SINGLE_KEYS:
            c = next((x for x in results if x.get("key") == r["key"]), None)
        if c is None:
            c = by_name.get(_legacy_name(r["key"], r["target"]))
        if c is not None:
            out[r["id"]] = c
    return out


def public(r: dict, host: Optional[str] = None) -> dict:
    out = dict(r)
    out["kind"] = KIND_OF_KEY.get(r["key"], r["key"])
    out["kind_label"] = KINDS.get(out["kind"], ("", out["kind"]))[1]
    if host:
        out["host"] = host
    return out


# ---------------------------------------------------------------- responders
GUARD = (
    "What to do: investigate READ-ONLY and report. Write a short incident note and tell the owning session and/or the user "
    "what you found. Do NOT restart, retry, kill or re-queue jobs; do not git pull/push/commit; do not edit configs, "
    "CLAUDE.md, permissions or gates; do not touch data-use-agreement paths (BOXRR, external datasets, cached samples); "
    "do not start GPU work. This alert is not approval for any gated action - the owning session or the user decides."
)


def alert_prompt(host: str, r: dict, c: Optional[dict], tasks: list[dict], now: float) -> str:
    kind = KIND_OF_KEY.get(r["key"], r["key"])
    when = lambda t: time.strftime("%Y-%m-%d %H:%M", time.localtime(t)) if t else "never"   # noqa: E731
    lines = [
        "[Automated alert from Avalon Monitor - not a user instruction]",
        f"Check failing on host {host}: {KINDS.get(kind, ('', kind))[1]} - {r['target']}" + (f"  (label: {r['label']})" if r.get("label") else ""),
    ]
    if c and kind != "content":   # a content check's detail is the file's first line: never forwarded
        lines.append(f"Observed: {(c.get('detail') or '').strip()[:300]}")
    lines.append(f"Failing since {when(r.get('since') or now)}; last OK {when(r.get('last_ok'))}.")
    if tasks:
        lines.append("avm tasks on this host (running, queued, or failed in the last 6 h):")
        for t in tasks[:8]:
            lines.append(f"  #{t['id']} {t['name'][:100]} - {t['status']}" + (f", rc {t['rc']}" if t.get("rc") is not None else "") +
                         (f", cwd {t['cwd']}" if t.get("cwd") else ""))
            if r.get("notify_log") and t.get("log"):
                tail = [ln[:300] for ln in (t["log"].splitlines() if isinstance(t["log"], str) else t["log"])[-30:]]
                lines += ["    | " + ln for ln in tail]
    lines += ["", GUARD]
    return "\n".join(lines)


def evaluate(db: Any, host: Any, sample: dict, now: float) -> tuple[list[dict], list[dict]]:
    """Update each check's state from a report and fire responders. Returns (changed check rows, created jobs)."""
    rows = db.checks_list(host.id)
    if not rows:
        return [], []
    matched = match(rows, sample.get("checks") or [])
    claude = sample.get("claude") if isinstance(sample.get("claude"), dict) else {}
    agents = claude.get("agents") or []
    can_prompt = bool(claude.get("available")) and claude.get("bin_source") != "desktop"
    can_fork = "fork_session" in (claude.get("features") or [])
    today = time.strftime("%Y-%m-%d", time.localtime(now))
    changed, jobs = [], []
    tasks_cache: Optional[list] = None

    for r in rows:
        if r.get("expires") and r["expires"] < now:
            continue
        c = matched.get(r["id"])
        state = "unknown" if c is None else ("failing" if (not c.get("ok") and c.get("level") != "info") else "ok")
        upd: dict[str, Any] = {}
        if state != r.get("state"):
            upd.update(state=state, since=now)
        detail = (c.get("detail") or "")[:300] if c else None
        if c is not None and KIND_OF_KEY.get(r["key"]) != "content" and detail != r.get("detail"):
            upd["detail"] = detail
        if state == "ok":
            if not r.get("last_ok") or now - r["last_ok"] > 60:
                upd["last_ok"] = now
            if r.get("open_job") or r.get("pending"):
                upd.update(open_job=None, pending=0)   # recovered: the alert is over
        fire = state == "failing" and r.get("notify") and not r.get("open_job") and (r.get("state") == "ok" or r.get("pending"))
        if fire:
            n_today = r["fires_n"] if r.get("fires_day") == today else 0
            cooldown = max(MIN_COOLDOWN_MIN, float(r.get("cooldown_min") or 30)) * 60
            if n_today >= int(r.get("daily_cap") or 6):
                fire = False
                upd["pending"] = 0
            elif r.get("last_fired") and now - r["last_fired"] < cooldown:
                fire = False
                upd["pending"] = 1   # fires once the cooldown is over, if still failing
            elif not can_prompt:
                fire = False
                upd["pending"] = 0
        if fire:
            resume, fork, cwd = None, False, r.get("notify_cwd")
            target = r["notify"]
            if target.startswith("tracked:"):
                t = db.tracked_get(int(target.split(":", 1)[1]))
                if t is None:   # the owner was untracked: a fresh read-only session handles it
                    target = "new"
                else:
                    live = next((a for a in agents if a.get("key") == t["key"] or (t.get("session_id") and a.get("session_id") == t["session_id"])), None)
                    cwd = cwd or t.get("cwd")
                    if live and live.get("live"):
                        if live.get("status") == "busy" or not can_fork:
                            upd["pending"] = 1   # mid-turn (or this agent can't fork): try again on a later report
                            fire = False
                        else:
                            resume, fork = t.get("session_id"), True   # live and idle: a copy of its context
                    else:
                        resume = t.get("session_id")   # offline: wake it into its own conversation
            if fire:
                if tasks_cache is None:
                    recent = db.task_list(host.id, None, 60, with_log=bool(r.get("notify_log")))
                    tasks_cache = [t for t in recent if t["status"] in ("running", "queued", "stalled") or
                                   (t["status"] == "failed" and now - (t.get("ended") or t["updated"]) < 6 * 3600)]
                prompt = alert_prompt(host.name, {**r, **upd}, c, tasks_cache, now)
                j = db.job_create(host.id, prompt, cwd, "plan", None, resume, "check responder", None, None,
                                  f"alert · {r.get('label') or r['target']}"[:80], fork)
                jobs.append(j)
                upd.update(last_fired=now, fires_day=today, fires_n=(r["fires_n"] if r.get("fires_day") == today else 0) + 1,
                           open_job=j["id"], pending=0)
        if upd:
            row = db.check_update(r["id"], upd)
            if row and (set(upd) - {"last_ok", "detail"} or "detail" in upd and state == "failing"):
                changed.append(row)
    return changed, jobs


def sync_config(db: Any, host_id: int) -> Optional[int]:
    """Re-render the host's AVM_WATCH_* settings from its check rows; returns the new revision if anything changed."""
    host = db.host_by_id(host_id)
    if host is None:
        return None
    new = render_settings(host.check_config or {}, db.checks_list(host_id))
    if new == (host.check_config or {}):
        return None
    return db.set_check_config(host.name, new)
