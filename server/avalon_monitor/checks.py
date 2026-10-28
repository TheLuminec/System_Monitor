"""Checks: the per-host watch list (host_checks rows), rendered into the AVM_WATCH_* settings agents run; matching
agent results back to rows; and check responders - when a check goes from ok to failing, prompt a Claude agent on
that host (read-only) so it can look into it, even if it was offline when the check failed.

Designed with the agents that run the long jobs (training runs on AVALON and Miami): responders report, they never act.
Guardrails: ok -> failing transitions only, per-check cooldown, a cap per rolling 24 h, one open alert per check until
it recovers, held while the target session is mid-turn, plan (read-only) mode only, never queued for a host whose
agent wouldn't accept a plan prompt (the same routes_ops.host_policy_refusal the dashboard's prompts go through), and
the alert carries paths, ids and exit codes but no file contents or log lines unless the check owner opted in
(--with-log). A held alert stays pending with its reason in hold_reason.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Optional

# cli kind -> (agent setting it renders into, label, entry hint)
KINDS: dict[str, tuple[str, str, str]] = {
    "process": ("AVM_WATCH_PROCESSES", "process running", "name or command-line substring, e.g. train.py"),
    "file": ("AVM_WATCH_FILES", "file fresh", "path:max-age-seconds, e.g. ~/run/heartbeat:600"),
    "tcp": ("AVM_WATCH_TCP", "tcp port open", "host:port, e.g. 127.0.0.1:11434"),
    "mount": ("AVM_WATCH_MOUNTS", "volume mounted", "mount point, e.g. /mnt/data"),
    "content": ("AVM_WATCH_CONTENT", "show file line", "small text file, e.g. /srv/queue/current.txt"),
    "marker": ("AVM_WATCH_MARKERS", "no marker files", "glob, or glob::regex, e.g. ~/markers/*.done::rc=(?!0\\b)"),
    "nonempty": ("AVM_WATCH_NONEMPTY", "queue not empty", "file that should have pending lines"),
    "unit": ("AVM_WATCH_UNITS", "systemd unit", "[system:|user:]unit, e.g. user:train-queue.service"),
    "job": ("AVM_WATCH_JOBS", "job scope active", "[user:]glob, e.g. user:train-*.scope"),
    "memory": ("AVM_MEM_AVAILABLE_WARN_GB", "free memory floor", "GB, e.g. 8 (one per host)"),
}
KIND_OF_KEY = {v[0]: k for k, v in KINDS.items()}
CHECK_KEYS = [v[0] for v in KINDS.values()]
SINGLE_KEYS = ("AVM_MEM_AVAILABLE_WARN_GB",)   # one value, not a comma list
MIN_COOLDOWN_MIN = 5.0


MANAGED = "_checks_managed"   # marker in a host's settings: its watch lists are owned by the hub (agents ignore it)


# Lowest Tailscale version without a known advisory: TS-2026-013 (leaked eMudhra TLS intermediates) is fixed in
# 1.104.2. Raise it when a new bulletin lands; a host's AVM_TAILSCALE_MIN setting overrides it.
TAILSCALE_MIN = "1.104.2"


def tailscale_checks(ts: Optional[dict], minimum: Optional[str]) -> list[dict]:
    """Advisory checks from the agent's Tailscale report. The 'Tailworm' findings (Oct 2026, no fix released yet) need
    Tailscale SSH, app connectors or serve forwarding to a Unix socket - warn while any of those is on."""
    if not isinstance(ts, dict) or not ts.get("version"):
        return []
    floor = (minimum or TAILSCALE_MIN).strip()
    ver = str(ts["version"])
    out = [{"name": "tailscale version", "kind": "rule", "ok": _version(ver) >= _version(floor),
            "detail": f"{ver}" + (f" - below {floor}: update Tailscale (TS-2026-013 and later bulletins)" if _version(ver) < _version(floor) else f" (needs ≥ {floor})")}]
    risky = [(ts.get("ssh"), "Tailscale SSH is on", "tailscale set --ssh=false if this host doesn't need it"),
             (ts.get("app_connector"), "app connector is advertised", "remove the connector capability if unused"),
             (ts.get("serve_unix"), f"serve forwards to a Unix socket ({', '.join(ts.get('serve_unix') or [])[:120]})", "remove forwards to privileged sockets")]
    for on, what, fix in risky:
        if on:
            out.append({"name": "tailscale exposure", "kind": "rule", "ok": False, "level": "warning",
                        "detail": f"{what}: exposed to the unpatched 'Tailworm' findings - {fix}"})
    return out


def _version(v: Optional[str]) -> tuple:
    try:
        return tuple(int(x) for x in str(v or "0").split(".")[:3])
    except ValueError:
        return (0,)


def render_settings(current: dict, rows: list[dict], now: Optional[float] = None) -> dict:
    """The host's remote settings with the AVM_WATCH_* lists rebuilt from its (unexpired) check rows.
    Hub settings replace the same keys in the agent's agent.conf. Once a host is managed (its agent.conf checks were
    adopted as rows) every list is always sent, empty if need be, so a removed check can't fall back to agent.conf."""
    now = now or time.time()
    out = {k: v for k, v in (current or {}).items() if k not in CHECK_KEYS}
    if out.get(MANAGED):
        out.update({k: "" for k in CHECK_KEYS})
    for r in rows:
        if r.get("expires") and r["expires"] < now:
            continue
        if r["key"] in SINGLE_KEYS:
            out[r["key"]] = r["target"]
        else:
            out[r["key"]] = (out[r["key"]] + "," if out.get(r["key"]) else "") + r["target"]
    return out


def declared(cfg: Optional[dict]) -> list[tuple[str, str]]:
    """(key, entry) of every AVM_WATCH_* / memory entry in a host's stored settings."""
    out = []
    for key in CHECK_KEYS:
        value = str((cfg or {}).get(key) or "").strip()
        out += [(key, e) for e in ([value] if key in SINGLE_KEYS else [x.strip() for x in value.split(",")]) if e]
    return out


def adopt(db: Any, host: Any, sample: dict) -> Optional[int]:
    """First report from an agent that tags its results (1.7+): make every check it runs from agent.conf a row, then
    mark the host managed so the rendered lists are complete. Without this, adding one process check from the hub
    would replace (and silently drop) agent.conf's whole process list. Returns the new settings revision, if any.
    Waits for a report with at least one tagged check: the agent sends an empty list when its check collector fails,
    and adopting that would push empty lists over every agent.conf check for good (and a host with no checks at all
    loses nothing by staying unmanaged until its first one).
    Rows come from the report AND from the lists the hub already declares for the host (its stored AVM_WATCH_*
    settings): once managed, the settings are rendered from rows only, so an entry the hub was pushing that the one
    adopted report didn't show (applied late, legacy import skipped it) would otherwise be dropped with its kind."""
    cfg = host.check_config or {}
    if cfg.get(MANAGED) or _version((sample.get("host") or {}).get("agent_version")) < (1, 7):
        return None   # already managed, or an agent that can't say where its checks come from
    results = [c for c in (sample.get("checks") or []) if c.get("key") in CHECK_KEYS and c.get("src") not in (None, "")]
    if not results:
        return None
    have = {(r["key"], r["target"]) for r in db.checks_list(host.id)}
    hub_side = declared(cfg)
    for key, target in hub_side + [(c["key"], str(c["src"])) for c in results]:
        if (key, target) in have or (key in SINGLE_KEYS and any(k == key for k, _ in have)):
            continue   # a single-value key keeps one row: an existing one, else the hub's declared one (listed first)
        db.check_add(host.id, key, target, {}, "hub settings" if (key, target) in hub_side else "agent.conf")
        have.add((key, target))
    db.set_check_config(host.name, render_settings({**cfg, MANAGED: "1"}, db.checks_list(host.id)))
    return host.check_rev + 1


def put_settings(db: Any, host: Any, body: dict, by: Optional[str]) -> int:
    """Set some of a host's remote settings (PUT /hosts/{name}/checks, manage.py checks --set/--clear). Only the keys
    in `body` change: an AVM_WATCH_* key replaces that kind's rows with its entries (rows of entries that stay keep
    their labels, responders and state; "" removes them all), any other key is stored ("" or None removes it).
    Rows of other kinds and settings not in the body are untouched, and a hub-managed host stays managed.
    The settings are re-read under the database lock, not taken from `host` (read when the request came in): a save
    racing another (an operator's, the data-use flag stored by ingest) never writes back the value it replaced.
    Returns the new revision."""
    with db.locked():
        host = db.host_by_id(host.id) or host
        cfg = {k: v for k, v in (host.check_config or {}).items() if k not in CHECK_KEYS}
        rows = db.checks_list(host.id)
        for key, raw in body.items():
            value = "" if raw is None else str(raw).strip()
            if key not in CHECK_KEYS:
                if value:
                    cfg[key] = value
                else:
                    cfg.pop(key, None)
                continue
            wanted = [e.strip() for e in ([value] if key in SINGLE_KEYS else value.split(",")) if e.strip()]
            for r in rows:
                if r["key"] == key and r["target"] not in wanted:
                    db.check_delete(r["id"])
            for target in wanted:
                db.check_add(host.id, key, target, {}, by)
        return db.set_check_config(host.name, render_settings(cfg, db.checks_list(host.id)))


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
    out["alert_mode"] = alert_mode(r)
    if host:
        out["host"] = host
    return out


# ---------------------------------------------------------------- responders
def alert_mode(r: dict) -> str:
    """Alert mode: the permission mode a check's responder prompt runs with. NULL (every row from before the column,
    and every check avm adds) is plan; only an operator route stores anything else (routes_checks.py)."""
    return r.get("alert_mode") or "plan"


GUARD = (
    "What to do: investigate READ-ONLY and report. Write a short incident note and tell the owning session and/or the user "
    "what you found. Do NOT restart, retry, kill or re-queue jobs; do not git pull/push/commit; do not edit configs, "
    "CLAUDE.md, permissions or gates; do not touch data-use-agreement paths (licensed datasets, external datasets, cached samples); "
    "do not start GPU work. This alert is not approval for any gated action - the owning session or the user decides."
)
# alert_mode above plan (operator-raised): the session may act, within its own permission mode, but only on this alert
GUARD_ACT = (
    "What to do: investigate and, within your own permissions, act only on what this alert concerns; then write a short "
    "incident note and tell the owning session and/or the user what you found and what you changed. Leave everything "
    "else alone: do not git push, do not edit CLAUDE.md, permissions or gates, do not touch data-use-agreement paths "
    "(licensed datasets, external datasets, cached samples), and do not start GPU work. This alert is not approval for "
    "any gated action - the owning session or the user decides."
)


def alert_guard(mode: str) -> str:
    """The closing instruction of an alert prompt: read-only for plan, scoped action for a raised alert mode."""
    return GUARD if mode == "plan" else GUARD_ACT


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
    lines += ["", alert_guard(alert_mode(r))]
    return "\n".join(lines)


CAP_WINDOW_SEC = 86400.0   # daily_cap counts alerts in this rolling window


def _recent_fires(r: dict, now: float) -> list[float]:
    """This check's alert times within the cap window."""
    try:
        times = json.loads(r.get("fire_log") or "null")
    except ValueError:
        times = None
    if not isinstance(times, list):   # a row from before fire_log: its per-day count, all at its last alert
        times = [r["last_fired"]] * int(r.get("fires_n") or 0) if r.get("last_fired") else []
    return [float(t) for t in times if isinstance(t, (int, float)) and now - t < CAP_WINDOW_SEC]


def evaluate(db: Any, host: Any, sample: dict, now: float) -> tuple[list[dict], list[dict]]:
    """Update each check's state from a report and fire responders. Returns (changed check rows, created jobs).
    A check missing from a report is "unknown" (e.g. the first report after an agent restart, before the hub's
    settings are applied again); that neither restarts its clock nor its alert: a check that was ok, goes unknown and
    comes back failing alerts like an ok -> failing one. An alert that can't go out yet (cooldown, cap, target
    mid-turn, nothing to prompt, an agent whose policy refuses the check's alert mode) stays pending, with why in
    hold_reason, and fires on a later report if the check is still failing. Alert mode: each check's responder runs
    in its own alert_mode (plan unless an operator raised it); a host whose reported cap is below that holds the alert
    with the reason - it is never sent in a lower mode instead."""
    from .routes_ops import JOB_MODES, host_policy_refusal   # local import: routes_ops -> core -> db imports this module
    rows = db.checks_list(host.id)
    if not rows:
        return [], []
    matched = match(rows, sample.get("checks") or [])
    claude = sample.get("claude") if isinstance(sample.get("claude"), dict) else {}
    agents = claude.get("agents") or []
    can_prompt = bool(claude.get("available")) and claude.get("bin_source") != "desktop"
    can_fork = "fork_session" in (claude.get("features") or [])
    # exact-session delivery: the agent relays the alert into the live session itself (a relay on that host that can
    # only list sessions and send one message); it needs AVM_ALLOW_ACTIONS there, like restart-with-prompt
    can_deliver = "deliver_session" in (claude.get("features") or []) and \
        bool((claude.get("policy") if isinstance(claude.get("policy"), dict) else {}).get("actions"))
    # what this report's agent says it accepts (no policy / root / a cap below the check's alert mode): the same
    # refusal as a dashboard prompt in that mode, worked out once per mode in use
    refusals: dict[str, Optional[str]] = {}

    def refused(mode: str) -> Optional[str]:
        if mode not in refusals:
            if mode not in JOB_MODES:   # never guess at a mode the agents don't know
                refusals[mode] = f"alert mode {mode!r} is not one of {', '.join(JOB_MODES)} (set it again on the dashboard)"
            else:
                why = host_policy_refusal(host, mode=mode, sample=sample)
                refusals[mode] = f"alert mode {mode}: {why}" if why and mode != "plan" else why
        return refusals[mode]

    changed, jobs = [], []
    tasks_cache: Optional[list] = None

    for r in rows:
        if r.get("expires") and r["expires"] < now:
            continue
        c = matched.get(r["id"])
        state = "unknown" if c is None else ("failing" if (not c.get("ok") and c.get("level") != "info") else "ok")
        known = r.get("known_state") or (r.get("state") if r.get("state") in ("ok", "failing") else None)
        upd: dict[str, Any] = {}
        if state != r.get("state"):
            upd["state"] = state
            if state != "unknown" and state != known:
                upd["since"] = now
        if state != "unknown" and state != r.get("known_state"):
            upd["known_state"] = state
        detail = (c.get("detail") or "")[:300] if c else None
        if c is not None and KIND_OF_KEY.get(r["key"]) != "content" and detail != r.get("detail"):
            upd["detail"] = detail
        if state == "ok":
            if not r.get("last_ok") or now - r["last_ok"] > 60:
                upd["last_ok"] = now
            if r.get("open_job") or r.get("pending"):
                upd.update(open_job=None, pending=0)   # recovered: the alert is over
        elif not r.get("notify") and r.get("pending"):
            upd["pending"] = 0   # responder removed: nothing left to send

        def hold(why: str) -> None:   # fire on a later report, if still failing; say why it's held (UI, API)
            if not r.get("pending"):
                upd["pending"] = 1
            if r.get("hold_reason") != why:
                upd["hold_reason"] = why

        fire = state == "failing" and r.get("notify") and not r.get("open_job") and (known == "ok" or r.get("pending"))
        fired = _recent_fires(r, now) if fire else []
        if fire:
            cooldown = max(MIN_COOLDOWN_MIN, float(r.get("cooldown_min") or 30)) * 60
            cap = int(r.get("daily_cap") or 6)
            mode = alert_mode(r)
            why = (refused(mode) or (None if can_prompt else "no Claude Code CLI to prompt on this host")
                   or (f"daily cap reached ({cap} alerts in 24 h)" if len(fired) >= cap else None)
                   or (f"cooling down ({cooldown / 60:g} min between alerts)" if r.get("last_fired") and now - r["last_fired"] < cooldown else None))
            if why:   # the host's agent won't take a prompt in this mode, nothing to prompt, cap (rolling 24 h) or cooldown
                fire = False
                hold(why)
        if fire:
            resume, fork, deliver, cwd = None, False, None, r.get("notify_cwd")
            target = r["notify"]
            if target.startswith("tracked:"):
                t = db.tracked_get(int(target.split(":", 1)[1]))
                if t is None:   # the owner was untracked: a fresh read-only session handles it
                    target = "new"
                else:
                    live = next((a for a in agents if a.get("key") == t["key"] or (t.get("session_id") and a.get("session_id") == t["session_id"])), None)
                    cwd = cwd or (live or {}).get("cwd") or t.get("cwd")   # a data-use host's row keeps only the last part
                    if live and live.get("live"):
                        if live.get("status") == "busy":
                            hold("the target session is mid-turn")   # try again on a later report
                            fire = False
                        elif can_fork:
                            resume, fork = t.get("session_id"), True   # live and idle: a copy of its context
                            name = (live.get("name") or t.get("name") or "").strip()
                            if can_deliver and name:   # better: into the session itself; the fork is the fallback
                                deliver = name
                        # else: live and idle, but this agent can't fork - never write into a live session; a fresh
                        # read-only session in its working dir handles it (as for an untracked owner / "new")
                    else:
                        resume = t.get("session_id")   # offline: wake it into its own conversation
            if fire:
                if tasks_cache is None:
                    recent = db.task_list(host.id, None, 60, with_log=bool(r.get("notify_log")))
                    tasks_cache = [t for t in recent if t["status"] in ("running", "queued", "stalled") or
                                   (t["status"] == "failed" and now - (t.get("ended") or t["updated"]) < 6 * 3600)]
                prompt = alert_prompt(host.name, {**r, **upd}, c, tasks_cache, now)
                fire_upd = {"last_fired": now, "fire_log": json.dumps(fired + [now]), "fires_n": len(fired) + 1, "pending": 0}
                j = db.check_fire(r["id"], fire_upd, host_id=host.id, prompt=prompt, cwd=cwd, mode=mode, model=None,
                                  resume_session=resume, created_by="check responder",
                                  title=f"alert · {r.get('label') or r['target']}"[:80], fork_session=fork,
                                  deliver_name=deliver)
                if j is not None:   # None: another report for this host fired it first
                    jobs.append(j)
                    upd.update(fire_upd, open_job=j["id"])
        if upd.get("pending") == 0 and r.get("hold_reason"):
            upd["hold_reason"] = None   # fired, recovered or responder removed: no longer held
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
