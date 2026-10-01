"""Shared singletons: settings, database, authorizer, live hub, agent bundle, rules."""
from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Optional

from fastapi import HTTPException, Request, WebSocket
from starlette.concurrency import run_in_threadpool

from .auth import AuthError, Authorizer, Identity
from .config import settings
from .db import Database, HostRow
from .rules import GpuStallRule

log = logging.getLogger("avalon")

AGENT_SCRIPT = Path(settings.agent_script) if settings.agent_script else Path(__file__).resolve().parents[2] / "agent" / "avalon_agent.py"


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

    def sibling(self, name: str) -> Optional[bytes]:
        """Another file shipped beside the agent script (e.g. avm.py)."""
        p = self.path.parent / name
        try:
            return p.read_bytes() if p.is_file() else None
        except OSError:
            return None


db = Database(settings.db_path)
authz = Authorizer(settings)
hub = LiveHub()
agent_bundle = AgentBundle(AGENT_SCRIPT)
gpu_stall = GpuStallRule(minutes=settings.gpu_stall_minutes, percent=settings.gpu_stall_percent)


# ------------------------------------------------------------ auth helpers
def client_ip(request: Request) -> str:
    return request.client.host if request.client else "0.0.0.0"


def require_viewer(request: Request) -> Identity:
    try:
        return authz.viewer(request.headers, request.cookies, client_ip(request))
    except AuthError as e:
        raise HTTPException(e.status, e.detail)


def require_same_origin(request: Request) -> None:
    """CSRF guard for state-changing calls: browsers can't add this header cross-origin without CORS."""
    if request.headers.get("x-requested-with") != "avalon-monitor":
        raise HTTPException(403, "missing X-Requested-With header")


async def require_agent(request: Request) -> HostRow:
    """Agent-side auth: tailnet/loopback only, never via Cloudflare, valid enabled host token."""
    ip = client_ip(request)
    try:
        authz.ingest_allowed(request.headers, ip)
    except AuthError as e:
        raise HTTPException(e.status, e.detail)
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        raise HTTPException(401, "missing bearer token")
    host = await run_in_threadpool(db.host_by_token, auth[7:].strip())
    if host is None or not host.enabled:
        log.warning("agent request rejected from %s: unknown or disabled token", ip)
        raise HTTPException(401, "unknown or disabled token")
    return host
