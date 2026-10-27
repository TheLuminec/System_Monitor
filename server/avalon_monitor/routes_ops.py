"""Tasks (avm CLI), Claude Code jobs, and dashboard preferences."""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from .auth import Identity
from . import datause
from .core import db, hub, require_agent, require_same_origin, require_operator, require_viewer
from .db import HostRow

log = logging.getLogger("avalon.ops")
router = APIRouter(prefix="/api/v1")

JOB_MODES = ("plan", "acceptEdits", "full")
MAX_PROMPT = 20000


def _hostname(host_id: int) -> str:
    for h in db.list_hosts():
        if h.id == host_id:
            return h.name
    return "?"


_host_names: dict[int, str] = {}


def host_name(host_id: int) -> str:
    if host_id not in _host_names:
        _host_names.clear()
        for h in db.list_hosts():
            _host_names[h.id] = h.name
    return _host_names.get(host_id, "?")


def task_public(t: dict) -> dict:
    out = dict(t)
    out["host"] = host_name(t["host_id"])
    out["meta"] = json.loads(t["meta"]) if t.get("meta") else None
    now = time.time()
    out["elapsed"] = (t["ended"] or now) - t["started"]
    out["beat_age"] = now - t["heartbeat"]
    if "log" in out and out["log"] is not None:
        out["log"] = out["log"].splitlines()
    return out


def job_public(j: dict) -> dict:
    out = dict(j)
    out["host"] = host_name(j["host_id"])
    if isinstance(out.get("target"), str):
        try:
            out["target"] = json.loads(out["target"])
        except ValueError:
            out["target"] = None
    return out


def tracked_public(t: dict) -> dict:
    out = dict(t)
    out["host"] = host_name(t["host_id"])
    out["live"] = bool(t.get("live"))
    return out


# ================================================================== tasks
class TaskStart(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    source: str = "task"                 # run | task | watch
    expect_beat: Optional[float] = Field(default=None, ge=5, le=86400)
    command: Optional[str] = None
    cwd: Optional[str] = None
    pid: Optional[int] = None
    user: Optional[str] = None
    meta: Optional[dict] = None
    status: str = Field(default="running", pattern=r"^(running|queued)$")   # queued: `avm run --wait` waiting for resources


class TaskUpdate(BaseModel):
    progress: Optional[float] = None
    log: list[str] = Field(default_factory=list)
    note: Optional[str] = None
    pid: Optional[int] = None
    status: Optional[str] = Field(default=None, pattern=r"^(running|queued)$")   # queued -> running on admission
    meta: Optional[dict] = None          # merged into the task's meta (queue position/reason, resources, code at start)


class TaskFinish(BaseModel):
    status: str = "done"                 # done | failed
    rc: Optional[int] = None
    log: list[str] = Field(default_factory=list)
    note: Optional[str] = None


async def _emit_task(t: dict) -> None:
    await hub.broadcast({"type": "task", "task": task_public({k: v for k, v in t.items() if k != "log"})})


@router.post("/tasks")
async def task_start(body: TaskStart, host: HostRow = Depends(require_agent)):
    if body.source not in ("run", "task", "watch"):
        raise HTTPException(422, "source must be run, task or watch")
    hide = datause.restricted(host)   # data-use host: no command lines or paths (they can name the data)
    t = await run_in_threadpool(db.task_create, host.id, body.name, body.source, body.expect_beat, None if hide else body.command,
                                None if hide else body.cwd, body.pid, body.user, body.meta, body.status)
    log.info("task #%d '%s' %s on %s", t["id"], t["name"], "queued" if body.status == "queued" else "started", host.name)
    await _emit_task(t)
    return {"ok": True, "id": t["id"], "task": task_public(t)}


@router.post("/tasks/{task_id}/beat")
async def task_beat(task_id: int, body: TaskUpdate, host: HostRow = Depends(require_agent)):
    hide = datause.restricted(host)   # data-use host: no output lines, and no notes (free text: `avm ... --note`)
    t = await run_in_threadpool(db.task_update, task_id, host.id, progress=body.progress, note=None if hide else body.note,
                                pid=body.pid, log_lines=None if hide else body.log, status=body.status, meta=body.meta)
    if t is None:
        raise HTTPException(404, "no such task on this host")
    await _emit_task(t)
    return {"ok": True, "status": t["status"]}


@router.post("/tasks/{task_id}/finish")
async def task_finish(task_id: int, body: TaskFinish, host: HostRow = Depends(require_agent)):
    if body.status not in ("done", "failed"):
        raise HTTPException(422, "status must be done or failed")
    hide = datause.restricted(host)   # as for beats: the exit code stays, output lines and the note don't
    t = await run_in_threadpool(db.task_update, task_id, host.id, status=body.status, rc=body.rc,
                                note=None if hide else body.note, log_lines=None if hide else body.log)
    if t is None:
        raise HTTPException(404, "no such task on this host")
    (log.warning if body.status == "failed" else log.info)("task #%d '%s' %s on %s (rc=%s)", t["id"], t["name"], body.status, host.name, body.rc)
    await _emit_task(t)
    return {"ok": True, "status": t["status"]}


@router.get("/tasks/mine")
async def tasks_mine(host: HostRow = Depends(require_agent), status: Optional[str] = None):
    rows = await run_in_threadpool(db.task_list, host.id, status, 100)
    return {"tasks": [task_public(t) for t in rows]}


@router.get("/tasks")
async def tasks_list(ident: Identity = Depends(require_viewer), host: Optional[str] = None, status: Optional[str] = None,
                     limit: int = Query(200, ge=1, le=1000)):
    host_id = None
    if host:
        h = await run_in_threadpool(db.host_by_name, host)
        if not h:
            raise HTTPException(404, "no such host")
        host_id = h.id
    rows = await run_in_threadpool(db.task_list, host_id, status, limit)
    return {"tasks": [task_public(t) for t in rows], "server_time": time.time()}


@router.get("/tasks/{task_id}")
async def task_get(task_id: int, ident: Identity = Depends(require_viewer)):
    t = await run_in_threadpool(db.task_get, task_id)
    if not t:
        raise HTTPException(404, "no such task")
    return task_public(t)


class TaskAck(BaseModel):
    ids: Optional[list[int]] = None      # None = every problem task (optionally on `host`)
    host: Optional[str] = None
    undo: bool = False


@router.post("/tasks/ack")
async def tasks_ack(body: TaskAck, request: Request, ident: Identity = Depends(require_operator)):
    """Acknowledge failed/stalled/lost tasks so they stop alerting (they stay in the task list)."""
    require_same_origin(request)
    host_id = None
    if body.host:
        h = await run_in_threadpool(db.host_by_name, body.host)
        if not h:
            raise HTTPException(404, "no such host")
        host_id = h.id
    rows = await run_in_threadpool(db.task_ack, body.ids, host_id, ident.subject, body.undo)
    for t in rows:
        await _emit_task(t)
    return {"ok": True, "changed": len(rows)}


@router.delete("/tasks/{task_id}")
async def task_delete(task_id: int, request: Request, ident: Identity = Depends(require_operator)):
    require_same_origin(request)
    if not await run_in_threadpool(db.task_delete, task_id):
        raise HTTPException(404, "no such task")
    await hub.broadcast({"type": "task_deleted", "id": task_id})
    return {"ok": True}


# ============================================================ claude jobs
class JobCreate(BaseModel):
    host: str
    prompt: str = Field(min_length=1, max_length=MAX_PROMPT)
    cwd: Optional[str] = Field(default=None, max_length=500)
    mode: str = "plan"
    model: Optional[str] = Field(default=None, max_length=60)
    resume_session: Optional[str] = Field(default=None, max_length=80)
    confirm_full: bool = False


class JobStart(BaseModel):
    session_id: Optional[str] = None


class JobEvents(BaseModel):
    events: list[dict] = Field(default_factory=list, max_length=500)


class JobFinish(BaseModel):
    status: str                          # done | failed | cancelled
    session_id: Optional[str] = None
    result: Optional[str] = None
    cost_usd: Optional[float] = None
    duration_ms: Optional[int] = None
    num_turns: Optional[int] = None
    error: Optional[str] = None
    new_key: Optional[str] = Field(default=None, max_length=80)   # restart came back as a new background agent


MODE_RANK = {"plan": 0, "acceptEdits": 1, "full": 2}


def host_policy_refusal(h: HostRow, mode: Optional[str] = None, action: Optional[str] = None,
                        sample: Optional[dict] = None) -> Optional[str]:
    """The hub's check of what a host's agent says it will accept (agent 1.9+ reports claude.policy): refuse up front
    what it would refuse anyway - and everything for agents that report no policy, which enforce no local cap.
    `sample`: the report being ingested (check responders, checks.evaluate), else the host's last stored one."""
    policy = (((sample if sample is not None else h.last_sample) or {}).get("claude") or {}).get("policy")
    if not isinstance(policy, dict):
        return (f"{h.name}'s agent reports no local policy (older than 1.9), so it enforces no limits of its own: "
                "prompts and restarts are refused until it updates")
    if not policy.get("jobs", True):
        return f"{h.name}'s agent runs as root/SYSTEM and refuses prompts and actions (AVM_ALLOW_ROOT_JOBS in its agent.conf)"
    if action and not policy.get("actions", True):
        return f"{h.name} doesn't allow restart/stop from the dashboard (AVM_ALLOW_ACTIONS in its agent.conf)"
    cap = policy.get("max_mode") if policy.get("max_mode") in MODE_RANK else "plan"
    if mode and MODE_RANK.get(mode, 99) > MODE_RANK[cap]:
        return f"{h.name} allows prompts up to {cap} (AVM_MAX_JOB_MODE in its agent.conf; the hub can't raise it)"
    return None


@router.post("/claude/jobs")
async def job_create(body: JobCreate, request: Request, ident: Identity = Depends(require_operator)):
    require_same_origin(request)
    if body.mode not in JOB_MODES:
        raise HTTPException(422, f"mode must be one of {', '.join(JOB_MODES)}")
    if body.mode == "full" and not body.confirm_full:
        raise HTTPException(422, "full access requires confirm_full=true")
    h = await run_in_threadpool(db.host_by_name, body.host)
    if not h:
        raise HTTPException(404, "no such host")
    if ((h.last_sample or {}).get("claude") or {}).get("bin_source") == "desktop":
        # Claude Desktop's bundled CLI gets its login from the app; started by the agent it fails with "OAuth session expired".
        raise HTTPException(422, f"{h.name} only has Claude Desktop's built-in Claude Code, which can't run prompts outside the app. "
                                 "Install the Claude Code CLI there (and log in once) to prompt it from the dashboard.")
    why = host_policy_refusal(h, mode=body.mode)
    if why:
        raise HTTPException(409, why)
    j = await run_in_threadpool(db.job_create, h.id, body.prompt, body.cwd, body.mode, body.model, body.resume_session, ident.subject)
    log.info("%s queued claude job #%d on %s (mode=%s)", ident.subject, j["id"], h.name, body.mode)
    await hub.broadcast({"type": "claude_job", "job": job_public(j)})
    return {"ok": True, "job": job_public(j)}


@router.get("/claude/jobs")
async def jobs_list(ident: Identity = Depends(require_viewer), host: Optional[str] = None, limit: int = Query(100, ge=1, le=500)):
    host_id = None
    if host:
        h = await run_in_threadpool(db.host_by_name, host)
        if not h:
            raise HTTPException(404, "no such host")
        host_id = h.id
    rows = await run_in_threadpool(db.job_list, host_id, limit)
    return {"jobs": [job_public(j) for j in rows]}


class JobAck(BaseModel):
    ids: Optional[list[int]] = None      # None = every failed prompt
    undo: bool = False


@router.post("/claude/jobs/ack")
async def jobs_ack(body: JobAck, request: Request, ident: Identity = Depends(require_operator)):
    """Acknowledge failed prompts so they stop alerting (they stay in the job list)."""
    require_same_origin(request)
    rows = await run_in_threadpool(db.job_ack, body.ids, ident.subject, body.undo)
    for j in rows:
        await hub.broadcast({"type": "claude_job", "job": job_public(j)})
    return {"ok": True, "changed": len(rows)}


@router.get("/claude/jobs/{job_id}")
async def job_get(job_id: int, ident: Identity = Depends(require_viewer), after: int = 0):
    j = await run_in_threadpool(db.job_get, job_id)
    if not j:
        raise HTTPException(404, "no such job")
    events = await run_in_threadpool(db.job_events, job_id, after)
    return {"job": job_public(j), "events": events}


@router.post("/claude/jobs/{job_id}/cancel")
async def job_cancel(job_id: int, request: Request, ident: Identity = Depends(require_operator)):
    require_same_origin(request)
    j = await run_in_threadpool(db.job_get, job_id)
    if not j:
        raise HTTPException(404, "no such job")
    if j["status"] == "queued":
        j = await run_in_threadpool(db.job_update, job_id, None, status="cancelled", ended=time.time(), cancel=1)
    elif j["status"] == "running":
        j = await run_in_threadpool(db.job_update, job_id, None, cancel=1)
    await hub.broadcast({"type": "claude_job", "job": job_public(j)})
    return {"ok": True, "job": job_public(j)}


# agent side
@router.post("/claude/jobs/{job_id}/start")
async def job_started(job_id: int, body: JobStart, host: HostRow = Depends(require_agent)):
    j = await run_in_threadpool(db.job_update, job_id, host.id, status="running", started=time.time(),
                                **({"session_id": body.session_id} if body.session_id else {}))
    if j is None:
        raise HTTPException(404, "no such job on this host")
    await hub.broadcast({"type": "claude_job", "job": job_public(j)})
    return {"ok": True}


@router.post("/claude/jobs/{job_id}/events")
async def job_events(job_id: int, body: JobEvents, host: HostRow = Depends(require_agent)):
    events = [datause.scrub_event(e) for e in body.events] if datause.restricted(host) else body.events
    stored = await run_in_threadpool(db.job_add_events, job_id, host.id, events)
    if not stored and body.events:
        raise HTTPException(404, "no such job on this host")
    sid = next((e.get("session_id") for _, e in stored if e.get("type") == "system" and e.get("session_id")), None)
    if sid:
        j = await run_in_threadpool(db.job_update, job_id, host.id, session_id=sid)
        if j:
            await hub.broadcast({"type": "claude_job", "job": job_public(j)})
    for seq, ev in stored:
        await hub.broadcast({"type": "claude_event", "job_id": job_id, "seq": seq, "event": ev})
    j = await run_in_threadpool(db.job_get, job_id)
    return {"ok": True, "cancel": bool(j and j["cancel"])}


@router.post("/claude/jobs/{job_id}/finish")
async def job_finish(job_id: int, body: JobFinish, host: HostRow = Depends(require_agent)):
    if body.status not in ("done", "failed", "cancelled"):
        raise HTTPException(422, "bad status")
    fields = {k: v for k, v in body.model_dump().items() if v is not None and k != "new_key"}
    j = await run_in_threadpool(db.job_update, job_id, host.id, ended=time.time(), **fields)
    if j is None:
        raise HTTPException(404, "no such job on this host")
    log.info("claude job #%d on %s %s (%s turns, $%s)", job_id, host.name, body.status, body.num_turns, body.cost_usd)
    pub = job_public(j)
    if body.new_key and j.get("action") and (pub.get("target") or {}).get("key"):
        row = await run_in_threadpool(db.tracked_rekey, host.id, pub["target"]["key"], body.new_key)
        if row:
            log.info("tracked session %s on %s now runs as %s", pub["target"]["key"], host.name, body.new_key)
            await hub.broadcast({"type": "claude_tracked", "row": tracked_public(row)})
    await hub.broadcast({"type": "claude_job", "job": pub})
    return {"ok": True}


# ======================================================= tracked sessions
KEY_RE = r"^[A-Za-z0-9][A-Za-z0-9_-]{3,79}$"


class TrackAdd(BaseModel):
    key: str = Field(pattern=KEY_RE)
    session_id: Optional[str] = Field(default=None, pattern=KEY_RE)
    name: Optional[str] = Field(default=None, max_length=120)
    cwd: Optional[str] = Field(default=None, max_length=500)
    kind: Optional[str] = Field(default=None, pattern=r"^(background|interactive|desktop)$")
    note: Optional[str] = Field(default=None, max_length=300)


class TrackAddHost(TrackAdd):
    host: str


class TrackAction(BaseModel):
    action: str = Field(pattern=r"^(restart|stop)$")
    prompt: Optional[str] = Field(default=None, max_length=MAX_PROMPT)   # only used when a restart resumes as a new agent


async def _host_or_404(name: str) -> HostRow:
    h = await run_in_threadpool(db.host_by_name, name)
    if not h:
        raise HTTPException(404, "no such host")
    return h


@router.get("/claude/tracked")
async def tracked_list(ident: Identity = Depends(require_viewer), host: Optional[str] = None):
    host_id = (await _host_or_404(host)).id if host else None
    return {"tracked": [tracked_public(t) for t in await run_in_threadpool(db.tracked_list, host_id)]}


@router.post("/claude/tracked")
async def tracked_add(body: TrackAddHost, request: Request, ident: Identity = Depends(require_operator)):
    require_same_origin(request)
    h = await _host_or_404(body.host)
    row = await run_in_threadpool(db.tracked_add, h.id, body.key, body.model_dump(), ident.subject)
    log.info("%s tracks claude session %s on %s", ident.subject, body.key, h.name)
    await hub.broadcast({"type": "claude_tracked", "row": tracked_public(row)})
    return {"ok": True, "row": tracked_public(row)}


@router.delete("/claude/tracked/{tid}")
async def tracked_delete(tid: int, request: Request, ident: Identity = Depends(require_operator)):
    require_same_origin(request)
    if not await run_in_threadpool(db.tracked_delete, tid):
        raise HTTPException(404, "not tracked")
    await hub.broadcast({"type": "claude_untracked", "id": tid})
    return {"ok": True}


@router.post("/claude/tracked/{tid}/action")
async def tracked_action(tid: int, body: TrackAction, request: Request, ident: Identity = Depends(require_operator)):
    """Queue a restart/stop for the session's host. It runs on the host's next report (or when it comes back online)."""
    require_same_origin(request)
    t = await run_in_threadpool(db.tracked_get, tid)
    if not t:
        raise HTTPException(404, "not tracked")
    if t["kind"] == "desktop":
        raise HTTPException(422, "Claude Desktop sessions belong to the Desktop app: reopen or close them there.")
    h = await run_in_threadpool(db.host_by_id, t["host_id"])
    why = host_policy_refusal(h, action=body.action) if h else "host gone"
    if why:
        raise HTTPException(409, why)
    label = t["name"] or t["key"][:8]
    target = {"key": t["key"], "session_id": t["session_id"], "kind": t["kind"], "name": t["name"], "cwd": t["cwd"]}
    prompt = body.prompt or "You were restarted from the Avalon Monitor dashboard. Re-read your recent context and pick up where you left off."
    j = await run_in_threadpool(db.job_create, t["host_id"], prompt, t["cwd"], "plan", None, None, ident.subject,
                                body.action, target, f"{body.action} · {label}")
    log.info("%s queued %s of %s on %s", ident.subject, body.action, t["key"], host_name(t["host_id"]))
    await hub.broadcast({"type": "claude_job", "job": job_public(j)})
    return {"ok": True, "job": job_public(j)}


# agent side: `avm agent track` lets a session register itself (the host comes from the agent token)
@router.post("/agent/claude/track")
async def agent_track(body: TrackAdd, host: HostRow = Depends(require_agent)):
    row = await run_in_threadpool(db.tracked_add, host.id, body.key, body.model_dump(), f"avm@{host.name}")
    await hub.broadcast({"type": "claude_tracked", "row": tracked_public(row)})
    return {"ok": True, "row": tracked_public(row)}


@router.post("/agent/claude/untrack")
async def agent_untrack(body: TrackAdd, host: HostRow = Depends(require_agent)):
    t = await run_in_threadpool(db.tracked_find, host.id, body.key)
    if not t or not await run_in_threadpool(db.tracked_delete, t["id"], host.id):
        raise HTTPException(404, "not tracked on this host")
    await hub.broadcast({"type": "claude_untracked", "id": t["id"]})
    return {"ok": True}


@router.get("/agent/claude/tracked")
async def agent_tracked(host: HostRow = Depends(require_agent)):
    return {"tracked": [tracked_public(t) for t in await run_in_threadpool(db.tracked_list, host.id)]}


# =================================================================== prefs
@router.get("/prefs")
async def prefs_get(ident: Identity = Depends(require_viewer)):
    return {"user": ident.subject, "prefs": await run_in_threadpool(db.prefs_get, ident.subject)}


@router.put("/prefs")
async def prefs_put(request: Request, ident: Identity = Depends(require_viewer)):
    require_same_origin(request)
    body = await request.json()
    if not isinstance(body, dict) or len(json.dumps(body)) > 200_000:
        raise HTTPException(422, "prefs must be a small JSON object")
    await run_in_threadpool(db.prefs_set, ident.subject, body)
    return {"ok": True}
