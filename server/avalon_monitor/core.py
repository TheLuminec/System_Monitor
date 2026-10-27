"""Shared singletons: settings, database, authorizer, live hub, agent bundle, rules."""
from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import re
import socket
import subprocess
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

from fastapi import HTTPException, Request, WebSocket
from starlette.concurrency import run_in_threadpool

from .auth import AuthError, Authorizer, Identity, via_cloudflare
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

    COMPANIONS = ("avm.py",)

    def companion_hashes(self) -> dict:
        """sha256 of each companion file, so agents can resync one that changed on its own
        (an avm-only release does not change the agent script, which is what triggers self-update)."""
        cache = self.__dict__.setdefault("_companion_cache", {})
        out = {}
        for name in self.COMPANIONS:
            p = self.path.parent / name
            try:
                mt = p.stat().st_mtime
            except OSError:
                continue
            if cache.get(name, (None,))[0] != mt:
                try:
                    cache[name] = (mt, hashlib.sha256(p.read_bytes()).hexdigest())
                except OSError:
                    continue
            out[name] = cache[name][1]
        return out

    def signature(self, name: str) -> Optional[str]:
        """The release signature shipped beside a file (agent/<name>.sig, made by deploy/sign-release.py). Agents
        refuse updates without a valid one; the hub only passes it along and cannot make one."""
        try:
            return (self.path.parent / (name + ".sig")).read_text().strip() or None
        except OSError:
            return None

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


_HOSTNAME = socket.gethostname().lower()
_PUBLIC_HOST = (urlsplit(settings.public_url).hostname or "").lower() if settings.public_url else ""
_tailnet: Optional[str] = None   # this hub's MagicDNS suffix ("" = unknown); None = not resolved yet (see tailnet())


def _detect_tailnet() -> str:
    """MagicDNSSuffix from `tailscale status --json` (e.g. "tail1234.ts.net"); "" on any failure."""
    try:
        out = subprocess.run(["tailscale", "status", "--json"], capture_output=True, timeout=3, check=False)
        suffix = json.loads(out.stdout or b"{}").get("MagicDNSSuffix") if out.returncode == 0 else ""
    except (OSError, ValueError, AttributeError, subprocess.SubprocessError):
        return ""
    return str(suffix or "").strip().lower().strip(".")


def tailnet() -> str:
    """The tailnet whose <hostname>.<tailnet> names are this hub: AVM_TAILNET, else detected once (at startup, from
    the lifespan; lazily if a request comes first). "" = neither: any <hostname>.*.ts.net passes, as before, with one
    warning - a device on another tailnet sharing this hostname could then rebind to the hub."""
    global _tailnet
    if _tailnet is None:
        _tailnet = settings.tailnet or _detect_tailnet()
        if _tailnet:
            log.info("Host allowlist: MagicDNS name %s.%s%s", _HOSTNAME, _tailnet, "" if settings.tailnet else " (detected)")
        else:
            log.warning("Host allowlist: tailnet unknown (AVM_TAILNET unset, `tailscale status --json` gave no MagicDNSSuffix):"
                        " accepting %s.<any tailnet>.ts.net - set AVM_TAILNET to pin it", _HOSTNAME)
    return _tailnet


def host_allowed(host_header: str, headers=None) -> bool:
    """DNS-rebinding guard: only Host names that are really this hub. An attacker's page can make its own name
    resolve to this machine, but its requests then carry the attacker's Host - refused here. IP literals can't be
    rebound (the page's origin would be the IP itself), so they are always fine; that is what agents use.
    Requests marked as coming through Cloudflare pass: Cloudflare only routes this tunnel's own hostnames, and such
    a request gets no trusted-network access - it needs a valid Access JWT (a page forging cf-* headers has none)."""
    if headers is not None and via_cloudflare(headers):
        return True
    host = (host_header or "").strip().lower()
    if host.startswith("["):   # [v6]:port
        host = host[1:host.find("]")] if "]" in host else host
    elif host.count(":") == 1:
        host = host.rsplit(":", 1)[0]
    host = host.rstrip(".")
    if not host:
        return False
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    if host in ("localhost", _HOSTNAME) or host == _PUBLIC_HOST or host in settings.allowed_hosts:
        return True
    pinned = tailnet()   # MagicDNS name of this machine: <hostname>.<tailnet>.ts.net - only this hub's tailnet if known
    if pinned:
        return host == f"{_HOSTNAME}.{pinned}"
    parts = host.split(".")
    return len(parts) == 4 and parts[0] == _HOSTNAME and parts[2:] == ["ts", "net"]


def same_origin(headers) -> bool:
    """True unless the request says a browser sent it from another site: an Origin that isn't this request's own
    host, or Sec-Fetch-Site cross-site/same-site. Non-browser clients (no Origin) pass."""
    origin = headers.get("origin")
    if origin and origin != "null":
        if urlsplit(origin).netloc.lower() != (headers.get("host") or "").lower():
            return False
    elif origin == "null":
        return False
    return headers.get("sec-fetch-site", "same-origin") in ("same-origin", "none")


def require_same_origin(request: Request) -> None:
    """CSRF guard for state-changing calls: browsers can't add this header cross-origin without CORS, and a
    same-origin request from a rebound name is already refused by the Host check; the Origin check is belt and braces."""
    if request.headers.get("x-requested-with") != "avalon-monitor":
        raise HTTPException(403, "missing X-Requested-With header")
    if not same_origin(request.headers):
        raise HTTPException(403, "cross-origin request refused")


def may_change(ident: Identity) -> Optional[str]:
    """None if this viewer may change things, else why not. Changing anything needs a Cloudflare Access identity
    (and, when AVM_OPERATORS is set, one on that list). Trusted-network viewers - tailnet devices, local processes -
    are read-only unless AVM_TRUSTED_WRITE is on. The dashboard greys out its controls with the same answer."""
    if ident.method != "access":
        if settings.trusted_write:
            return None
        where = settings.public_url or "the public dashboard address"
        return f"read-only here: changes need your Cloudflare Access login - open {where}"
    if settings.operators and ident.subject.lower() not in settings.operators:
        return f"{ident.subject} may view but not change things (AVM_OPERATORS)"
    return None


def require_operator(request: Request) -> Identity:
    """Guard for every dashboard route that changes state (see may_change)."""
    ident = require_viewer(request)
    require_same_origin(request)
    why = may_change(ident)
    if why:
        raise HTTPException(403, why)
    return ident


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
