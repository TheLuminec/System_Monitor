#!/usr/bin/python3 -I
"""Sign an agent release so the fleet will install it.

Agents (1.9+) only self-update to code - the agent script and avm.py - that carries an Ed25519 signature by a key
they already trust (RELEASE_PUBKEYS in agent/avalon_agent.py, or AVM_UPDATE_PUBKEYS in a host's agent.conf). The
private key is encrypted with a passphrase only the owner knows.

What gets signed is a pushed commit, never the working tree (audit F-02): the files are read from the commit on
origin/main, every git object on the way from the commit sha to the file is re-hashed here, the sha, subject, file
sha256s, versions and the agent/ diffstat since the last signed release are printed BEFORE the passphrase is asked
for, and the working tree's agent/*.py must equal that commit. Run the committed copy of this script with the system
Python (root-owned, apt's python3-cryptography), so nothing from the user-writable checkout or its .venv runs while
the passphrase is in memory:

    git fetch origin && git show origin/main:deploy/sign-release.py | /usr/bin/python3 -I -              # sign origin/main
    git fetch origin && git show origin/main:deploy/sign-release.py | /usr/bin/python3 -I - --commit SHA # sign SHA (on origin/main)
    git fetch origin && git show origin/main:deploy/sign-release.py | /usr/bin/python3 -I - --init       # once: create the key
    .venv/bin/python deploy/sign-release.py --check [--commit REF]   # verify signatures (no passphrase; any Python)

Run it from the checkout (or pass --repo). Commit the .sig files it writes on top of the signed commit and push.
tests/test_release.py fails while a signature is missing or stale, and deploy-hub.sh runs the tests, so an unsigned
agent change cannot be deployed by accident. See docs/RELEASING.md and docs/SECURITY.md for what this does and does
not protect against.
"""
import argparse
import base64
import getpass
import hashlib
import importlib.util
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

FILES = ("avalon_agent.py", "avm.py")
FETCH_TIMEOUT = 60
GIT = "/usr/bin/git" if os.path.exists("/usr/bin/git") else "git"
SAFE_PYTHON = "/usr/bin/python3"
# repo-location overrides that would make git read another repository than --repo
_GIT_ENV_DROP = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
                 "GIT_COMMON_DIR", "GIT_NAMESPACE")


class Refused(Exception):
    pass


# ----------------------------------------------------------------- the release format (kept byte-identical to the agent)
def release_message(name: str, data: bytes) -> bytes:
    """What a release signature covers. MUST stay byte-identical to release_message() in agent/avalon_agent.py
    (tests/test_sign_release.py compares them); inlined so signing never imports working-tree code."""
    return ("avalon-release:v1:%s:%s" % (name, hashlib.sha256(data.replace(b"\r\n", b"\n")).hexdigest())).encode()


def embedded_keys(agent_src: str) -> list:
    """RELEASE_PUBKEYS read by regex from avalon_agent.py's text (never imported)."""
    m = re.search(r"^RELEASE_PUBKEYS\b[^=\n]*=\s*\(([^)]*)\)", agent_src, re.M)
    return re.findall(r'"([0-9a-f]{64})"', m.group(1)) if m else []


def version_of(src: str, var: str) -> str:
    m = re.search(r'^%s\s*=\s*"([^"]+)"' % re.escape(var), src, re.M)
    return m.group(1) if m else "?"


def verify(pub_hex: str, msg: bytes, sig: bytes) -> bool:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    try:
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(pub_hex)).verify(sig, msg)
        return True
    except (InvalidSignature, ValueError):
        return False


# ----------------------------------------------------------------- git, trusting nothing it returns without a hash check
def git(repo: Path, *args: str, check: bool = True, timeout: float = 60, binary: bool = False):
    env = {k: v for k, v in os.environ.items() if k not in _GIT_ENV_DROP}
    env["GIT_TERMINAL_PROMPT"] = "0"
    r = subprocess.run([GIT, "-C", str(repo), *args], capture_output=True, env=env, timeout=timeout,
                       stdin=subprocess.DEVNULL)
    if check and r.returncode != 0:
        raise Refused("git %s failed: %s" % (" ".join(args), r.stderr.decode(errors="replace").strip()[:300]))
    return r if binary else r.stdout.decode(errors="replace").strip()


def read_object(repo: Path, kind: str, sha: str) -> bytes:
    """A git object's raw content, re-hashed here: the object store is as writable as the working tree, so a loose
    object rewritten under the same name must not be signed as if it were the commit's content."""
    data = git(repo, "cat-file", kind, sha, binary=True).stdout
    algo = {40: "sha1", 64: "sha256"}.get(len(sha))
    if algo is None or hashlib.new(algo, b"%s %d\0" % (kind.encode(), len(data)) + data).hexdigest() != sha:
        raise Refused("git object %s does not match its hash: the repository's object store was tampered with" % sha)
    return data


def tree_entry(repo: Path, tree_sha: str, name: str):
    """(mode, sha) of `name` in tree `tree_sha`, or None."""
    data = read_object(repo, "tree", tree_sha)
    raw_len = len(tree_sha) // 2
    i = 0
    while i < len(data):
        sp = data.index(b" ", i)
        nul = data.index(b"\0", sp)
        mode, ent = data[i:sp].decode(), data[sp + 1:nul].decode("utf-8", "surrogateescape")
        sha = data[nul + 1:nul + 1 + raw_len].hex()
        i = nul + 1 + raw_len
        if ent == name:
            return mode, sha
    return None


def commit_info(repo: Path, sha: str):
    """(tree sha, subject) parsed from the verified commit object."""
    data = read_object(repo, "commit", sha).decode("utf-8", "replace")
    head, _, msg = data.partition("\n\n")
    tree = re.match(r"tree ([0-9a-f]+)\n", head)
    if not tree:
        raise Refused("commit %s has no tree" % sha)
    return tree.group(1), (msg.strip().splitlines() or [""])[0]


def committed_file(repo: Path, tree: str, rel: str):
    """Bytes of `rel` (e.g. agent/avm.py) in the verified tree, or None when it is not there."""
    *dirs, base = rel.split("/")
    for d in dirs:
        ent = tree_entry(repo, tree, d)
        if not ent or ent[0] != "40000":
            return None
        tree = ent[1]
    ent = tree_entry(repo, tree, base)
    if not ent or not ent[0].startswith("100"):
        return None
    return read_object(repo, "blob", ent[1])


def fetch(repo: Path) -> None:
    try:
        git(repo, "fetch", "--quiet", "origin", timeout=FETCH_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise Refused("git fetch origin did not finish in %ds" % FETCH_TIMEOUT)


def resolve(repo: Path, ref: str) -> str:
    sha = git(repo, "rev-parse", "--verify", "--quiet", ref + "^{commit}", check=False)
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", sha or ""):
        raise Refused("cannot resolve %r to a commit" % ref)
    return sha


def on_origin_main(repo: Path, sha: str) -> bool:
    r = git(repo, "merge-base", "--is-ancestor", sha, "refs/remotes/origin/main", check=False, binary=True)
    return r.returncode == 0


def repo_root(arg) -> Path:
    if arg:
        return Path(arg).resolve()
    top = git(Path.cwd(), "rev-parse", "--show-toplevel", check=False)
    if not top:
        raise Refused("not inside a git checkout: run from the repository or pass --repo")
    return Path(top).resolve()


# ----------------------------------------------------------------- a clean interpreter
def _inside(p: Path, roots) -> bool:
    return any(p == r or r in p.parents for r in roots)


def interpreter_problem(repo) -> str:
    """Why this interpreter must not hold the passphrase ('' = fine): not isolated (-I), a virtualenv, or the
    interpreter / sys.path / cryptography inside the repo or $HOME or writable by us. Checked before cryptography
    is imported, so a planted package never runs."""
    if not sys.flags.isolated:
        return "Python is not running isolated (-I): PYTHON* variables, the user site and the current directory count"
    if sys.prefix != sys.base_prefix:
        return "running inside a virtualenv (%s), which is user-writable" % sys.prefix
    roots = [Path(os.path.expanduser("~")).resolve()] + ([Path(repo).resolve()] if repo else [])
    for label, p in (("the interpreter", sys.executable), ("sys.prefix", sys.prefix)):
        if _inside(Path(os.path.realpath(p)), roots):
            return "%s (%s) is inside the repository or $HOME" % (label, p)
    for p in sys.path:
        if p and _inside(Path(os.path.realpath(p)), roots):
            return "sys.path has %s, inside the repository or $HOME" % p
    spec = importlib.util.find_spec("cryptography")
    if spec is None or not spec.origin:
        return "no system cryptography package (sudo apt install python3-cryptography)"
    pkg = Path(os.path.realpath(spec.origin)).parent
    if _inside(pkg, roots):
        return "cryptography comes from %s, inside the repository or $HOME" % pkg
    if hasattr(os, "getuid"):
        for p in (Path(os.path.realpath(sys.executable)), pkg):
            st = p.stat()
            if st.st_uid != 0 or (os.getuid() != 0 and os.access(p, os.W_OK)):
                return "%s is not root-owned and read-only to you" % p
    return ""


def safe_command(repo, argv) -> str:
    keep, skip = [], False
    for a in argv:
        if skip:
            skip = False
            continue
        if a in ("--repo", "--passphrase-fd"):
            skip = True
            continue
        if a.startswith(("--repo=", "--passphrase-fd=")):
            continue
        keep.append(a)
    r = shlex.quote(str(repo)) if repo else "<checkout>"
    return ("git -C %s fetch origin && git -C %s show origin/main:deploy/sign-release.py | %s -I - --repo %s%s"
            % (r, r, SAFE_PYTHON, r, "".join(" " + shlex.quote(a) for a in keep)))


# ----------------------------------------------------------------- passphrase
class Asker:
    """getpass on the terminal; --passphrase-fd (tests only) reads one line per question from that fd instead.
    Never falls back to stdin: when the script itself arrives on stdin there is nothing there to read."""

    def __init__(self, fd):
        self.f = os.fdopen(fd, "r", closefd=False) if fd is not None else None

    def __call__(self, prompt: str) -> str:
        if self.f is not None:
            return self.f.readline().rstrip("\n")
        try:
            os.close(os.open("/dev/tty", os.O_RDWR))
        except OSError:
            raise Refused("no terminal to ask for the passphrase on")
        return getpass.getpass(prompt)


def key_path() -> Path:
    return Path(os.environ.get("AVM_RELEASE_KEY") or os.path.join(os.path.expanduser("~"), ".config", "avalon-release", "release-key.pem"))


# ----------------------------------------------------------------- modes
def check(repo: Path, ref) -> int:
    if ref:
        try:
            fetch(repo)
        except Refused as e:
            print("warning: %s - checking the local refs" % e)
        sha = resolve(repo, ref)
        tree, subject = commit_info(repo, sha)
        print("commit %s  %s%s" % (sha, subject, "" if on_origin_main(repo, sha) else "  (NOT on origin/main)"))
        get = lambda rel: committed_file(repo, tree, rel)   # noqa: E731
    else:
        def get(rel):
            try:
                return (repo / rel).read_bytes()
            except OSError:
                return None
    src = get("agent/avalon_agent.py")
    keys = embedded_keys(src.decode("utf-8", "replace")) if src else []
    if not keys:
        print("RELEASE_PUBKEYS is empty in agent/avalon_agent.py - run --init and add the printed key")
        return 1
    bad = 0
    for name in FILES:
        rel = "agent/%s.sig" % name
        raw, data = get(rel), get("agent/" + name)
        try:
            sig = base64.b64decode((raw or b"").decode().strip(), validate=True)
        except (ValueError, UnicodeDecodeError):
            sig = b""
        if not sig or data is None:
            print("MISSING  %s" % rel)
            bad += 1
            continue
        ok = any(verify(k, release_message(name, data), sig) for k in keys)
        print("%s  %s" % ("ok      " if ok else "STALE   ", rel))
        bad += not ok
    return 1 if bad else 0


def init(ask) -> int:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    kp = key_path()
    if kp.exists():
        print("%s already exists - refusing to overwrite a release key" % kp)
        return 1
    pw = ask("New release-key passphrase: ")
    if len(pw) < 12 or pw != ask("Again: "):
        print("passphrases differ or shorter than 12 characters")
        return 1
    key = Ed25519PrivateKey.generate()
    kp.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.BestAvailableEncryption(pw.encode()))
    fd = os.open(kp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(pem)
    pub = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()
    kp.with_suffix(".pub").write_text(pub + "\n")   # public half: not secret, read when embedding the key
    print("release key written to %s (encrypted; back it up with the passphrase somewhere other than this machine)" % kp)
    print("public key: %s  (also in %s)" % (pub, kp.with_suffix(".pub")))
    return 0


def sign(repo: Path, ref: str, ask) -> int:
    fetch(repo)
    sha = resolve(repo, ref)
    if not on_origin_main(repo, sha):
        raise Refused("%s (%s) is not on origin/main: push it first; only pushed, reviewed commits are signed" % (ref, sha))
    tree, subject = commit_info(repo, sha)
    content = {}
    for name in FILES:
        data = committed_file(repo, tree, "agent/" + name)
        if data is None:
            raise Refused("agent/%s is not in commit %s" % (name, sha))
        content[name] = data
    # the working tree must hold exactly what is signed, so the .sig commit on top of it ships these bytes
    on_disk = sorted({p.name for p in (repo / "agent").glob("*.py")} | set(FILES))
    dirty = []
    for name in on_disk:
        try:
            wt = (repo / "agent" / name).read_bytes()
        except OSError:
            wt = None
        committed = content.get(name) if name in content else committed_file(repo, tree, "agent/" + name)
        if wt != committed:
            dirty.append("agent/" + name)
    if dirty:
        raise Refused("the working tree differs from %s in %s: commit and push (or check out) first, so the commit "
                      "carrying the .sig files holds exactly the signed code" % (sha[:12], ", ".join(dirty)))
    agent_src = content["avalon_agent.py"].decode("utf-8", "replace")
    keys = embedded_keys(agent_src)

    last = git(repo, "log", "-1", "--format=%H", sha, "--", *("agent/%s.sig" % n for n in FILES))
    print("Signing commit %s" % sha)
    print("  subject:   %s" % subject)
    print("  origin:    %s" % git(repo, "remote", "get-url", "origin", check=False))
    print("  versions:  agent %s, avm %s" % (version_of(agent_src, "AGENT_VERSION"),
                                             version_of(content["avm.py"].decode("utf-8", "replace"), "AVM_VERSION")))
    for name in FILES:
        print("  sha256     %s  agent/%s" % (hashlib.sha256(content[name]).hexdigest(), name))
    if last:
        print("  agent/ since the last signed release (%s):" % last[:12])
        stat = git(repo, "diff", "--stat", "--no-ext-diff", "--no-textconv", "--no-color", last, sha, "--", "agent/")
        log = git(repo, "log", "--oneline", "--no-color", "%s..%s" % (last, sha), "--", "agent/")
        for line in (stat or "(no changes)").splitlines() + ["  commits:"] + (log or "(none)").splitlines()[:30]:
            print("    " + line)
    else:
        print("  agent/ since the last signed release: no earlier signed release in this history")
    if not keys:
        print("  warning: RELEASE_PUBKEYS is empty in this commit")
    print()
    print("Compare the sha above with the head of main on GitHub (from another device if you can) before typing the passphrase.")
    print("Ctrl-C to stop.")

    from cryptography.hazmat.primitives import serialization
    kp = key_path()
    try:
        pem = kp.read_bytes()
    except OSError:
        print("no release key at %s - run --init first" % kp)
        return 1
    try:
        key = serialization.load_pem_private_key(pem, password=ask("Release-key passphrase: ").encode())
    except (ValueError, TypeError):
        print("wrong passphrase - nothing signed")
        return 1
    pub = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()
    if pub not in keys:
        print("warning: this key (%s...) is not in RELEASE_PUBKEYS - agents running this release would refuse the next one" % pub[:16])
    for name in FILES:
        msg = release_message(name, content[name])
        sig = key.sign(msg)
        if not verify(pub, msg, sig):
            raise Refused("signature for agent/%s does not verify" % name)
        out = repo / "agent" / (name + ".sig")
        if out.is_symlink():
            out.unlink()
        out.write_text(base64.b64encode(sig).decode() + "\n")
        print("signed  agent/%s" % name)
    print("Now commit agent/*.sig on top of %s and push, e.g.: git add agent/*.sig && git commit -m 'Release signatures' && git push"
          % sha[:12])
    return 0


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    ap = argparse.ArgumentParser(prog="sign-release.py", description=__doc__.split("\n\n")[0])
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--init", action="store_true", help="create the release key")
    g.add_argument("--check", action="store_true", help="verify signatures (working tree, or --commit)")
    ap.add_argument("--commit", default=None, help="commit to sign (default origin/main; must be on origin/main) or check")
    ap.add_argument("--repo", default=None, help="the checkout (default: the git toplevel of the current directory)")
    ap.add_argument("--passphrase-fd", type=int, default=None, help=argparse.SUPPRESS)   # tests only
    a = ap.parse_args(argv)
    repo = None
    try:
        try:
            repo = repo_root(a.repo)
        except Refused:
            if not a.init:
                raise
        if a.check:
            return check(repo, a.commit)
        problem = interpreter_problem(repo)
        if problem:
            print("refusing to handle the release key: %s.\nRun the committed copy with the system Python instead:\n  %s"
                  % (problem, safe_command(repo, argv)), file=sys.stderr)
            return 1
        ask = Asker(a.passphrase_fd)
        return init(ask) if a.init else sign(repo, a.commit or "origin/main", ask)
    except Refused as e:
        print("refused: %s - nothing signed" % e, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nstopped - nothing signed", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
