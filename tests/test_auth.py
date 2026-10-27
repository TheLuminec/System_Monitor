"""Audit E-01/E-02/E-03/E-16: who may change things, Host allowlist, Origin checks on writes and the live socket."""
import re

import pytest
from fastapi.routing import APIRoute
from starlette.websockets import WebSocketDisconnect

from conftest import WRITE, access_token, report_policy
from avalon_monitor import core
from avalon_monitor.config import settings



def _routes(routes):
    for r in routes:
        if isinstance(r, APIRoute):
            yield r
        inner = getattr(r, "original_router", None)   # FastAPI >= 0.13x wraps include_router() results
        if inner is not None:
            yield from _routes(inner.routes)


def _write_routes(app):
    for r in _routes(app.routes):
        for m in sorted(r.methods & {"POST", "PUT", "PATCH", "DELETE"}):
            yield m, r.path


def test_route_walk_sees_the_routers():
    """Guard for the parametrisation below: it must cover the included routers, not just main.py."""
    paths = {p for _m, p in _write_routes(__import__("avalon_monitor.main").main.app)}
    assert {"/api/v1/claude/jobs", "/api/v1/hosts/{name}/checklist", "/api/v1/tasks/ack"} <= paths, paths
    assert len(paths) > 25


def test_trusted_network_can_view_but_is_read_only(client):
    cfg = client.get("/api/v1/config").json()
    assert cfg["user"]["method"] == "trusted-network" and cfg["can_change"] is False


def test_trusted_network_cannot_queue_a_full_access_prompt(client, host):
    """E-01: the exploit as reported - a no-login tailnet/localhost caller queueing --dangerously-skip-permissions."""
    r = client.post("/api/v1/claude/jobs", headers=WRITE,
                    json={"host": host[0].name, "prompt": "x", "mode": "full", "confirm_full": True})
    assert r.status_code == 403 and "Access login" in r.json()["detail"]
    assert not [j for j in core.db.job_list(host[0].id)]


VIEWER_WRITES = {("PUT", "/api/v1/prefs")}   # dashboard layout only - read-only viewers may arrange their view


def _guard(route):
    calls = {d.call for d in route.dependant.dependencies}
    if core.require_operator in calls:
        return "operator"
    if core.require_agent in calls:
        return "agent"
    return "ingest" if route.path == "/api/v1/ingest" else "viewer"   # ingest authenticates inside the handler


ALL_WRITES = [(m, r.path, _guard(r)) for r in _routes(__import__("avalon_monitor.main").main.app.routes)
              for m in sorted(r.methods & {"POST", "PUT", "PATCH", "DELETE"})]


def test_every_write_route_is_guarded():
    """Static: a new route that changes state must say who may call it - an Access operator or an agent token."""
    loose = [(m, p) for m, p, g in ALL_WRITES if g == "viewer" and (m, p) not in VIEWER_WRITES]
    assert not loose, f"write routes guarded only by require_viewer: {loose}"


@pytest.mark.parametrize("method,path,guard", ALL_WRITES)
def test_every_write_route_refuses_trusted_network(client, method, path, guard):
    """Dynamic: a no-login loopback/tailnet caller gets 403 (operator routes) or 401 (agent-token routes)."""
    if (method, path) in VIEWER_WRITES:
        return
    r = client.request(method, re.sub(r"\{[^}]+\}", "1", path), headers=WRITE, json={})
    assert r.status_code == {"operator": 403, "agent": 401, "ingest": 401}[guard], (method, path, r.status_code, r.text)


def test_access_login_may_change_things(tunnel, agent, host):
    report_policy(agent)
    cfg = tunnel.get("/api/v1/config").json()
    assert cfg["user"] == {"subject": "owner@example.test", "method": "access"} and cfg["can_change"] is True
    r = tunnel.post("/api/v1/claude/jobs", headers=WRITE, json={"host": host[0].name, "prompt": "hello", "mode": "plan"})
    assert r.status_code == 200, r.text
    assert tunnel.post("/api/v1/claude/jobs/ack", headers=WRITE, json={"ids": []}).status_code == 200


def test_operator_allowlist(tunnel, monkeypatch):
    monkeypatch.setattr(settings, "operators", ["someone@else.test"])
    r = tunnel.post("/api/v1/claude/jobs/ack", headers=WRITE, json={"ids": []})
    assert r.status_code == 403 and "AVM_OPERATORS" in r.json()["detail"]
    monkeypatch.setattr(settings, "operators", ["owner@example.test"])
    assert tunnel.post("/api/v1/claude/jobs/ack", headers=WRITE, json={"ids": []}).status_code == 200


@pytest.mark.parametrize("operators,trusted_write", [([], False), (["someone@else.test"], False), (["owner@example.test"], False), ([], True)])
def test_config_can_change_matches_the_server_answer(client, tunnel, monkeypatch, operators, trusted_write):
    """The dashboard greys out controls from /config's can_change: it must agree with what a write actually gets."""
    monkeypatch.setattr(settings, "operators", operators)
    monkeypatch.setattr(settings, "trusted_write", trusted_write)
    for c in (client, tunnel):
        can = c.get("/api/v1/config").json()["can_change"]
        status = c.post("/api/v1/claude/jobs/ack", headers=WRITE, json={"ids": []}).status_code
        assert can is (status == 200), (operators, trusted_write, can, status)


def test_trusted_write_switch_restores_old_behaviour(client, monkeypatch):
    monkeypatch.setattr(settings, "trusted_write", True)
    assert client.post("/api/v1/claude/jobs/ack", headers=WRITE, json={"ids": []}).status_code == 200


def test_forged_cloudflare_headers_get_no_trusted_access(client):
    """A page that adds cf-* headers itself (e.g. after DNS rebinding) lands on the Access path and has no JWT."""
    r = client.get("/api/v1/config", headers={"cf-ray": "x", "host": "evil.example"})
    assert r.status_code == 401
    bad = access_token(aud="other-app")
    assert client.get("/api/v1/config", headers={"cf-access-jwt-assertion": bad}).status_code == 401


@pytest.mark.parametrize("host,ok", [
    ("testserver", True), ("127.0.0.1:8787", True), ("100.64.1.2:8787", True), ("[fd7a::1]:8787", True),
    ("localhost:8787", True), ("monitor.example.test", True),
    (f"{core._HOSTNAME}.some-tailnet.ts.net", True), (f"{core._HOSTNAME}:8787", True),
    ("evil.example", False), ("evil.example:8787", False), ("attacker.some-tailnet.ts.net", False),
    (f"{core._HOSTNAME}.evil.example", False), ("", False),
])
def test_host_allowlist(client, host, ok):
    """E-02: DNS rebinding - a request carrying a foreign Host is refused before auth."""
    r = client.get("/api/v1/config", headers={"host": host})
    assert (r.status_code != 421) is ok, (host, r.status_code)


def test_cross_origin_writes_refused_even_with_header(tunnel):
    for extra in ({"origin": "https://evil.example"}, {"origin": "null"}, {"sec-fetch-site": "cross-site"}):
        r = tunnel.post("/api/v1/claude/jobs/ack", headers={**WRITE, **extra}, json={"ids": []})
        assert r.status_code == 403, extra
    ok = tunnel.post("/api/v1/claude/jobs/ack", json={"ids": []},
                     headers={**WRITE, "origin": "https://monitor.example.test", "sec-fetch-site": "same-origin"})
    assert ok.status_code == 200


def test_live_socket_refuses_foreign_origin(client):
    """E-03: cross-site WebSocket hijacking."""
    with pytest.raises(WebSocketDisconnect) as e:
        with client.websocket_connect("/api/v1/live", headers={"origin": "http://evil.example"}) as ws:
            ws.receive_text()
    assert e.value.code == 4403
    with client.websocket_connect("/api/v1/live", headers={"origin": "http://testserver"}) as ws:
        assert '"snapshot"' in ws.receive_text()


def test_live_socket_refuses_foreign_host(client):
    with pytest.raises(WebSocketDisconnect) as e:
        with client.websocket_connect("/api/v1/live", headers={"host": "evil.example"}) as ws:
            ws.receive_text()
    assert e.value.code == 4421


def test_agents_unaffected(agent):
    """Agents talk to the tailnet IP with their token; nothing here may break ingest."""
    r = agent.post("/api/v1/tasks", json={"name": "t", "source": "run"}, headers={"host": "100.64.1.2:8787"})
    assert r.status_code == 200, r.text
