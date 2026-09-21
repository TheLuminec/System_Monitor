"""Avalon Monitor hub - FastAPI application."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from . import __version__
from .auth import AuthError, Authorizer, Identity
from .config import settings
from .db import METRICS, Database, HostRow
from .models import Check, Sample
from .rules import GpuStallRule

logging.basicConfig(level=settings.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("avalon")

STATIC_DIR = Path(__file__).parent / "static"
AGENT_SCRIPT = Path(settings.agent_script) if settings.agent_script else Path(__file__).resolve().parents[2] / "agent" / "avalon_agent.py"
RANGES = {"15m": 900, "1h": 3600, "6h": 21600, "24h": 86400, "7d": 7 * 86400, "30d": 30 * 86400}

# Agent settings the dashboard may set remotely (pushed to the agent in every ingest reply).
REMOTE_KEYS = (
    "AVM_WATCH_PROCESSES", "AVM_WATCH_FILES", "AVM_WATCH_TCP", "AVM_WATCH_MOUNTS", "AVM_WATCH_CONTENT",
    "AVM_WATCH_MARKERS", "AVM_WATCH_NONEMPTY", "AVM_WATCH_UNITS", "AVM_WATCH_JOBS",
    "AVM_MEM_AVAILABLE_WARN_GB", "AVM_PROCESSES", "AVM_TAGS",
)
AGENT_COMMANDS = ("respawn",)


# ------------------------------------------------------------------ live hub
class LiveHub:
    """Fan-out of host updates to connected dashboard WebSockets."""

    def __init__(self) -> None:
        self.clients: set[WebSocket] = set()
        self.online: dict[int, bool] = {}

    async def broadcast(self, message: dict) -> None:
        if not self.clients:
            return
        data = json.dumps(message, separators=(",", ":"))
        dead = []
        for ws in list(self.clients):
            try:
                await ws.send_text(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.clients.discard(ws)


class AgentBundle:
    """The agent script the hub hands out, re-read when the file changes."""

    def __init__(self, path: Path):
        self.path = path
        self._mtime = -1.0
        self.data = b""
        self.sha256 = ""
        self.version = ""
        self.refresh()

    def refresh(self) -> None:
        try:
            st = self.path.stat()
        except OSError:
            self.data, self.sha256, self.version, self._mtime = b"", "", "", -1.0
            return
        if st.st_mtime == self._mtime:
            return
        self.data = self.path.read_bytes()
        self.sha256 = hashlib.sha256(self.data).hexdigest()
        m = re.search(rb'^AGENT_VERSION\s*=\s*"([^"]+)"', self.data, re.M)
        self.version = m.group(1).decode() if m else "?"
        self._mtime = st.st_mtime
        log.info("serving agent %s (%s) from %s", self.version, self.sha256[:12], self.path)

    @property
    def available(self) -> bool:
        return bool(self.data)


db = Database(settings.db_path)
authz = Authorizer(settings)
hub = LiveHub()
agent_bundle = AgentBundle(AGENT_SCRIPT)
gpu_stall = GpuStallRule(minutes=settings.gpu_stall_minutes, percent=settings.gpu_stall_percent)


# --------------------------------------------------------------- serializers
def host_status(h: HostRow, now: float | None = None) -> str:
    now = now or time.time()
    if not h.enabled:
        return "disabled"
    if h.last_seen is None:
        return "never"
    return "online" if now - h.last_seen <= settings.offline_after_sec else "offline"


def _summary_of(sample: Optional[dict]) -> dict[str, Any]:
    """Compact per-host summary for the fleet view (full sample is in the detail view)."""
    if not sample:
        return {}
    cpu = sample.get("cpu", {})
    mem = sample.get("memory", {})
    net = sample.get("network", {})
    dio = sample.get("disk_io", {})
    disks = sample.get("disks", [])
    gpus = sample.get("gpus", [])
    temps = sample.get("temps", [])
    root = next((d for d in disks if d.get("mountpoint") in ("/", "C:\\")), None) or (
        max(disks, key=lambda d: d.get("total") or 0) if disks else None
    )
    checks = sample.get("checks", [])
    return {
        "ts": sample.get("ts"),
        "host": sample.get("host", {}),
        "cpu": {"percent": cpu.get("percent"), "load": cpu.get("load"), "temp_c": cpu.get("temp_c"),
                "cores": len(cpu.get("per_core") or [])},
        "memory": {"percent": mem.get("percent"), "used": mem.get("used"), "total": mem.get("total"),
                   "swap_percent": mem.get("swap_percent"), "committed": mem.get("committed"),
                   "commit_limit": mem.get("commit_limit")},
        "disk": {"percent": root.get("percent") if root else None, "used": root.get("used") if root else None,
                 "total": root.get("total") if root else None, "mountpoint": root.get("mountpoint") if root else None,
                 "count": len(disks), "max_percent": max((d.get("percent") or 0) for d in disks) if disks else None},
        "disk_io": {"read_bps": dio.get("read_bps"), "write_bps": dio.get("write_bps")},
        "network": {"rx_bps": net.get("rx_bps"), "tx_bps": net.get("tx_bps")},
        "gpus": [{"name": g.get("name"), "vendor": g.get("vendor"), "util_percent": g.get("util_percent"),
                  "mem_percent": g.get("mem_percent"), "mem_used": g.get("mem_used"), "mem_total": g.get("mem_total"),
                  "temp_c": g.get("temp_c"), "power_w": g.get("power_w")} for g in gpus],
        "temp_max": max((t.get("current") or 0) for t in temps) if temps else None,
        "battery": sample.get("battery"),
        "checks": checks,
        "checks_failing": sum(1 for c in checks if not c.get("ok") and c.get("level") != "info"),
        "top_process": max(sample.get("processes", []), key=lambda p: p.get("cpu_percent") or 0, default=None),
    }


def host_public(h: HostRow, full: bool = False) -> dict[str, Any]:
    now = time.time()
    out = {
        "id": h.id,
        "name": h.name,
        "display_name": h.display_name or h.name,
        "enabled": h.enabled,
        "status": host_status(h, now),
        "last_seen": h.last_seen,
        "age_sec": (now - h.last_seen) if h.last_seen else None,
        "notes": h.notes,
        "summary": _summary_of(h.last_sample),
        "check_rev": h.check_rev,
        "agent_rev": (h.last_sample or {}).get("config_rev"),
        "agent_version": ((h.last_sample or {}).get("host") or {}).get("agent_version"),
        "agent_sha256": (h.last_sample or {}).get("agent_sha256"),
        "agent_outdated": bool(agent_bundle.available and (h.last_sample or {}).get("agent_sha256")
                               and (h.last_sample or {}).get("agent_sha256") != agent_bundle.sha256),
        "agent_update_error": (h.last_sample or {}).get("agent_update_error"),
    }
    if full:
        out["sample"] = h.last_sample
        out["check_config"] = h.check_config or {}
        out["pending_cmd"] = h.pending_cmd
    return out


# ------------------------------------------------------------ auth helpers
def client_ip(request: Request) -> str:
    return request.client.host if request.client else "0.0.0.0"


def require_viewer(request: Request) -> Identity:
    try:
        return authz.viewer(request.headers, request.cookies, client_ip(request))
    except AuthError as e:
        raise HTTPException(e.status, e.detail)


# ------------------------------------------------------- background tasks
async def maintenance_loop() -> None:
    last_prune = 0.0
    while True:
        try:
            await run_in_threadpool(db.rollup)
            if time.time() - last_prune > 3600:
                a, b = await run_in_threadpool(db.prune, settings.retention_raw_hours, settings.retention_1m_days)
                last_prune = time.time()
                if a or b:
                    log.info("pruned %d raw rows, %d rollup rows", a, b)
        except Exception:
            log.exception("maintenance failed")
        await asyncio.sleep(60)


async def presence_loop() -> None:
    """Detect online/offline transitions and notify dashboards."""
    while True:
        try:
            hosts = await run_in_threadpool(db.list_hosts)
            for h in hosts:
                online = host_status(h) == "online"
                prev = hub.online.get(h.id)
                hub.online[h.id] = online
                if prev is not None and prev != online:
                    log.info("host %s is now %s", h.name, "online" if online else "OFFLINE")
                    await hub.broadcast({"type": "host", "host": host_public(h)})
        except Exception:
            log.exception("presence check failed")
        await asyncio.sleep(5)


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Avalon Monitor %s starting on %s:%s (db=%s)", __version__, settings.bind_host, settings.bind_port, settings.db_path)
    tasks = [asyncio.create_task(maintenance_loop()), asyncio.create_task(presence_loop())]
    try:
        yield
    finally:
        for t in tasks:
            t.cancel()
        db.close()


app = FastAPI(title="Avalon Monitor", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response: Response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
        "script-src 'self'; connect-src 'self' ws: wss:; frame-ancestors 'none'; base-uri 'none'",
    )
    if request.url.path == "/" or request.url.path.endswith(".html"):
        response.headers["Cache-Control"] = "no-store"
    elif request.url.path.startswith("/static/"):
        # Always revalidate (ETag) so a hub upgrade reaches open browsers without a hard refresh.
        response.headers["Cache-Control"] = "no-cache"
    return response


# ------------------------------------------------------------------ ingest
@app.post("/api/v1/ingest")
async def ingest(request: Request):
    ip = client_ip(request)
    try:
        authz.ingest_allowed(request.headers, ip)
    except AuthError as e:
        raise HTTPException(e.status, e.detail)

    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        raise HTTPException(401, "missing bearer token")
    token = auth[7:].strip()
    host = await run_in_threadpool(db.host_by_token, token)
    if host is None or not host.enabled:
        log.warning("ingest rejected from %s: unknown or disabled token", ip)
        raise HTTPException(401, "unknown or disabled token")

    body = await request.body()
    if len(body) > settings.max_payload_bytes:
        raise HTTPException(413, "payload too large")
    try:
        sample = Sample.model_validate_json(body)
    except Exception as e:  # pydantic ValidationError
        raise HTTPException(422, f"invalid payload: {e}") from None

    # Clamp wildly skewed agent clocks to server time.
    now = time.time()
    if abs(sample.ts - now) > 120:
        sample.ts = now

    # Hub-side derived alerts (need history the agent doesn't have).
    history = await run_in_threadpool(db.latest_points, host.id, "gpu_util", sample.ts - gpu_stall.window_sec)
    verdict = gpu_stall.evaluate(host.id, sample, history)
    if verdict is not None:
        sample.checks.append(Check(**verdict))

    await run_in_threadpool(db.record_sample, host, sample, ip)
    updated = await run_in_threadpool(db.host_by_name, host.name)
    reply: dict[str, Any] = {"ok": True, "server_time": now}
    if updated:
        hub.online[updated.id] = True
        await hub.broadcast({"type": "host", "host": host_public(updated)})
        # Remote config + commands ride the reply so agents need no inbound port.
        if updated.check_config is not None:
            reply["config"] = {"rev": updated.check_rev, "settings": updated.check_config}
        cmd = await run_in_threadpool(db.pop_command, updated.id)
        if cmd:
            reply["command"] = cmd
            log.info("sent command %r to %s", cmd, updated.name)
    agent_bundle.refresh()
    if agent_bundle.available:
        reply["agent"] = {"version": agent_bundle.version, "sha256": agent_bundle.sha256,
                          "auto_update": settings.agent_auto_update}
    return reply


@app.get("/api/v1/agent/script")
async def agent_script(request: Request):
    """The current agent script, for self-update. Same access rules as ingest."""
    ip = client_ip(request)
    try:
        authz.ingest_allowed(request.headers, ip)
    except AuthError as e:
        raise HTTPException(e.status, e.detail)
    auth = request.headers.get("authorization", "")
    host = await run_in_threadpool(db.host_by_token, auth[7:].strip()) if auth.lower().startswith("bearer ") else None
    if host is None or not host.enabled:
        raise HTTPException(401, "unknown or disabled token")
    agent_bundle.refresh()
    if not agent_bundle.available:
        raise HTTPException(404, "hub has no agent script to serve")
    return Response(agent_bundle.data, media_type="text/x-python",
                    headers={"X-Agent-Version": agent_bundle.version, "X-Agent-Sha256": agent_bundle.sha256})


@app.get("/api/v1/agent")
async def agent_info(ident: Identity = Depends(require_viewer)):
    agent_bundle.refresh()
    hosts = await run_in_threadpool(db.list_hosts)
    return {"version": agent_bundle.version, "sha256": agent_bundle.sha256, "path": str(agent_bundle.path),
            "auto_update": settings.agent_auto_update,
            "hosts": [{"name": h.name, "version": ((h.last_sample or {}).get("host") or {}).get("agent_version"),
                       "sha256": (h.last_sample or {}).get("agent_sha256"),
                       "update_error": (h.last_sample or {}).get("agent_update_error"),
                       "up_to_date": (h.last_sample or {}).get("agent_sha256") == agent_bundle.sha256} for h in hosts]}


# -------------------------------------------------------------- dashboard API
@app.get("/api/v1/config")
async def get_config(ident: Identity = Depends(require_viewer)):
    return {
        "version": __version__,
        "user": {"subject": ident.subject, "method": ident.method},
        "offline_after_sec": settings.offline_after_sec,
        "ranges": RANGES,
        "metrics": list(METRICS),
        "access_enabled": settings.access_enabled,
        "agent_version": agent_bundle.version,
    }


@app.get("/api/v1/hosts")
async def list_hosts(ident: Identity = Depends(require_viewer)):
    hosts = await run_in_threadpool(db.list_hosts)
    return {"hosts": [host_public(h) for h in hosts], "server_time": time.time()}


@app.get("/api/v1/hosts/{name}")
async def get_host(name: str, ident: Identity = Depends(require_viewer)):
    h = await run_in_threadpool(db.host_by_name, name)
    if not h:
        raise HTTPException(404, "no such host")
    return host_public(h, full=True)


@app.get("/api/v1/hosts/{name}/series")
async def get_series(
    name: str,
    metrics: str = Query("cpu", description="comma-separated metric names"),
    range: str = Query("1h", alias="range"),
    points: int = Query(600, ge=10, le=5000),
    ident: Identity = Depends(require_viewer),
):
    h = await run_in_threadpool(db.host_by_name, name)
    if not h:
        raise HTTPException(404, "no such host")
    keys = [m.strip() for m in metrics.split(",") if m.strip()]
    bad = [k for k in keys if k not in METRICS]
    if bad:
        raise HTTPException(400, f"unknown metrics: {', '.join(bad)}")
    if range not in RANGES:
        raise HTTPException(400, f"range must be one of {', '.join(RANGES)}")
    end = time.time()
    start = end - RANGES[range]
    data = await run_in_threadpool(
        db.series, h.id, keys, start, end, points, settings.retention_raw_hours * 3600
    )
    data.update({"host": h.name, "start": start, "end": end, "range": range})
    return data


def require_same_origin(request: Request) -> None:
    """CSRF guard for state-changing calls: browsers can't add this header cross-origin without CORS."""
    if request.headers.get("x-requested-with") != "avalon-monitor":
        raise HTTPException(403, "missing X-Requested-With header")


@app.get("/api/v1/hosts/{name}/checks")
async def get_checks(name: str, ident: Identity = Depends(require_viewer)):
    h = await run_in_threadpool(db.host_by_name, name)
    if not h:
        raise HTTPException(404, "no such host")
    return {"host": h.name, "rev": h.check_rev, "settings": h.check_config or {}, "keys": REMOTE_KEYS,
            "agent_rev": (h.last_sample or {}).get("config_rev")}


@app.put("/api/v1/hosts/{name}/checks")
async def put_checks(name: str, request: Request, ident: Identity = Depends(require_viewer)):
    require_same_origin(request)
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(422, "expected an object of AVM_* settings")
    bad = [k for k in body if k not in REMOTE_KEYS]
    if bad:
        raise HTTPException(422, f"not remotely settable: {', '.join(bad)}")
    settings_out = {k: str(v).strip() for k, v in body.items() if v is not None}
    try:
        rev = await run_in_threadpool(db.set_check_config, name, settings_out)
    except KeyError:
        raise HTTPException(404, "no such host")
    log.info("%s updated checks for %s (rev %d)", ident.subject, name, rev)
    return {"ok": True, "rev": rev, "settings": settings_out}


@app.post("/api/v1/hosts/{name}/command/{cmd}")
async def post_command(name: str, cmd: str, request: Request, ident: Identity = Depends(require_viewer)):
    require_same_origin(request)
    if cmd not in AGENT_COMMANDS:
        raise HTTPException(400, f"command must be one of {', '.join(AGENT_COMMANDS)}")
    try:
        await run_in_threadpool(db.set_command, name, cmd)
    except KeyError:
        raise HTTPException(404, "no such host")
    log.info("%s queued %r for %s", ident.subject, cmd, name)
    return {"ok": True, "queued": cmd, "note": "delivered with the agent's next push"}


@app.get("/api/v1/stats")
async def get_stats(ident: Identity = Depends(require_viewer)):
    return await run_in_threadpool(db.stats)


@app.websocket("/api/v1/live")
async def live(ws: WebSocket):
    ip = ws.client.host if ws.client else "0.0.0.0"
    try:
        authz.viewer(ws.headers, ws.cookies, ip)
    except AuthError:
        await ws.close(code=4401)
        return
    await ws.accept()
    hub.clients.add(ws)
    try:
        hosts = await run_in_threadpool(db.list_hosts)
        await ws.send_text(json.dumps({"type": "snapshot", "hosts": [host_public(h) for h in hosts],
                                       "server_time": time.time()}, separators=(",", ":")))
        while True:
            # Clients send periodic pings; anything else is ignored.
            msg = await ws.receive_text()
            if msg == "ping":
                await ws.send_text('{"type":"pong"}')
    except WebSocketDisconnect:
        pass
    except Exception:
        log.debug("websocket closed", exc_info=True)
    finally:
        hub.clients.discard(ws)


# ------------------------------------------------------------------ static
@app.get("/healthz")
async def healthz():
    return {"ok": True, "version": __version__}


@app.get("/")
async def index(request: Request):
    # Authenticate the shell too, so an unauthenticated visitor sees nothing.
    try:
        authz.viewer(request.headers, request.cookies, client_ip(request))
    except AuthError as e:
        return JSONResponse({"detail": e.detail}, status_code=e.status)
    html = (STATIC_DIR / "index.html").read_text().replace("__V__", __version__)
    return Response(html, media_type="text/html", headers={"Cache-Control": "no-store"})


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def run() -> None:
    import uvicorn

    uvicorn.run(
        "avalon_monitor.main:app",
        host=settings.bind_host,
        port=settings.bind_port,
        log_level=settings.log_level,
        proxy_headers=False,   # never trust X-Forwarded-For: client IP decides the auth policy
        ws_ping_interval=20,
        ws_ping_timeout=20,
    )


if __name__ == "__main__":
    run()
