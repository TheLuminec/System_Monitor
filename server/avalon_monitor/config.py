"""Configuration, loaded from environment variables (optionally via an env file).

Every setting has a safe default so the server can run out of the box on a
tailnet; Cloudflare Access settings are required only when exposing the
dashboard through the tunnel.
"""
from __future__ import annotations

import ipaddress
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

# Tailscale CGNAT v4 range + its ULA v6 range, plus loopback.
DEFAULT_TRUSTED_NETWORKS = "127.0.0.0/8,::1/128,100.64.0.0/10,fd7a:115c:a1e0::/48"


def _load_env_file(path: str | None) -> None:
    """Populate os.environ from KEY=VALUE lines (does not override existing vars)."""
    if not path:
        return
    p = Path(path)
    if not p.is_file():
        return
    for raw in p.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if value[:1] not in ('"', "'"):   # unquoted: an inline comment starts at " #"
            value = re.split(r"\s+#", value, maxsplit=1)[0]
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _bool(value: str | None, default: bool) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _networks(value: str) -> list[ipaddress._BaseNetwork]:
    nets = []
    for item in value.split(","):
        item = item.strip()
        if item:
            nets.append(ipaddress.ip_network(item, strict=False))
    return nets


@dataclass
class Settings:
    bind_host: str = "0.0.0.0"
    bind_port: int = 8787
    db_path: str = "data/monitor.db"
    log_level: str = "info"

    # Cloudflare Access (Zero Trust). When both are set, requests that arrive
    # through Cloudflare must carry a valid Access JWT for this application.
    access_team_domain: str = ""  # e.g. "avalontech.cloudflareaccess.com"
    access_aud: str = ""          # Application Audience (AUD) tag

    # Networks (CIDRs) that may reach the dashboard *without* an Access JWT
    # when they connect directly (i.e. not through Cloudflare). Defaults to
    # loopback + the Tailscale ranges.
    trusted_networks: list = field(default_factory=lambda: _networks(DEFAULT_TRUSTED_NETWORKS))
    allow_trusted_no_auth: bool = True
    # Trusted-network viewers are READ-ONLY: changing anything (prompts, restarts, checks, dismissals, settings)
    # needs a Cloudflare Access identity. AVM_TRUSTED_WRITE=true restores the old behaviour (e.g. a hub without
    # Access, on a private network you fully trust).
    trusted_write: bool = False
    # Access identities (emails, lower-case) allowed to change things; empty = anyone Access lets in.
    operators: list = field(default_factory=list)
    # Host header values accepted (DNS-rebinding guard). Always accepted: IP literals, localhost, this machine's
    # hostname (also as <hostname>.<tailnet>.ts.net) and the host of AVM_PUBLIC_URL; this list adds more.
    allowed_hosts: list = field(default_factory=list)
    # This hub's tailnet DNS suffix, e.g. "tail1234.ts.net": <hostname>.<it> is then the only MagicDNS name accepted.
    # Empty = detected once at startup (`tailscale status --json`, MagicDNSSuffix); if that fails too, any
    # <hostname>.*.ts.net is accepted (logged as a warning). See core.host_allowed.
    tailnet: str = ""

    # Networks agents are allowed to push metrics from.
    ingest_networks: list = field(default_factory=lambda: _networks(DEFAULT_TRUSTED_NETWORKS))

    # Data lifecycle
    retention_raw_hours: int = 24 * 7
    retention_1m_days: int = 90
    offline_after_sec: int = 30
    retention_tasks_days: int = 60
    # Failed / stalled / lost tasks alert (Alerts tab, sidebar badges, host-card chips) until
    # acknowledged on the dashboard or until this many hours after they last changed. 0 = never expire.
    task_alert_hours: float = 24
    max_payload_bytes: int = 512 * 1024

    # Derived-alert rules (hub-side). GPU stall: a host reports an active job
    # (a "job" check with value > 0) for at least `gpu_stall_minutes` while its
    # GPU utilisation never exceeds `gpu_stall_percent` -> red "gpu stalled" check.
    gpu_stall_minutes: int = 10
    gpu_stall_percent: float = 5.0

    # Agent auto-update: the hub serves this script (default: the agent/ dir shipped
    # beside server/) and agents replace themselves when its hash changes.
    agent_script: str = ""
    agent_auto_update: bool = True

    # Extra Content-Security-Policy script sources. Cloudflare's Web Analytics (RUM) injects a
    # beacon + inline snippet into HTML it proxies; allow it here or disable RUM for the hostname.
    csp_script_extra: str = "https://static.cloudflareinsights.com"

    # Public URL (informational; used in /api/v1/config for the UI)
    public_url: str = ""

    @property
    def access_enabled(self) -> bool:
        return bool(self.access_team_domain and self.access_aud)


def load_settings() -> Settings:
    _load_env_file(os.environ.get("AVM_ENV_FILE", ".env"))
    env = os.environ
    s = Settings()
    s.bind_host = env.get("AVM_BIND_HOST", s.bind_host)
    s.bind_port = int(env.get("AVM_BIND_PORT", s.bind_port))
    s.db_path = env.get("AVM_DB_PATH", s.db_path)
    s.log_level = env.get("AVM_LOG_LEVEL", s.log_level)
    s.access_team_domain = env.get("AVM_ACCESS_TEAM_DOMAIN", "").strip().removeprefix("https://").rstrip("/")
    s.access_aud = env.get("AVM_ACCESS_AUD", "").strip()
    s.trusted_networks = _networks(env.get("AVM_TRUSTED_NETWORKS", DEFAULT_TRUSTED_NETWORKS))
    s.allow_trusted_no_auth = _bool(env.get("AVM_ALLOW_TRUSTED_NO_AUTH"), True)
    s.trusted_write = _bool(env.get("AVM_TRUSTED_WRITE"), False)
    s.operators = [x.strip().lower() for x in env.get("AVM_OPERATORS", "").split(",") if x.strip()]
    s.allowed_hosts = [x.strip().lower() for x in env.get("AVM_ALLOWED_HOSTS", "").split(",") if x.strip()]
    s.tailnet = env.get("AVM_TAILNET", "").strip().lower().strip(".")
    s.ingest_networks = _networks(env.get("AVM_INGEST_NETWORKS", DEFAULT_TRUSTED_NETWORKS))
    s.retention_raw_hours = int(env.get("AVM_RETENTION_RAW_HOURS", s.retention_raw_hours))
    s.retention_1m_days = int(env.get("AVM_RETENTION_1M_DAYS", s.retention_1m_days))
    s.offline_after_sec = int(env.get("AVM_OFFLINE_AFTER_SEC", s.offline_after_sec))
    s.retention_tasks_days = int(env.get("AVM_RETENTION_TASKS_DAYS", s.retention_tasks_days))
    s.task_alert_hours = float(env.get("AVM_TASK_ALERT_HOURS", s.task_alert_hours))
    s.max_payload_bytes = int(env.get("AVM_MAX_PAYLOAD_BYTES", s.max_payload_bytes))
    s.gpu_stall_minutes = int(env.get("AVM_RULE_GPU_STALL_MINUTES", s.gpu_stall_minutes))
    s.gpu_stall_percent = float(env.get("AVM_RULE_GPU_STALL_PERCENT", s.gpu_stall_percent))
    s.agent_script = env.get("AVM_AGENT_SCRIPT", "")
    s.agent_auto_update = _bool(env.get("AVM_AGENT_AUTO_UPDATE"), True)
    s.csp_script_extra = env.get("AVM_CSP_SCRIPT_EXTRA", s.csp_script_extra).strip()
    s.public_url = env.get("AVM_PUBLIC_URL", "")
    return s


settings = load_settings()
