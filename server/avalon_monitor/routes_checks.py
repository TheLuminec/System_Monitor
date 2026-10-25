"""Checks API: the host page's Checks panel (viewer) and `avm check` (agent token, its own host only).
Every change re-renders the host's AVM_WATCH_* settings from its check rows (see checks.py)."""
from __future__ import annotations

import logging
import time
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from . import checks as ck
from .auth import Identity
from .core import db, hub, require_agent, require_same_origin, require_viewer
from .db import HostRow
from .routes_ops import tracked_public

log = logging.getLogger("avalon.checks")
router = APIRouter(prefix="/api/v1")

SETTING_KEYS = ("AVM_COMMIT_WARN_PCT", "AVM_PROCESSES", "AVM_TAGS")   # the non-check host settings the panel edits


class CheckIn(BaseModel):
    kind: str = Field(pattern=r"^(" + "|".join(ck.KINDS) + r")$")
    target: str = Field(min_length=1, max_length=400)
    label: Optional[str] = Field(default=None, max_length=120)
    ttl_sec: Optional[float] = Field(default=None, gt=0, le=90 * 86400)
    notify: Optional[str] = Field(default=None, pattern=r"^(none|new|self|tracked:\d+)$")
    notify_cwd: Optional[str] = Field(default=None, max_length=500)
    notify_log: Optional[bool] = None
    cooldown_min: Optional[float] = Field(default=None, ge=ck.MIN_COOLDOWN_MIN, le=7 * 1440)
    daily_cap: Optional[int] = Field(default=None, ge=1, le=48)
    session: Optional[dict] = None       # avm: the calling Claude session (key, session_id, kind, name, cwd) for notify=self


class CheckPatch(BaseModel):
    target: Optional[str] = Field(default=None, min_length=1, max_length=400)
    label: Optional[str] = Field(default=None, max_length=120)
    ttl_sec: Optional[float] = Field(default=None, ge=0, le=90 * 86400)   # 0 clears the expiry
    notify: Optional[str] = Field(default=None, pattern=r"^(none|new|self|tracked:\d+)$")
    notify_cwd: Optional[str] = Field(default=None, max_length=500)
    notify_log: Optional[bool] = None
    cooldown_min: Optional[float] = Field(default=None, ge=ck.MIN_COOLDOWN_MIN, le=7 * 1440)
    daily_cap: Optional[int] = Field(default=None, ge=1, le=48)
    session: Optional[dict] = None


def _clean(target: str, kind: str) -> str:
    t = target.strip()
    if "," in t:
        raise HTTPException(422, "a check target cannot contain a comma (agents read the settings as comma lists)")
    if kind == "memory":
        try:
            float(t)
        except ValueError:
            raise HTTPException(422, "memory floor is a number of GB, e.g. 8") from None
    return t


async def _fields(host: HostRow, body: Any, owner: Optional[str]) -> dict:
    """Editable columns from a CheckIn/CheckPatch; resolves notify=self to the caller's tracked session."""
    f: dict[str, Any] = {}
    if body.label is not None:
        f["label"] = body.label.strip() or None
    if body.ttl_sec is not None:
        f["expires"] = (time.time() + body.ttl_sec) if body.ttl_sec else None
    if body.notify_cwd is not None:
        f["notify_cwd"] = body.notify_cwd.strip() or None
    if body.notify_log is not None:
        f["notify_log"] = 1 if body.notify_log else 0
    if body.cooldown_min is not None:
        f["cooldown_min"] = body.cooldown_min
    if body.daily_cap is not None:
        f["daily_cap"] = body.daily_cap
    if body.notify is not None:
        if body.notify == "none":
            f["notify"] = None
        elif body.notify == "self":
            s = body.session or {}
            if not s.get("key"):
                raise HTTPException(422, "notify=self needs the calling Claude session (run avm check add inside one)")
            t = await run_in_threadpool(db.tracked_add, host.id, s["key"], s, f"avm@{host.name}")
            await hub.broadcast({"type": "claude_tracked", "row": tracked_public(t)})
            f["notify"] = f"tracked:{t['id']}"
            f.setdefault("notify_cwd", s.get("cwd"))
        elif body.notify.startswith("tracked:"):
            t = await run_in_threadpool(db.tracked_get, int(body.notify.split(":")[1]))
            if not t or t["host_id"] != host.id:
                raise HTTPException(422, "that tracked session is not on this host")
            f["notify"] = body.notify
        else:
            f["notify"] = "new"
    if owner:
        f["owner"] = owner
    return f


async def _changed(host_id: int, name: str, row: Optional[dict] = None, deleted: Optional[int] = None) -> None:
    rev = await run_in_threadpool(ck.sync_config, db, host_id)
    if rev is not None:
        log.info("checks for %s re-rendered (rev %d)", name, rev)
    if row is not None:
        await hub.broadcast({"type": "check", "check": ck.public(row, name)})
    if deleted is not None:
        await hub.broadcast({"type": "check_deleted", "id": deleted, "host": name})


async def _add(host: HostRow, body: CheckIn, by: str, owner: Optional[str]) -> dict:
    key = ck.KINDS[body.kind][0]
    target = _clean(body.target, body.kind)
    if key in ck.SINGLE_KEYS:   # one memory floor per host: replace the old one
        for r in await run_in_threadpool(db.checks_list, host.id):
            if r["key"] == key and r["target"] != target:
                await run_in_threadpool(db.check_delete, r["id"])
    fields = await _fields(host, body, owner)
    row = await run_in_threadpool(db.check_add, host.id, key, target, fields, by)
    log.info("%s added check %s %s on %s", by, body.kind, target, host.name)
    await _changed(host.id, host.name, row)
    return ck.public(row, host.name)


async def _patch(host: HostRow, cid: int, body: CheckPatch, host_only: bool) -> dict:
    row = await run_in_threadpool(db.check_get, cid)
    if not row or row["host_id"] != host.id:
        raise HTTPException(404, "no such check" + (" on this host" if host_only else ""))
    fields = await _fields(host, body, None)
    if body.target is not None:
        fields["target"] = _clean(body.target, ck.KIND_OF_KEY.get(row["key"], ""))
    try:
        row = await run_in_threadpool(db.check_update, cid, fields)
    except Exception as e:   # unique (host, key, target)
        raise HTTPException(409, f"cannot update: {e}") from None
    await _changed(host.id, host.name, row)
    return ck.public(row, host.name)


async def _delete(host: HostRow, cid: int) -> None:
    row = await run_in_threadpool(db.check_delete, cid, host.id)
    if not row:
        raise HTTPException(404, "no such check on this host")
    await _changed(host.id, host.name, deleted=cid)


async def _host(name: str) -> HostRow:
    h = await run_in_threadpool(db.host_by_name, name)
    if not h:
        raise HTTPException(404, "no such host")
    return h


async def _check_host(cid: int) -> HostRow:
    row = await run_in_threadpool(db.check_get, cid)
    if not row:
        raise HTTPException(404, "no such check")
    h = await run_in_threadpool(db.host_by_id, row["host_id"])
    if not h:
        raise HTTPException(404, "no such host")
    return h


# ---------------------------------------------------------------- dashboard
@router.get("/hosts/{name}/checklist")
async def checklist(name: str, ident: Identity = Depends(require_viewer)):
    """Everything the Checks panel shows: rows with live results, rule checks the hub adds, responder targets."""
    h = await _host(name)
    rows = await run_in_threadpool(db.checks_list, h.id)
    results = (h.last_sample or {}).get("checks") or []
    matched = ck.match(rows, results)
    used = {id(c) for c in matched.values()}
    tracked = await run_in_threadpool(db.tracked_list, h.id)
    claude = (h.last_sample or {}).get("claude") or {}
    return {
        "host": h.name,
        "checks": [dict(ck.public(r, h.name), result=matched.get(r["id"])) for r in rows],
        "other_results": [c for c in results if id(c) not in used],   # hub rules (gpu stall, duplicate agent) and unmatched
        "kinds": [{"kind": k, "key": v[0], "label": v[1], "hint": v[2]} for k, v in ck.KINDS.items()],
        "settings": {k: (h.check_config or {}).get(k, "") for k in SETTING_KEYS},
        "rev": h.check_rev, "agent_rev": (h.last_sample or {}).get("config_rev"),
        "responders": [{"value": f"tracked:{t['id']}", "label": t["name"] or t["key"][:8], "kind": t["kind"], "cwd": t["cwd"]} for t in tracked],
        "can_prompt": bool(claude.get("available")) and claude.get("bin_source") != "desktop",
    }


@router.post("/hosts/{name}/checklist")
async def checklist_add(name: str, body: CheckIn, request: Request, ident: Identity = Depends(require_viewer)):
    require_same_origin(request)
    if body.notify == "self":
        raise HTTPException(422, "notify=self is for avm inside a Claude session; pick a tracked agent here")
    return {"ok": True, "check": await _add(await _host(name), body, ident.subject, None)}


@router.patch("/checklist/{cid}")
async def checklist_patch(cid: int, body: CheckPatch, request: Request, ident: Identity = Depends(require_viewer)):
    require_same_origin(request)
    return {"ok": True, "check": await _patch(await _check_host(cid), cid, body, False)}


@router.delete("/checklist/{cid}")
async def checklist_delete(cid: int, request: Request, ident: Identity = Depends(require_viewer)):
    require_same_origin(request)
    await _delete(await _check_host(cid), cid)
    return {"ok": True}


@router.put("/hosts/{name}/check-settings")
async def check_settings(name: str, request: Request, ident: Identity = Depends(require_viewer)):
    """The few non-check host settings (commit warning %, top processes, tags); check rows are untouched."""
    require_same_origin(request)
    body = await request.json()
    if not isinstance(body, dict) or any(k not in SETTING_KEYS for k in body):
        raise HTTPException(422, f"settable here: {', '.join(SETTING_KEYS)}")
    h = await _host(name)
    cfg = {k: v for k, v in (h.check_config or {}).items() if k not in SETTING_KEYS}
    cfg.update({k: str(v).strip() for k, v in body.items() if v is not None and str(v).strip()})
    rev = await run_in_threadpool(db.set_check_config, h.name, ck.render_settings(cfg, await run_in_threadpool(db.checks_list, h.id)))
    return {"ok": True, "rev": rev}


# -------------------------------------------------------------------- agent
@router.get("/agent/checks")
async def agent_checks(host: HostRow = Depends(require_agent)):
    rows = await run_in_threadpool(db.checks_list, host.id)
    matched = ck.match(rows, (host.last_sample or {}).get("checks") or [])
    return {"checks": [dict(ck.public(r, host.name), result=matched.get(r["id"])) for r in rows]}


@router.post("/agent/checks")
async def agent_check_add(body: CheckIn, host: HostRow = Depends(require_agent)):
    owner = (body.session or {}).get("key")
    return {"ok": True, "check": await _add(host, body, f"avm@{host.name}", owner)}


@router.patch("/agent/checks/{cid}")
async def agent_check_patch(cid: int, body: CheckPatch, host: HostRow = Depends(require_agent)):
    return {"ok": True, "check": await _patch(host, cid, body, True)}


@router.delete("/agent/checks/{cid}")
async def agent_check_delete(cid: int, host: HostRow = Depends(require_agent)):
    await _delete(host, cid)
    return {"ok": True}
