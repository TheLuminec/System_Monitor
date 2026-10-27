"""Release gate (E-04/E-23): the agent files in the tree must carry valid release signatures, or the fleet will refuse
them - and deploy-hub.sh refuses to ship them. Fix a failure by pushing the agent change and signing that commit (F-02):
    git fetch origin && git show origin/main:deploy/sign-release.py | /usr/bin/python3 -I -
then commit and push agent/*.sig (docs/RELEASING.md#signing-a-release)."""
import base64
from pathlib import Path

import avalon_agent as a
from avalon_monitor import core

AGENT = Path(a.__file__).resolve().parent
SIGN = "push it, then sign the pushed commit (docs/RELEASING.md#signing-a-release)"


def test_agent_trusts_a_release_key():
    assert a.RELEASE_PUBKEYS, "RELEASE_PUBKEYS is empty: run sign-release.py --init (docs/RELEASING.md) and add the printed key"
    for k in a.RELEASE_PUBKEYS:
        assert len(bytes.fromhex(k)) == 32


def test_agent_files_are_signed_by_a_release_key():
    keys = [bytes.fromhex(k) for k in a.RELEASE_PUBKEYS]
    for name in ("avalon_agent.py", "avm.py"):
        sig_file = AGENT / (name + ".sig")
        assert sig_file.exists(), f"{sig_file.name} missing: " + SIGN
        sig = base64.b64decode(sig_file.read_text().strip())
        msg = a.release_message(name, (AGENT / name).read_bytes())
        assert any(a.ed25519_verify(k, msg, sig) for k in keys), f"{name} changed since it was signed: " + SIGN


def test_hub_passes_signatures_to_agents(agent):
    """The hub forwards the .sig files in the update offer; agents verify them (tests/test_agent.py)."""
    reply = agent.post("/api/v1/ingest", json={"ts": 0, "interval": 5, "host": {"hostname": "x", "agent_version": "0"},
                                                "agent_sha256": "old"}).json()
    offer = reply.get("agent") or {}
    assert offer.get("sig") == core.agent_bundle.signature("avalon_agent.py")
    assert set(offer.get("file_sigs") or {}) == {"avm.py"}
