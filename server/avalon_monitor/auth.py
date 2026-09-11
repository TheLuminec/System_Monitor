"""Request authorization.

Two audiences:

* **Viewers** (the dashboard). A request is allowed when it carries a valid
  Cloudflare Access JWT for this application, or when it arrives *directly*
  (not via Cloudflare) from a trusted network such as the tailnet.
* **Agents** (metric ingest). A request is allowed when it carries a valid
  per-host bearer token *and* arrives directly from an allowed network. Ingest
  through the Cloudflare tunnel is always refused.
"""
from __future__ import annotations

import ipaddress
import logging
import threading
from dataclasses import dataclass
from typing import Mapping, Optional

import jwt
from jwt import PyJWKClient

from .config import Settings

log = logging.getLogger("avalon.auth")

CF_MARKER_HEADERS = ("cf-connecting-ip", "cf-ray", "cf-access-jwt-assertion")


@dataclass
class Identity:
    subject: str          # email or "tailnet"
    method: str           # "access" | "trusted-network"
    ip: str


class AuthError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _ip_in(ip: str, networks) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return any(addr in n for n in networks)


def via_cloudflare(headers: Mapping[str, str]) -> bool:
    return any(h in headers for h in CF_MARKER_HEADERS)


class AccessVerifier:
    """Validates Cloudflare Access application tokens (RS256, JWKS from the team domain)."""

    def __init__(self, team_domain: str, aud: str):
        self.team_domain = team_domain
        self.aud = aud
        self.issuer = f"https://{team_domain}"
        self._client = PyJWKClient(f"{self.issuer}/cdn-cgi/access/certs", cache_keys=True, lifespan=3600)
        self._lock = threading.Lock()

    def verify(self, token: str) -> dict:
        with self._lock:
            key = self._client.get_signing_key_from_jwt(token)
        return jwt.decode(
            token, key.key, algorithms=["RS256"], audience=self.aud, issuer=self.issuer,
            options={"require": ["exp", "iat", "aud", "iss"]},
        )


class Authorizer:
    def __init__(self, settings: Settings):
        self.s = settings
        self.verifier: Optional[AccessVerifier] = None
        if settings.access_enabled:
            self.verifier = AccessVerifier(settings.access_team_domain, settings.access_aud)
            log.info("Cloudflare Access verification enabled (team=%s)", settings.access_team_domain)
        else:
            log.warning("Cloudflare Access NOT configured: dashboard is reachable only from trusted networks")

    @staticmethod
    def _token_from(headers: Mapping[str, str], cookies: Mapping[str, str]) -> Optional[str]:
        return headers.get("cf-access-jwt-assertion") or cookies.get("CF_Authorization")

    def viewer(self, headers: Mapping[str, str], cookies: Mapping[str, str], ip: str) -> Identity:
        through_cf = via_cloudflare(headers)
        token = self._token_from(headers, cookies)

        if self.verifier and token:
            try:
                claims = self.verifier.verify(token)
            except jwt.PyJWTError as e:
                log.warning("Access JWT rejected from %s: %s", ip, e)
                raise AuthError(401, "invalid Cloudflare Access token")
            subject = claims.get("email") or claims.get("common_name") or claims.get("sub") or "access-user"
            return Identity(subject=subject, method="access", ip=ip)

        if not through_cf and self.s.allow_trusted_no_auth and _ip_in(ip, self.s.trusted_networks):
            return Identity(subject="tailnet", method="trusted-network", ip=ip)

        if through_cf and not self.verifier:
            # Someone reached us via the tunnel while Access is unconfigured - refuse loudly.
            raise AuthError(403, "Cloudflare Access is not configured on this server; refusing tunnel traffic")
        raise AuthError(401, "authentication required")

    def ingest_allowed(self, headers: Mapping[str, str], ip: str) -> None:
        if via_cloudflare(headers):
            raise AuthError(403, "metric ingest is not accepted through Cloudflare")
        if not _ip_in(ip, self.s.ingest_networks):
            raise AuthError(403, f"ingest not allowed from {ip}")
