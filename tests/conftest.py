"""Shared fixtures. The hub reads its settings and opens its DB at import time, so the environment is set here,
before anything imports avalon_monitor: a throwaway DB, Cloudflare Access "configured" with a test key, and
"testserver" (TestClient's Host) on the Host allowlist."""
import os
import sys
import tempfile
import time
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "server"), str(ROOT / "agent"), str(ROOT / "deploy" / "backup")]

_TMP = tempfile.mkdtemp(prefix="avm-tests-")
os.environ.update({
    "AVM_ENV_FILE": os.devnull,
    "AVM_DB_PATH": os.path.join(_TMP, "monitor.db"),
    "AVM_ACCESS_TEAM_DOMAIN": "team.example.test",
    "AVM_ACCESS_AUD": "aud-test",
    "AVM_PUBLIC_URL": "https://monitor.example.test",
    "AVM_ALLOWED_HOSTS": "testserver",
    "AVM_OPERATORS": "",
})

import jwt  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from avalon_monitor import core  # noqa: E402
from avalon_monitor.main import app  # noqa: E402

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


class _SigningKey:
    key = _KEY.public_key()


# Access tokens are verified against the test key instead of Cloudflare's JWKS
core.authz.verifier._client.get_signing_key_from_jwt = lambda token: _SigningKey()


def access_token(email="owner@example.test", **over):
    now = int(time.time())
    claims = {"email": email, "iss": "https://team.example.test", "aud": "aud-test", "iat": now, "exp": now + 600, **over}
    return jwt.encode(claims, _KEY, algorithm="RS256")


WRITE = {"X-Requested-With": "avalon-monitor"}


@pytest.fixture(scope="session")
def app_obj():
    """The app with its lifespan (background loops) running once for the whole session: entering a TestClient
    context per test would run shutdown - which closes the shared DB - after every test."""
    with TestClient(app):
        yield app


@pytest.fixture
def client(app_obj):
    """A caller on loopback (= trusted network): may view, may not change."""
    return TestClient(app_obj, client=("127.0.0.1", 50000))


@pytest.fixture
def tunnel(app_obj):
    """A caller through the Cloudflare tunnel with a valid Access login (cloudflared connects from loopback)."""
    c = TestClient(app_obj, client=("127.0.0.1", 50001))
    c.headers.update({"cf-ray": "test", "cf-access-jwt-assertion": access_token(), "host": "monitor.example.test"})
    return c


@pytest.fixture
def host():
    """A freshly enrolled host: (row, token)."""
    return core.db.create_host("t-" + uuid.uuid4().hex[:8])


@pytest.fixture
def agent(app_obj, host):
    """An agent on the tailnet for `host`."""
    row, token = host
    c = TestClient(app_obj, client=("100.64.0.9", 50002))
    c.headers.update({"Authorization": f"Bearer {token}"})
    c.host_row = row
    return c
