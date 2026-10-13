"""Research results pages: projects push a self-describing JSON document (`avm results push`), the
dashboard renders the latest and keeps every push as history.

The hub only stores and renders what is pushed. It never reads project repos or data directories,
which matters for projects under data-use agreements: the feed is aggregates by contract.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from starlette.concurrency import run_in_threadpool

from .auth import Identity
from .config import settings
from .core import db, hub, require_agent, require_viewer
from .db import HostRow
from .routes_ops import host_name

log = logging.getLogger("avalon.results")
router = APIRouter(prefix="/api/v1")

PROJECT_RE = re.compile(r"^[a-z0-9_-]{1,40}$")
SCHEMA_RE = re.compile(r"^[a-z0-9_.-]{1,40}-results/\d+$")
LIST_FIELDS = ("headline", "programmes", "bars", "distributions", "notes")


def _project(project: str) -> str:
    if not PROJECT_RE.match(project):
        raise HTTPException(422, "project must match [a-z0-9_-]{1,40}")
    return project


def _num(v: Any) -> Optional[float]:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def validate_doc(project: str, doc: Any) -> list[dict]:
    """Shape checks so the page never has to guess. Returns the headline summary kept for history."""
    if not isinstance(doc, dict):
        raise HTTPException(422, "document must be a JSON object")
    schema = doc.get("schema")
    if not isinstance(schema, str) or not SCHEMA_RE.match(schema):
        raise HTTPException(422, 'schema must look like "<name>-results/<version>"')
    if doc.get("project") != project:
        raise HTTPException(422, f"document project {doc.get('project')!r} does not match URL project {project!r}")
    for f in LIST_FIELDS:
        if f in doc and doc[f] is not None and not isinstance(doc[f], list):
            raise HTTPException(422, f"{f} must be a list")
    summary = []
    for i, row in enumerate(doc.get("headline") or []):
        if not isinstance(row, dict) or not isinstance(row.get("id"), str):
            raise HTTPException(422, f"headline[{i}] needs a string id")
        ci = row.get("ci")
        ci = [_num(ci[0]), _num(ci[1])] if isinstance(ci, list) and len(ci) == 2 else None
        summary.append({"id": row["id"], "value": _num(row.get("value")), "ci": ci})
    return summary


@router.post("/projects/{project}/results")
async def results_push(project: str, request: Request, host: HostRow = Depends(require_agent)):
    _project(project)
    body = await request.body()
    if len(body) > settings.max_payload_bytes:
        raise HTTPException(413, "document too large")
    try:
        doc = json.loads(body)
    except ValueError as e:
        raise HTTPException(422, f"invalid JSON: {e}") from None
    summary = validate_doc(project, doc)
    commit = doc.get("commit") if isinstance(doc.get("commit"), str) else None
    gen = doc.get("generated_at") if isinstance(doc.get("generated_at"), str) else None
    row = await run_in_threadpool(db.results_add, project, host.id, doc["schema"], gen, commit, summary, body.decode("utf-8"))
    log.info("results #%d for '%s' pushed from %s (commit %s)", row["id"], project, host.name, commit)
    await hub.broadcast({"type": "results", "project": project, "id": row["id"], "received": row["received"]})
    return {"ok": True, "id": row["id"], "project": project}


@router.get("/projects")
async def results_projects(ident: Identity = Depends(require_viewer)):
    return {"projects": await run_in_threadpool(db.results_projects)}


@router.get("/projects/{project}/results")
async def results_get(project: str, ident: Identity = Depends(require_viewer), push: Optional[int] = None):
    """The latest push (or ?push=<id>) with its document."""
    r = await run_in_threadpool(db.results_get, _project(project), push)
    if not r:
        raise HTTPException(404, "no results pushed for this project" if push is None else "no such push")
    return {"id": r["id"], "project": project, "received": r["received"], "host": host_name(r["host_id"]) if r["host_id"] else None,
            "commit": r["commit_ref"], "generated_at": r["generated_at"], "doc": json.loads(r["doc"])}


@router.get("/projects/{project}/results/history")
async def results_history(project: str, ident: Identity = Depends(require_viewer), limit: int = Query(500, ge=1, le=5000)):
    rows = await run_in_threadpool(db.results_history, _project(project), limit)
    return {"pushes": [{"id": r["id"], "received": r["received"], "generated_at": r["generated_at"], "commit": r["commit_ref"],
                        "host": host_name(r["host_id"]) if r["host_id"] else None, "headline": r["headline"]} for r in rows]}
