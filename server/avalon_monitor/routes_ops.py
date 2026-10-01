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
from .core import db, hub, require_agent, require_same_origin, require_viewer
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


class TaskUpdate(BaseModel):
    progress: Optional[float] = None
    log: list[str] = Field(default_factory=list)
    note: Optional[str] = None
    pid: Optional[int] = None


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
    t = await run_in_threadpool(db.task_create, host.id, body.name, body.source, body.expect_beat, body.command,
                                body.cwd, body.pid, body.user, body.meta)
    log.info("task #%d '%s' started on %s", t["id"], t["name"], host.name)
    await _emit_task(t)
    return {"ok": True, "id": t["id"], "task": task_public(t)}


@router.post("/tasks/{task_id}/beat")
async def task_beat(task_id: int, body: TaskUpdate, host: HostRow = Depends(require_agent)):
    t = await run_in_threadpool(db.task_update, task_id, host.id, progress=body.progress, log_lines=body.log, note=body.note, pid=body.pid)
    if t is None:
        raise HTTPException(404, "no such task on this host")
    await _emit_task(t)
    return {"ok": True, "status": t["status"]}


@router.post("/tasks/{task_id}/finish")
async def task_finish(task_id: int, body: TaskFinish, host: HostRow = Depends(require_agent)):
    if body.status not in ("done", "failed"):
        raise HTTPException(422, "status must be done or failed")
    t = await run_in_threadpool(db.task_update, task_id, host.id, status=body.status, rc=body.rc, log_lines=body.log, note=body.note)
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


@router.delete("/tasks/{task_id}")
async def task_delete(task_id: int, request: Request, ident: Identity = Depends(require_viewer)):
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


@router.post("/claude/jobs")
async def job_create(body: JobCreate, request: Request, ident: Identity = Depends(require_viewer)):
    require_same_origin(request)
    if body.mode not in JOB_MODES:
        raise HTTPException(422, f"mode must be one of {', '.join(JOB_MODES)}")
    if body.mode == "full" and not body.confirm_full:
        raise HTTPException(422, "full access requires confirm_full=true")
    h = await run_in_threadpool(db.host_by_name, body.host)
    if not h:
        raise HTTPException(404, "no such host")
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


@router.get("/claude/jobs/{job_id}")
async def job_get(job_id: int, ident: Identity = Depends(require_viewer), after: int = 0):
    j = await run_in_threadpool(db.job_get, job_id)
    if not j:
        raise HTTPException(404, "no such job")
    events = await run_in_threadpool(db.job_events, job_id, after)
    return {"job": job_public(j), "events": events}


@router.post("/claude/jobs/{job_id}/cancel")
async def job_cancel(job_id: int, request: Request, ident: Identity = Depends(require_viewer)):
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
    stored = await run_in_threadpool(db.job_add_events, job_id, host.id, body.events)
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
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    j = await run_in_threadpool(db.job_update, job_id, host.id, ended=time.time(), **fields)
    if j is None:
        raise HTTPException(404, "no such job on this host")
    log.info("claude job #%d on %s %s (%s turns, $%s)", job_id, host.name, body.status, body.num_turns, body.cost_usd)
    await hub.broadcast({"type": "claude_job", "job": job_public(j)})
    return {"ok": True}


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
