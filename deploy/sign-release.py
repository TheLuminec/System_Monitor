#!/usr/bin/env python3
"""Sign an agent release so the fleet will install it.

Agents (1.9+) only self-update to code - the agent script and avm.py - that carries an Ed25519 signature by a key
they already trust (RELEASE_PUBKEYS in agent/avalon_agent.py, or AVM_UPDATE_PUBKEYS in a host's agent.conf). The
private key is encrypted with a passphrase only you know, so neither the hub, nor its code directory, nor a Claude
session can produce a release on its own.

    .venv/bin/python deploy/sign-release.py --init     # once: create the key (asks for a passphrase twice),
                                                       # prints the public key to put into RELEASE_PUBKEYS
    .venv/bin/python deploy/sign-release.py            # sign agent/avalon_agent.py + agent/avm.py -> *.sig
    .venv/bin/python deploy/sign-release.py --check    # verify the committed signatures (no passphrase)

Commit the .sig files with the release. tests/test_release.py fails while a signature is missing or stale, and
deploy-hub.sh runs the tests, so an unsigned agent change cannot be deployed by accident.
"""
import argparse
import base64
import getpass
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AGENT_DIR = ROOT / "agent"
FILES = ("avalon_agent.py", "avm.py")
KEY_PATH = Path(os.environ.get("AVM_RELEASE_KEY", Path.home() / ".config" / "avalon-release" / "release-key.pem"))

sys.path.insert(0, str(AGENT_DIR))
import avalon_agent  # noqa: E402  (release_message / ed25519_verify: the exact code the agents run)


def embedded_keys() -> list:
    src = (AGENT_DIR / "avalon_agent.py").read_text()
    m = re.search(r"^RELEASE_PUBKEYS: Tuple\[str, \.\.\.\] = \(([^)]*)\)", src, re.M)
    return re.findall(r'"([0-9a-f]{64})"', m.group(1)) if m else []


def check() -> int:
    keys = [bytes.fromhex(k) for k in embedded_keys()]
    if not keys:
        print("RELEASE_PUBKEYS is empty in agent/avalon_agent.py - run --init and add the printed key")
        return 1
    bad = 0
    for name in FILES:
        sig_path = AGENT_DIR / (name + ".sig")
        try:
            sig = base64.b64decode(sig_path.read_text().strip(), validate=True)
        except (OSError, ValueError):
            print("MISSING  %s" % sig_path.relative_to(ROOT))
            bad += 1
            continue
        msg = avalon_agent.release_message(name, (AGENT_DIR / name).read_bytes())
        ok = any(avalon_agent.ed25519_verify(k, msg, sig) for k in keys)
        print("%s  %s" % ("ok      " if ok else "STALE   ", sig_path.relative_to(ROOT)))
        bad += not ok
    return 1 if bad else 0


def init() -> int:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    if KEY_PATH.exists():
        print("%s already exists - refusing to overwrite a release key" % KEY_PATH)
        return 1
    pw = getpass.getpass("New release-key passphrase: ")
    if len(pw) < 12 or pw != getpass.getpass("Again: "):
        print("passphrases differ or shorter than 12 characters")
        return 1
    key = Ed25519PrivateKey.generate()
    KEY_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.BestAvailableEncryption(pw.encode()))
    fd = os.open(KEY_PATH, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(pem)
    pub = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()
    KEY_PATH.with_suffix(".pub").write_text(pub + "\n")   # public half: not secret, read when embedding the key
    print("release key written to %s (encrypted; back it up with the passphrase somewhere safe)" % KEY_PATH)
    print("public key: %s  (also in %s)" % (pub, KEY_PATH.with_suffix(".pub")))
    return 0


def sign() -> int:
    from cryptography.hazmat.primitives import serialization
    try:
        pem = KEY_PATH.read_bytes()
    except OSError:
        print("no release key at %s - run --init first" % KEY_PATH)
        return 1
    try:
        key = serialization.load_pem_private_key(pem, password=getpass.getpass("Release-key passphrase: ").encode())
    except ValueError:
        print("wrong passphrase - nothing signed")
        return 1
    pub = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()
    if pub not in embedded_keys():
        print("warning: this key (%s...) is not in RELEASE_PUBKEYS - agents running this release would refuse the next one" % pub[:16])
    for name in FILES:
        sig = key.sign(avalon_agent.release_message(name, (AGENT_DIR / name).read_bytes()))
        (AGENT_DIR / (name + ".sig")).write_text(base64.b64encode(sig).decode() + "\n")
        print("signed  agent/%s" % name)
    return check()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--init", action="store_true", help="create the release key")
    g.add_argument("--check", action="store_true", help="verify the committed signatures")
    a = ap.parse_args()
    return init() if a.init else check() if a.check else sign()


if __name__ == "__main__":
    sys.exit(main())
