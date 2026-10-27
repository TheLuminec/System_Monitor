"""deploy/sign-release.py (audit F-02) against a throwaway world: a temp git repo with a bare "origin", a temp
Ed25519 release key made here (passphrase-encrypted), and HOME / AVM_RELEASE_KEY pointing into tmp_path, so the real
key in ~/.config/avalon-release is never read. The signer runs as it would for the owner, `/usr/bin/python3 -I` with
the system cryptography, in its own session (no controlling terminal: getpass could never block on /dev/tty), and
gets the passphrase through the hidden test-only --passphrase-fd."""
import base64
import hashlib
import importlib.util
import os
import shutil
import subprocess
import sys
import zlib
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

import avalon_agent

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "sign-release.py"
SYS_PY = "/usr/bin/python3"
PASS = "correct horse battery staple"


def _system_python_ok() -> bool:
    if not (os.path.exists(SYS_PY) and shutil.which("git")):
        return False
    r = subprocess.run([SYS_PY, "-I", "-c", "import cryptography.hazmat.primitives.asymmetric.ed25519"], capture_output=True)
    return r.returncode == 0


needs_system_python = pytest.mark.skipif(not _system_python_ok(),
                                         reason="needs git and /usr/bin/python3 with the system cryptography")


def load_signer():
    spec = importlib.util.spec_from_file_location("sign_release", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)   # no side effects at import: no repo lookup, no key, no agent import
    return mod


def agent_text(pub, version="1.0.0", extra=""):
    return ('AGENT_VERSION = "%s"\nRELEASE_PUBKEYS: Tuple[str, ...] = ("%s",)   # test key\n'
            "def release_message(name, data):\n    pass\n%s" % (version, pub, extra))


class World:
    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.home = tmp / "home"
        self.home.mkdir()
        key = Ed25519PrivateKey.generate()
        self.pub = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()
        self.key_path = tmp / "keys" / "release-key.pem"
        self.key_path.parent.mkdir()
        self.key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                    serialization.BestAvailableEncryption(PASS.encode())))
        self.env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(self.home), "LANG": "C.UTF-8",
                    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
                    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.test",
                    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.test",
                    "AVM_RELEASE_KEY": str(self.key_path)}
        self.origin, self.repo = tmp / "origin.git", tmp / "repo"
        self.git("init", "-q", "--bare", "-b", "main", str(self.origin), cwd=tmp)
        self.git("init", "-q", "-b", "main", str(self.repo), cwd=tmp)
        self.git("remote", "add", "origin", str(self.origin))
        self.write("deploy/sign-release.py", SCRIPT.read_text())
        self.write("agent/avalon_agent.py", agent_text(self.pub))
        self.write("agent/avm.py", 'AVM_VERSION = "1.0.0"\nprint("avm")\n')
        self.write("agent/avalon_agent.py.sig", "old\n")
        self.write("agent/avm.py.sig", "old\n")
        self.release1 = self.commit("release 1.0.0", push=True)
        self.write("agent/avm.py", 'AVM_VERSION = "1.1.0"\nprint("avm, now better")\n')
        self.head = self.commit("avm: a reviewed change", push=True)

    def git(self, *args, cwd=None):
        return subprocess.run(["git", *args], cwd=cwd or self.repo, env=self.env, check=True,
                              capture_output=True, text=True).stdout.strip()

    def write(self, rel, text):
        p = self.repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)

    def commit(self, msg, push=False):
        self.git("add", "-A")
        self.git("commit", "-q", "-m", msg)
        if push:
            self.git("push", "-q", "origin", "main")
        return self.git("rev-parse", "HEAD")

    def sign(self, *args, python=(SYS_PY, "-I"), passphrase=PASS, script=None):
        cmd = [*python, str(script or self.repo / "deploy" / "sign-release.py"), "--passphrase-fd", "0", *args]
        return subprocess.run(cmd, cwd=self.repo, env=self.env, input=passphrase + "\n", capture_output=True, text=True,
                              timeout=120, start_new_session=True)

    def sig(self, name):
        p = self.repo / "agent" / (name + ".sig")
        return p.read_text() if p.exists() else None

    def committed(self, sha, name):
        return subprocess.run(["git", "show", "%s:agent/%s" % (sha, name)], cwd=self.repo, env=self.env, check=True,
                              capture_output=True).stdout


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def _verifies(pub, name, data, sig_text):
    try:
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(pub)).verify(base64.b64decode(sig_text.strip()),
                                                                      avalon_agent.release_message(name, data))
        return True
    except Exception:
        return False


# ------------------------------------------------------------------ the format stays the agent's
def test_release_message_is_byte_identical_to_the_agents():
    """F-02: the signer inlines release_message instead of importing working-tree code; it must never drift."""
    sr = load_signer()
    samples = [b"", b"print(1)\n", b"a\r\nb\r\n", b"mixed\r\nline\nendings\r", "unicode é\n".encode(),
               (ROOT / "agent" / "avm.py").read_bytes(), (ROOT / "agent" / "avalon_agent.py").read_bytes()]
    for name in ("avalon_agent.py", "avm.py"):
        for data in samples:
            assert sr.release_message(name, data) == avalon_agent.release_message(name, data)


def test_keys_and_versions_read_by_regex_match_the_agent():
    sr = load_signer()
    src = (ROOT / "agent" / "avalon_agent.py").read_text()
    assert sr.embedded_keys(src) == list(avalon_agent.RELEASE_PUBKEYS)
    assert sr.version_of(src, "AGENT_VERSION") == avalon_agent.AGENT_VERSION
    import avm
    assert sr.version_of((ROOT / "agent" / "avm.py").read_text(), "AVM_VERSION") == avm.AVM_VERSION


def test_signer_never_imports_the_agent():
    """Static (source text only, F-17): the signer neither imports avalon_agent nor locates the repo via __file__;
    the behaviour itself is covered by the runs below, including the stdin (`-`) one."""
    text = SCRIPT.read_text()
    assert "import avalon_agent" not in text and "sys.path.insert" not in text and "__file__" not in text


# ------------------------------------------------------------------ signing
@needs_system_python
def test_signs_the_pushed_commit_and_prints_what_it_signs(world):
    r = world.sign()
    assert r.returncode == 0, r.stdout + r.stderr
    out = r.stdout
    assert world.head in out and "avm: a reviewed change" in out
    for name in ("avalon_agent.py", "avm.py"):
        data = world.committed(world.head, name)
        assert hashlib.sha256(data).hexdigest() in out
        assert _verifies(world.pub, name, data, world.sig(name))
    assert "agent 1.0.0, avm 1.1.0" in out
    assert world.release1[:12] in out and "agent/avm.py" in out   # diffstat since the last signed release
    assert "Compare the sha" in out
    # all of that is printed before the passphrase is asked for (the key is only read after it)
    assert out.index("Compare the sha") < out.index("signed  agent/avm.py")


@needs_system_python
def test_committed_copy_from_stdin_with_repo_arg(world, tmp_path):
    """The documented invocation: `git show origin/main:deploy/sign-release.py | /usr/bin/python3 -I - --repo ...`
    (no __file__), run from another directory; the passphrase comes on fd 3 since stdin carries the script."""
    script = world.git("show", "origin/main:deploy/sign-release.py") + "\n"
    rfd, wfd = os.pipe()
    os.write(wfd, (PASS + "\n").encode())
    os.close(wfd)
    try:
        r = subprocess.run([SYS_PY, "-I", "-", "--repo", str(world.repo), "--passphrase-fd", str(rfd)], cwd=tmp_path,
                           env=world.env, input=script, capture_output=True, text=True, timeout=120,
                           start_new_session=True, pass_fds=(rfd,))
    finally:
        os.close(rfd)
    assert r.returncode == 0, r.stdout + r.stderr
    assert _verifies(world.pub, "avm.py", world.committed(world.head, "avm.py"), world.sig("avm.py"))


@needs_system_python
def test_refuses_an_unpushed_commit(world):
    world.write("agent/avm.py", 'AVM_VERSION = "9.9.9"\nprint("not reviewed")\n')
    local = world.commit("local only")
    r = world.sign("--commit", "HEAD")
    assert r.returncode == 1 and "not on origin/main" in r.stderr
    assert local in r.stderr
    assert world.sig("avm.py") == "old\n"


@needs_system_python
def test_refuses_a_dirty_agent_tree(world):
    """The working tree differs from the commit: what would be committed next is not what gets signed - refuse,
    before the passphrase is asked for and before the key is read."""
    world.write("agent/avm.py", 'AVM_VERSION = "1.1.0"\nprint("tampered in the checkout")\n')
    r = world.sign()
    assert r.returncode == 1 and "working tree differs" in r.stderr and "agent/avm.py" in r.stderr
    assert "passphrase" not in r.stdout.lower()
    assert world.sig("avm.py") == "old\n" and world.sig("avalon_agent.py") == "old\n"
    world.write("agent/extra.py", "x = 1\n")   # an untracked agent/*.py is a difference too
    world.git("checkout", "--", "agent/avm.py")
    r = world.sign()
    assert r.returncode == 1 and "agent/extra.py" in r.stderr


@needs_system_python
def test_local_branch_behind_origin_still_signs_origin_main_bytes(world):
    """origin/main moved on elsewhere; the local checkout is behind, so its agent/ differs from origin/main: refuse.
    After pulling, it signs origin/main's bytes - never the stale local ones."""
    other = world.tmp / "other"
    world.git("clone", "-q", str(world.origin), str(other), cwd=world.tmp)
    (other / "agent" / "avm.py").write_text('AVM_VERSION = "1.2.0"\nprint("pushed from elsewhere")\n')
    world.git("commit", "-q", "-am", "avm 1.2.0", cwd=other)
    world.git("push", "-q", "origin", "main", cwd=other)
    newer = world.git("rev-parse", "HEAD", cwd=other)
    r = world.sign()
    assert r.returncode == 1 and newer[:12] in r.stderr and "working tree differs" in r.stderr
    world.git("pull", "-q", "--ff-only", "origin", "main")
    r = world.sign()
    assert r.returncode == 0, r.stdout + r.stderr
    assert newer in r.stdout
    assert _verifies(world.pub, "avm.py", world.committed(newer, "avm.py"), world.sig("avm.py"))


@needs_system_python
def test_refuses_a_tampered_object_store(world):
    """A loose object rewritten under the same name (the object store is as writable as the checkout): `git show`
    would hand back the new bytes, the signer's own hash check refuses them."""
    blob = world.git("rev-parse", "%s:agent/avm.py" % world.head)
    path = world.repo / ".git" / "objects" / blob[:2] / blob[2:]
    evil = b'AVM_VERSION = "1.1.0"\nprint("evil")\n'
    os.chmod(path, 0o644)
    path.write_bytes(zlib.compress(b"blob %d\0" % len(evil) + evil))
    world.write("agent/avm.py", evil.decode())   # the checkout agrees with the forged object
    r = world.sign()
    assert r.returncode == 1 and "does not match its hash" in r.stderr
    assert world.sig("avm.py") == "old\n"


@needs_system_python
def test_wrong_passphrase_signs_nothing(world):
    r = world.sign(passphrase="not the passphrase")
    assert r.returncode == 1 and "wrong passphrase" in r.stdout
    assert world.sig("avm.py") == "old\n"


# ------------------------------------------------------------------ the interpreter
@needs_system_python
def test_refuses_a_non_isolated_interpreter(world):
    r = world.sign(python=(SYS_PY,))
    assert r.returncode == 1 and "not running isolated" in r.stderr
    assert "show origin/main:deploy/sign-release.py | /usr/bin/python3 -I -" in r.stderr   # the exact command
    assert "Compare the sha" not in r.stdout and world.sig("avm.py") == "old\n"


@pytest.mark.skipif(sys.prefix == sys.base_prefix, reason="the test suite is not running from a virtualenv")
def test_refuses_the_repo_venv(world):
    r = world.sign(python=(sys.executable, "-I"))
    assert r.returncode == 1 and "virtualenv" in r.stderr
    assert "/usr/bin/python3 -I -" in r.stderr
    assert world.sig("avm.py") == "old\n"


@needs_system_python
def test_refuses_cryptography_from_home(world):
    """A cryptography planted on sys.path inside $HOME (e.g. via a .pth or a copied interpreter) is refused before it
    is imported."""
    fake = world.home / "lib" / "cryptography"
    fake.mkdir(parents=True)
    (fake / "__init__.py").write_text("raise SystemExit('planted cryptography ran')\n")
    shim = world.tmp / "shim.py"
    shim.write_text("import sys, runpy\nsys.path.insert(0, %r)\nsys.argv = sys.argv[1:]\n"
                    "runpy.run_path(sys.argv[0], run_name='__main__')\n" % str(world.home / "lib"))
    r = world.sign(python=(SYS_PY, "-I", str(shim)))
    assert r.returncode == 1 and "inside the repository or $HOME" in r.stderr
    assert "planted cryptography ran" not in r.stdout + r.stderr


# ------------------------------------------------------------------ --check
@needs_system_python
def test_check_working_tree_and_commit(world):
    r = world.sign()
    assert r.returncode == 0, r.stdout + r.stderr
    chk = subprocess.run([sys.executable, str(world.repo / "deploy" / "sign-release.py"), "--check"], cwd=world.repo,
                         env=world.env, capture_output=True, text=True, timeout=60)
    assert chk.returncode == 0 and chk.stdout.count("ok") == 2, chk.stdout
    signed = world.commit("Release signatures", push=True)
    chk = subprocess.run([sys.executable, str(world.repo / "deploy" / "sign-release.py"), "--check", "--commit", signed],
                         cwd=world.repo, env=world.env, capture_output=True, text=True, timeout=60)
    assert chk.returncode == 0 and signed in chk.stdout, chk.stdout + chk.stderr
    chk = subprocess.run([sys.executable, str(world.repo / "deploy" / "sign-release.py"), "--check", "--commit",
                          world.release1], cwd=world.repo, env=world.env, capture_output=True, text=True, timeout=60)
    assert chk.returncode == 1 and "MISSING" in chk.stdout   # release 1's placeholder .sig files are not signatures
