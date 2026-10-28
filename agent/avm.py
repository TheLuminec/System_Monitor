#!/usr/bin/env python3
"""avm - report tasks (training runs, services, anything long) to Avalon Monitor.

One command when you set something off:

    avm run "seed 3 training" [--log | --no-log] -- python train.py --seed 3
        Runs the command, streams its last lines and a heartbeat to the dashboard,
        reports the exit code; non-zero -> the task shows red. Exit code is passed through (killed by a signal:
        128+n, like a shell, with the signal named in the task's note). The output reaches your terminal byte for
        byte (progress bars, prompts without a newline). The command ALWAYS runs: when the hub is down, rejects
        the token or there is no config, avm says "untracked: <reason>" on stderr and runs it anyway.

    avm watch "nightly backup" --pid 4242 [--rc-file /path]
        Tracks an already-running process until it exits (detaches into the background). With --rc-file, a
        missing or unreadable file at the end means failed, "exit code unknown".

    avm task start "data prep" [--expect 300] [--note "..."]   -> prints TASK_ID
    avm task beat  TASK_ID [--progress 0.4] [--log "epoch 3 done"]
    avm task done  TASK_ID [--note "..."]
    avm task fail  TASK_ID [--rc 1] [--note "why"]
    avm task list  [--status running]

    avm results push PROJECT FILE
        Publishes a results document (JSON, "<name>-results/<n>" schema) to the project's
        Research page; every push is kept as history. Aggregates only - the hub renders what you send.

    avm agent track [--name "Project Coordinator"] [--note "..."]
        Run from inside a Claude Code session: registers that session on the dashboard's Agents page, where it
        stays listed (and can be restarted) even after it stops or the machine goes offline.
    avm agent untrack            (from inside the session)    avm agent list

Checks on this host (shown on its dashboard page; agents may manage their own):

    avm check list [--json]
    avm check add KIND TARGET [--label "R4 chain"] [--ttl 12h] [--notify self|new|none] [--cwd DIR] [--with-log]
                              [--cooldown 30m] [--daily-cap 6]
        KIND: process | file (path:max-age-s) | tcp (host:port) | mount | content | marker (glob[::regex])
              | nonempty | unit ([user:]name) | job ([user:]glob scope) | memory (free-GB floor, one per host)
        --notify self   when it goes ok -> failing, wake THIS Claude session (resumed, read-only) with an alert;
                        new = a fresh read-only session in --cwd. Guard-railed: cooldown, daily cap, one open alert
                        until it recovers, held while the session is mid-turn. Alerts carry paths/ids/exit codes,
                        never file contents; --with-log adds related avm task log tails (leave off for DUA data).
        --ttl 12h       remove the check by itself afterwards (e.g. when a chain will be finished).
    avm check set ID [same options] [--target T] [--no-log]       avm check rm ID | KIND TARGET

Resources and the wait queue (host-local; opt-in - plain `avm run` behaves exactly as before):

    avm run "nymeria b4020" --gpu --exclusive --mem 19G --wait [--max-wait 8h] -- python train.py
        --gpu / --exclusive  uses the GPU / is a heavy GPU job. --exclusive only excludes other --exclusive jobs
                             (one heavy job at a time); light --gpu jobs are NOT held back by it and may share the
                             GPU with it - give them --vram to be admitted on measured free VRAM instead
        --mem 19G, --vram 6G what the job expects to use (admission counts it while the job is starting up)
        --wait               queue until it fits: first come first served among jobs competing for the same thing,
                             admitted on MEASURED free memory/VRAM (never RSS) minus declared start-up reservations
        --max-wait 8h        give up loudly (task failed, exit 124) instead of waiting forever
      Tags without --wait just declare (shown on the dashboard, counted by queued jobs). A job that can never fit is
      rejected at submit (exit 2). Exit code and output of the command are passed through untouched. Nested
      `avm run --wait` inside a running queued job inherits its reservation instead of queueing behind it.
      The child gets AVM_TASK_ID, AVM_STARTED_AT and (in a git checkout) AVM_CODE_SHA, recorded at start, not queue time.
    avm queue status [--json]     avm queue drain [--wait]     avm queue resume
        drain: admit nothing new (running jobs finish; --wait returns once the host is idle) - e.g. before a git pull.
      Settings (agent.conf): AVM_QUEUE_HEADROOM_GB (default 2), AVM_QUEUE_SETTLE_SEC (default 180), AVM_QUEUE_DIR.
      The queue dir (default /run/avm-queue when the root installer made it, else /tmp/avm-queue) is shared by every
      user on the host. avm uses it only if it is a real directory (not a symlink) owned by root or you, not writable
      by others unless sticky, in a parent chain with the same properties; files in it are never followed through
      symlinks. If it fails those rules (e.g. another user created /tmp/avm-queue first), the command STILL runs: avm
      warns "queue disabled: <why>; running without admission control" and notes it on the task. The fix is
      /run/avm-queue (root install, or: d /run/avm-queue 1777 root root - in /etc/tmpfiles.d/avm-queue.conf).

Task logs and data-use agreements (local switches in agent.conf; the hub cannot set them):
    AVM_TASK_LOG=off              no output lines leave this host: avm run / watch / task send only name, status,
                                  rc, timing and avm's own status note (no log tail, --note text, queue reasons,
                                  command line or working directory)
    AVM_TASK_LOG_OFF_DIRS=/data/dua,/srv/cohort   the same for runs whose working directory is inside one of these
    Read from every agent.conf avm can open; "off" in any of them wins and the directory lists add up. Per run:
    `avm run --no-log` forces it off, `avm run --log` forces it on. A run with the log off passes AVM_TASK_LOG=off
    to its child, so nested avm calls stay quiet too (the environment can only turn it off, never on).

Needs the agent's config (server URL + host token). Looked up in order:
AVM_CONFIG, ~/.config/avalon-agent/agent.conf, /etc/avalon-agent/agent.conf,
%ProgramData%\\AvalonAgent\\agent.conf. Only one dependency: Python 3.8+.
"""
from __future__ import annotations

import argparse
import codecs
import getpass
import json
import os
import re
import shlex
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from typing import Any, Dict, List, Optional, Tuple

AVM_VERSION = "1.10.0"
IS_WINDOWS = sys.platform.startswith("win")


# ------------------------------------------------------------------ config
def _candidates() -> List[str]:
    c = [os.environ.get("AVM_CONFIG", "")]
    c.append(os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), "avalon-agent", "agent.conf"))
    c.append("/etc/avalon-agent/agent.conf")
    if IS_WINDOWS:
        c.append(os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"), "AvalonAgent", "agent.conf"))
    c.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "agent.conf"))
    return [x for x in c if x]


OFF_WORDS = ("off", "0", "no", "false")


def _read_conf(path: str) -> Optional[Dict[str, str]]:
    vals: Dict[str, str] = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    v = v.strip()
                    if v[:1] not in ('"', "'"):   # unquoted: an inline comment starts at " #" (same as the agent)
                        v = re.split(r"\s+#", v, maxsplit=1)[0]
                    vals.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except (OSError, ValueError):
        return None
    return vals


def load_config(required: bool = True) -> Dict[str, str]:
    cfg: Dict[str, str] = {}
    log_off, off_dirs = False, []   # the task-log switches: from EVERY readable config, most restrictive wins
    for path in _candidates():
        if not os.path.isfile(path):
            continue
        vals = _read_conf(path)
        if vals is None:
            continue
        log_off = log_off or vals.get("AVM_TASK_LOG", "").strip().lower() in OFF_WORDS
        off_dirs += [d.strip() for d in vals.get("AVM_TASK_LOG_OFF_DIRS", "").split(",") if d.strip()]
        if "_path" not in cfg:
            for k, v in vals.items():
                cfg.setdefault(k, v)
            if cfg.get("AVM_SERVER_URL") and cfg.get("AVM_TOKEN"):
                cfg["_path"] = path
    cfg["AVM_TASK_LOG"] = "off" if log_off else "on"
    cfg["AVM_TASK_LOG_OFF_DIRS"] = ",".join(off_dirs)
    for k in ("AVM_SERVER_URL", "AVM_TOKEN"):
        if os.environ.get(k):
            cfg[k] = os.environ[k]
    if required and (not cfg.get("AVM_SERVER_URL") or not cfg.get("AVM_TOKEN")):
        sys.stderr.write("avm: no agent config found (looked in %s); set AVM_SERVER_URL and AVM_TOKEN\n" % ", ".join(_candidates()))
        sys.exit(2)
    return cfg


def _inside(path: str, dirs: str) -> bool:
    """Is `path` one of the comma-separated `dirs` or below one of them (symlinks resolved)?"""
    p = os.path.normcase(os.path.realpath(path))
    for d in dirs.split(","):
        d = d.strip()
        if not d:
            continue
        d = os.path.normcase(os.path.realpath(os.path.expanduser(d)))
        try:
            if os.path.commonpath([p, d]) == d:
                return True
        except ValueError:   # different drives (Windows)
            continue
    return False


def task_log_on(cfg: Dict[str, str], force: Optional[bool] = None, cwd: Optional[str] = None) -> bool:
    """May output lines of this run go to the hub? --log/--no-log decide; otherwise AVM_TASK_LOG=off (agent.conf, or
    inherited from a parent run with the log off) or a cwd inside AVM_TASK_LOG_OFF_DIRS turns it off. Local only."""
    if force is not None:
        return force
    if os.environ.get("AVM_TASK_LOG", "").strip().lower() in OFF_WORDS:
        return False
    if cfg.get("AVM_TASK_LOG", "on").strip().lower() in OFF_WORDS:
        return False
    try:
        cwd = cwd or os.getcwd()
    except OSError:
        return False
    return not _inside(cwd, cfg.get("AVM_TASK_LOG_OFF_DIRS", ""))


def _say(msg: str) -> None:
    """stderr that never raises: a closed stderr (`2>&1 | head`) must not cost the exit code or the task's finish."""
    try:
        sys.stderr.write(msg)
        sys.stderr.flush()
    except (OSError, ValueError):
        pass


class Hub:
    def __init__(self, cfg: Dict[str, str]):
        self.cfg = cfg
        url = cfg.get("AVM_SERVER_URL") or ""
        self.base = url.rstrip("/") + "/api/v1" if url else ""
        self.token = cfg.get("AVM_TOKEN") or ""
        self.last_error = ""

    def call(self, path: str, body: Optional[dict] = None, method: str = "POST", timeout: float = 10.0, raw: Optional[bytes] = None) -> dict:
        if not self.base or not self.token:
            raise RuntimeError("no agent config found (AVM_SERVER_URL / AVM_TOKEN)")
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request(self.base + path, method=method, data=data,
                                     headers={"Content-Type": "application/json", "Authorization": "Bearer " + self.token,
                                              "User-Agent": "avm/" + AVM_VERSION})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8") or "{}")

    def try_call(self, path: str, body: Optional[dict] = None, method: str = "POST", quiet: bool = False) -> Optional[dict]:
        """Never raises: None (and the reason in last_error) when the hub is down, erroring or rejects the token."""
        try:
            r = self.call(path, body, method)
            return r if isinstance(r, dict) else {}
        except urllib.error.HTTPError as e:
            try:
                detail = e.read().decode(errors="replace")[:160]
            except Exception:
                detail = ""
            self.last_error = "hub rejected %s: HTTP %s %s" % (path, e.code, detail)
        except Exception as e:
            self.last_error = ("cannot reach hub for %s: %s" % (path, e)) if self.base and self.token else str(e)
        if not quiet:
            _say("avm: %s\n" % self.last_error)
        return None


def _task_body(name: str, source: str, expect: Optional[float], command: Optional[str], pid: Optional[int],
               note: Optional[str], log_on: bool = True, meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The task as the hub sees it. With the task log off, the command line, working directory and --note stay local too."""
    meta = dict(meta) if meta else {"hostname": socket.gethostname()}
    if note and log_on:   # I-01: a note is free text about the work, like its output
        meta["note"] = note
    cwd: Optional[str] = None
    if log_on:
        try:
            cwd = os.getcwd()
        except OSError:
            pass
    return {"name": name, "source": source, "expect_beat": expect, "command": command if log_on else None, "cwd": cwd,
            "pid": pid, "user": _user(), "meta": meta}


def _start(hub: Hub, name: str, source: str, expect: Optional[float], command: Optional[str], pid: Optional[int], note: Optional[str],
           log_on: bool = True) -> int:
    r = hub.call("/tasks", _task_body(name, source, expect, command, pid, note, log_on))
    return int(r["id"])


def _user() -> str:
    try:
        return getpass.getuser()
    except Exception:
        return "?"


def _fmt_dur(s: float) -> str:
    s = int(s)
    if s < 60:
        return "%ds" % s
    if s < 3600:
        return "%dm%02ds" % (s // 60, s % 60)
    return "%dh%02dm" % (s // 3600, (s % 3600) // 60)


# ------------------------------------------------------------------- queue
# Resource tags and an opt-in wait queue, local to this host. No daemon: every declared (--gpu/--mem/--vram) or
# waiting (--wait) job holds an OS lock on its own ticket for as long as its avm process lives, so a crash or a
# reboot releases its place and reservation by itself, and nothing starts again after a reboot. Admission never
# needs the hub (it only displays the queue); tickets carry job names and sizes only - no commands or paths.
QUEUE_POLL = 5.0
QUEUE_BEAT = 30.0
EXIT_GAVE_UP = 124     # --max-wait expired (like timeout(1))
MIN_BEAT = 5.0         # floor for --beat
WATCH_RC_GRACE = 5.0   # avm watch: how long to wait for the rc file after the process is gone (its wrapper writes it)

_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)   # a FIFO planted under a ticket's name must not hang every reader
_BINARY = getattr(os, "O_BINARY", 0)


class QueueUnsafe(Exception):
    """The queue directory (or a file in it) is in a state another local user could abuse: never put a ticket there.
    `avm run` then runs the command without admission control (F-07); `avm queue ...` reports the error."""


def _open_queue_file(path: str, create: bool = False) -> Any:
    """Open a file in the shared queue dir without following a symlink or blocking on a FIFO; regular files only.
    create=True: read-write, created 0644 if missing; falls back to read-only on someone else's file (enough to lock)."""
    flags = _NOFOLLOW | _NONBLOCK | _BINARY
    writable = create
    try:
        fd = os.open(path, (os.O_RDWR | os.O_CREAT if create else os.O_RDONLY) | flags, 0o644)
    except PermissionError:
        if not create:
            raise
        fd, writable = os.open(path, os.O_RDONLY | flags), False
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise QueueUnsafe("%s is not a regular file" % path)
        if writable and not IS_WINDOWS and st.st_uid == os.getuid():
            os.fchmod(fd, 0o644)   # readable by the other users of the queue whatever our umask
    except BaseException:
        os.close(fd)
        raise
    return os.fdopen(fd, "r+b" if writable else "rb")


def _write_atomic(directory: str, path: str, data: bytes) -> None:
    """Write `path` in the queue dir: a fresh mkstemp file (O_EXCL, never a planted one), then an atomic rename,
    which replaces a symlink at `path` instead of writing through it."""
    fd, tmp = tempfile.mkstemp(prefix="." + os.path.basename(path) + ".", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            if not IS_WINDOWS:
                os.fchmod(f.fileno(), 0o644)
        for attempt in range(20):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if not IS_WINDOWS or attempt == 19:   # POSIX: a file of another user in a sticky dir - refuse
                    raise
                time.sleep(0.05)   # Windows: someone is reading it right now
    finally:
        try:
            if os.path.lexists(tmp):
                os.remove(tmp)
        except OSError:
            pass


def _dir_problem(d: str, uid: Optional[int], final: bool) -> Optional[str]:
    """Why another local user could take over directory `d` (None = safe): a symlink (final component), not a dir,
    owned by someone other than root/us, or world-writable without the sticky bit. Group write is the owner's choice
    (with user private groups and umask 002 every directory is g+w) and is not refused."""
    try:
        st = os.lstat(d) if final else os.stat(d)
    except OSError as e:
        return "cannot stat %s: %s" % (d, e.strerror)
    if stat.S_ISLNK(st.st_mode):
        return "%s is a symlink" % d
    if not stat.S_ISDIR(st.st_mode):
        return "%s is not a directory" % d
    if uid is None:   # Windows: ACLs, not modes; the default lives under %ProgramData%
        return None
    if st.st_uid not in (0, uid):
        return "%s is owned by uid %d (not root or you)" % (d, st.st_uid)
    if st.st_mode & stat.S_IWOTH and not st.st_mode & stat.S_ISVTX:
        return "%s is writable by others and not sticky (mode %o)" % (d, stat.S_IMODE(st.st_mode))
    return None


def _default_queue_dir() -> str:
    if IS_WINDOWS:
        return os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"), "AvalonAgent", "queue")
    # /run/avm-queue (root-owned, 1777) only exists when the installer made it - no user can plant it there;
    # otherwise /tmp/avm-queue, accepted only after the ownership checks in Queue._dir
    return "/run/avm-queue" if os.path.isdir("/run/avm-queue") else "/tmp/avm-queue"


def _size_gb(v: Optional[str]) -> Optional[float]:
    if v in (None, ""):
        return None
    m = re.fullmatch(r"\s*([\d.]+)\s*([kmgt]?)i?b?\s*", str(v), re.I)
    if not m:
        raise ValueError("bad size %r (use e.g. 19G or 512M)" % v)
    return float(m.group(1)) * {"": 1.0, "G": 1.0, "T": 1024.0, "M": 1 / 1024, "K": 1 / 1048576}[m.group(2).upper()]


def _duration(v: Optional[str]) -> Optional[float]:
    if v in (None, ""):
        return None
    m = re.fullmatch(r"\s*([\d.]+)\s*([smhd]?)\s*", str(v), re.I)
    if not m:
        raise ValueError("bad duration %r (use e.g. 90m or 8h)" % v)
    return float(m.group(1)) * {"": 1, "S": 1, "M": 60, "H": 3600, "D": 86400}[m.group(2).upper()]


def _mem_gb() -> Tuple[Optional[float], Optional[float]]:
    """(available, total) RAM in GB: MemAvailable on Linux (never RSS), GlobalMemoryStatusEx on Windows."""
    if IS_WINDOWS:
        import ctypes

        class _MS(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong)] + \
                       [(n, ctypes.c_ulonglong) for n in ("tp", "ap", "tpf", "apf", "tv", "av", "aev")]
        ms = _MS()
        ms.dwLength = ctypes.sizeof(_MS)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(ms)):  # type: ignore[attr-defined]
            return ms.ap / 2 ** 30, ms.tp / 2 ** 30
        return None, None
    try:
        info: Dict[str, float] = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, _, v = line.partition(":")
                info[k] = int(v.split()[0]) / 2 ** 20   # kB -> GB
        return info.get("MemAvailable"), info.get("MemTotal")
    except (OSError, ValueError, IndexError):
        return None, None


def _vram_gb() -> Tuple[Optional[float], Optional[float]]:
    """(free, total) VRAM of GPU 0 in GB from nvidia-smi; (None, None) without an NVIDIA GPU."""
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=memory.free,memory.total", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=10).stdout.strip().splitlines()
        free, total = (float(x) / 1024 for x in out[0].split(","))
        return free, total
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return None, None


def _try_lock(f, blocking: bool = False) -> bool:
    """Exclusive OS lock on an open file; released by the OS when the process dies."""
    try:
        if IS_WINDOWS:
            import msvcrt
            f.seek(0)
            while True:
                try:
                    msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                    return True
                except OSError:
                    if not blocking:
                        return False
                    time.sleep(0.1)
        import fcntl
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        return True
    except OSError:
        return False


def _unlock(f) -> None:
    try:
        if IS_WINDOWS:
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


def _competes(a: dict, b: dict) -> bool:
    """Two jobs contend when both are exclusive GPU jobs, or both declared RAM, or both declared VRAM."""
    return (a.get("gpu") == "exclusive" and b.get("gpu") == "exclusive") or bool(a.get("mem_gb") and b.get("mem_gb")) \
        or bool(a.get("vram_gb") and b.get("vram_gb"))


def _res_text(t: dict) -> str:
    parts = [("GPU exclusive" if t.get("gpu") == "exclusive" else "GPU") if t.get("gpu") else "",
             "%gG RAM" % t["mem_gb"] if t.get("mem_gb") else "", "%gG VRAM" % t["vram_gb"] if t.get("vram_gb") else ""]
    return ", ".join(x for x in parts if x) or "-"


def _headroom(cfg: Dict[str, str]) -> float:
    return float(cfg.get("AVM_QUEUE_HEADROOM_GB") or 2)


def _impossible(res: Dict[str, Any], headroom: float) -> Optional[str]:
    """Reject at submit what could never fit, instead of queueing it forever. Needs no queue dir."""
    _, total = _mem_gb()
    if res.get("mem_gb") and total and res["mem_gb"] + headroom > total:
        return "asks for %g GB RAM but this host has %.1f GB (%.1f GB after %g GB headroom)" % (
            res["mem_gb"], total, total - headroom, headroom)
    if res.get("vram_gb"):
        _, vtotal = _vram_gb()
        if vtotal is None:
            return "asks for VRAM but no NVIDIA GPU was found (nvidia-smi)"
        if res["vram_gb"] > vtotal:
            return "asks for %g GB VRAM but the GPU has %.1f GB" % (res["vram_gb"], vtotal)
    return None


class Queue:
    def __init__(self, cfg: Dict[str, str]):
        self.dir = self._dir(cfg)
        self.headroom = _headroom(cfg)
        self.settle = float(cfg.get("AVM_QUEUE_SETTLE_SEC") or 180)
        self.me: Optional[Dict[str, Any]] = None
        self._lockf = None
        self._lock_id = ""

    @staticmethod
    def _dir(cfg: Dict[str, str]) -> str:
        """The shared queue dir, created if missing. Tickets must be visible to every user on the host (root's backup
        and a user's training share the GPU), so it can't be per-user; instead it must be one nobody else controls:
        a real directory owned by root or us, sticky if others may write, in a parent chain with the same properties.
        Anything else raises QueueUnsafe with the reason - no silent fallback to a private dir that would split the
        queue (avm run then runs the job without admission, loudly)."""
        d = os.path.abspath(os.environ.get("AVM_QUEUE_DIR") or cfg.get("AVM_QUEUE_DIR") or _default_queue_dir())
        uid = None if IS_WINDOWS else os.getuid()
        fix = ("remove it (or ask its owner / root to), or set AVM_QUEUE_DIR in agent.conf; lasting fix: a root-owned "
               "/run/avm-queue (root install-linux.sh, or 'd /run/avm-queue 1777 root root -' in /etc/tmpfiles.d)")
        parent = os.path.dirname(d)
        if not IS_WINDOWS:
            chain = os.path.realpath(parent)
            while True:   # a parent another user can write to (non-sticky) could swap the queue dir under us
                why = _dir_problem(chain, uid, final=False)
                if why:
                    raise QueueUnsafe("refusing queue dir %s: %s" % (d, why))
                if os.path.dirname(chain) == chain:
                    break
                chain = os.path.dirname(chain)
        try:
            if IS_WINDOWS:
                os.makedirs(d, exist_ok=True)
            else:
                os.mkdir(d, 0o700)   # private until we know it is ours, then opened up below
        except FileExistsError:
            pass
        except OSError as e:
            raise QueueUnsafe("cannot create queue dir %s: %s" % (d, e.strerror))
        why = _dir_problem(d, uid, final=True)
        if why:
            raise QueueUnsafe("refusing queue dir: %s; %s" % (why, fix))
        if uid is not None and os.lstat(d).st_uid == uid and stat.S_IMODE(os.lstat(d).st_mode) != 0o1777:
            try:
                os.chmod(d, 0o1777)   # shared by every user on the host; sticky: only owners remove their tickets
            except OSError:
                pass
        if not os.access(d, os.W_OK | os.X_OK):
            raise QueueUnsafe("queue dir %s is not writable by you (it should be mode 1777); %s" % (d, fix))
        return d

    def _path(self, tid: str, ext: str) -> str:
        return os.path.join(self.dir, tid + ext)

    def _alive(self, tid: str) -> bool:
        try:
            f = _open_queue_file(self._path(tid, ".lock"))
        except (OSError, QueueUnsafe):
            return False
        try:
            if _try_lock(f):   # we got it: its owner is gone
                _unlock(f)
                return False
            return True
        finally:
            f.close()

    def _forget(self, tid: str) -> None:
        for ext in (".json", ".lock"):
            try:
                os.remove(self._path(tid, ext))
            except OSError:
                pass   # another user's leftovers (sticky dir): ignored, they are dead anyway

    def tickets(self) -> List[Dict[str, Any]]:
        """Live tickets in queue order; dead ones (owner crashed / rebooted) are dropped."""
        out = []
        try:
            names = os.listdir(self.dir)
        except OSError:
            return []
        for fn in names:
            if not fn.endswith(".json"):
                continue
            tid = fn[:-5]
            if self.me and tid == self.me["id"]:
                out.append(self.me)
                continue
            if not self._alive(tid):
                self._forget(tid)
                continue
            try:
                with _open_queue_file(self._path(tid, ".json")) as f:
                    t = json.loads(f.read().decode("utf-8"))
                if isinstance(t, dict):
                    out.append(t)
            except (OSError, ValueError, QueueUnsafe):
                continue   # being rewritten right now (or not a ticket at all)
        return sorted(out, key=lambda t: str(t.get("seq", "")))

    def register(self, info: Dict[str, Any]) -> None:
        for attempt in range(5):   # lock first, then publish: a visible ticket is always locked
            tid = "%019d-%d" % (int(time.time() * 1e6), os.getpid())
            try:   # O_EXCL: never adopt (or write through) a file someone planted under our name
                fd = os.open(self._path(tid, ".lock"), os.O_RDWR | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _BINARY, 0o644)
                break
            except FileExistsError:
                time.sleep(0.002)
        else:
            raise QueueUnsafe("cannot create a queue ticket in %s: the names are taken" % self.dir)
        self._lock_id = tid   # release() removes it even if registering fails below
        if not IS_WINDOWS:
            os.fchmod(fd, 0o644)
        self._lockf = os.fdopen(fd, "r+b")
        self._lockf.write(b"x")
        self._lockf.flush()
        if not _try_lock(self._lockf):
            raise OSError("cannot lock queue ticket in %s" % self.dir)
        self.me = dict(info, id=tid, seq=tid, pid=os.getpid(), user=_user(), queued_at=time.time())
        self.save()

    def save(self) -> None:
        if not self.me:
            return
        _write_atomic(self.dir, self._path(self.me["id"], ".json"), json.dumps(self.me).encode("utf-8"))

    def release(self) -> None:
        if self.me:
            try:
                os.remove(self._path(self.me["id"], ".json"))
            except OSError:
                pass
        if self._lockf:
            _unlock(self._lockf)
            self._lockf.close()
            try:
                os.remove(self._path(self._lock_id, ".lock"))
            except OSError:
                pass
        self.me, self._lockf = None, None

    def draining(self) -> Optional[str]:
        try:
            with _open_queue_file(os.path.join(self.dir, "DRAIN")) as f:
                return f.read(500).decode("utf-8", "replace").strip() or "draining"
        except (OSError, QueueUnsafe):
            return None

    def impossible(self, res: Dict[str, Any]) -> Optional[str]:
        return _impossible(res, self.headroom)

    def check(self) -> Tuple[bool, str, int]:
        """(fits now, why not, place in line). Call with the admit lock held."""
        me, now = self.me or {}, time.time()
        drain = self.draining()
        ts = self.tickets()
        running = [t for t in ts if t.get("state") == "running"]
        ahead = [t for t in ts if t.get("state") == "waiting" and t.get("seq", "") < me.get("seq", "") and _competes(t, me)]
        if drain:
            return False, "host is draining (%s); `avm queue resume` reopens it" % drain, len(ahead) + 1
        if ahead:
            return False, "behind '%s'%s" % (ahead[0].get("name"), " and %d more" % (len(ahead) - 1) if len(ahead) > 1 else ""), len(ahead) + 1
        if me.get("gpu") == "exclusive":   # one heavy job at a time; shared --gpu jobs are never held back by it
            holder = next((t for t in running if t.get("gpu") == "exclusive"), None)
            if holder:
                return False, "one --exclusive GPU job at a time: '%s' is running" % holder.get("name"), 1
        settling = [t for t in running if now - (t.get("started_at") or 0) < self.settle]
        if me.get("mem_gb"):
            avail, _ = _mem_gb()
            if avail is not None:
                held = sum(t.get("mem_gb") or 0 for t in settling)
                free = avail - held - self.headroom
                if free < me["mem_gb"]:
                    return False, "needs %g GB RAM; %.1f GB available%s, %g GB headroom" % (
                        me["mem_gb"], avail, " minus %g GB held by starting jobs" % held if held else "", self.headroom), 1
        if me.get("vram_gb"):
            vfree, _ = _vram_gb()
            if vfree is not None:
                held = sum(t.get("vram_gb") or 0 for t in settling)
                if vfree - held - 0.5 < me["vram_gb"]:
                    return False, "needs %g GB VRAM; %.1f GB free%s" % (
                        me["vram_gb"], vfree, " minus %g GB held by starting jobs" % held if held else ""), 1
        return True, "", 0

    def try_admit(self) -> Tuple[bool, str, int]:
        """Check and, if it fits, mark this ticket running - atomically, so two jobs can't both take the same free memory."""
        try:
            lockf = _open_queue_file(os.path.join(self.dir, "admit.lock"), create=True)
        except OSError as e:   # e.g. ELOOP: someone planted a symlink there
            raise QueueUnsafe("cannot use %s: %s" % (os.path.join(self.dir, "admit.lock"), e))
        try:
            _try_lock(lockf, blocking=True)
            ok, why, pos = self.check()
            if ok and self.me is not None:
                self.me.update(state="running", started_at=time.time(), reason=None)
            elif self.me is not None:
                self.me["reason"] = why
            self.save()
            return ok, why, pos
        finally:
            _unlock(lockf)
            lockf.close()


def _code_identity(cwd: str) -> Optional[Dict[str, Any]]:
    """git HEAD (+ dirty flag) of the job's directory, taken when it actually starts (not when it was queued)."""
    try:
        r = subprocess.run(["git", "-C", cwd, "rev-parse", "--short=12", "HEAD"], capture_output=True, text=True, timeout=10)
        if r.returncode != 0:
            return None
        d = subprocess.run(["git", "-C", cwd, "status", "--porcelain", "--untracked-files=no"], capture_output=True, text=True, timeout=20)
        return {"sha": r.stdout.strip(), "dirty": bool(d.stdout.strip()), "at": time.time()}
    except (OSError, subprocess.SubprocessError):
        return None


def _resources(args: argparse.Namespace) -> Dict[str, Any]:
    res: Dict[str, Any] = {}
    if args.gpu or args.exclusive:
        res["gpu"] = "exclusive" if args.exclusive else "shared"
    if args.mem:
        res["mem_gb"] = round(_size_gb(args.mem) or 0, 2)
    if args.vram:
        res["vram_gb"] = round(_size_gb(args.vram) or 0, 2)
    return res


# --------------------------------------------------------------------- run
def cmd_run(args: argparse.Namespace, hub: Hub) -> int:
    command = args.command
    if not command:
        sys.stderr.write("avm run: give the command after `--`, e.g. avm run \"name\" -- python train.py\n")
        return 2
    if len(command) == 1 and (" " in command[0]):
        command = command[0] if IS_WINDOWS else shlex.split(command[0])
    shown = command if isinstance(command, str) else " ".join(shlex.quote(c) for c in command)
    try:
        res = _resources(args)
        max_wait = _duration(args.max_wait)
    except ValueError as e:
        sys.stderr.write("avm run: %s\n" % e)
        return 2
    args.task_log = task_log_on(hub.cfg, getattr(args, "log_force", None))
    if res or args.wait:
        return _run_queued(args, hub, command, shown, res, max_wait)
    beat_every = max(MIN_BEAT, float(args.beat))
    expect = beat_every * 2   # the hub marks it stalled after 1.5 x expect = 3 x beat without a beat
    # the hub never stands between the user and the command: down, erroring or a rotated token -> run it untracked
    tid = _try_start(hub, _task_body(args.name, "run", expect, shown, None, args.note, args.task_log))
    if tid is None:
        _say("avm: untracked: %s - running the command anyway\n" % hub.last_error)
    else:
        _say("avm: task #%d '%s' started (%s)%s\n" % (tid, args.name, shown[:80], "" if args.task_log else " [task log off: no output sent]"))
    return _execute(hub, args, command, tid, beat_every, None if args.task_log else dict(os.environ, AVM_TASK_LOG="off"))


_LINE_BREAK = re.compile(r"(\r\n|\r|\n)")


class _Tail:
    """Whole lines of the child's output, for the hub only (the terminal gets the raw bytes). A lone \\r - a progress
    bar redrawing itself - overwrites the current line instead of starting a new one, so tqdm makes one line, not
    thousands; the line still being drawn is available as partial() for the heartbeat."""
    MAX = 2000

    def __init__(self) -> None:
        self._dec = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._held = ""   # a trailing \r: CRLF or a redraw, the next chunk tells
        self.cur = ""     # the line being written
        self.shown = ""   # what the last redraw left on screen

    def feed(self, data: bytes, final: bool = False) -> List[str]:
        text = self._held + self._dec.decode(data, final)
        self._held = ""
        if text.endswith("\r") and not final:
            text, self._held = text[:-1], "\r"
        out: List[str] = []
        for part in _LINE_BREAK.split(text):
            if part in ("\n", "\r\n"):
                out.append(self.cur or self.shown)
                self.cur = self.shown = ""
            elif part == "\r":
                if self.cur:
                    self.shown = self.cur
                self.cur = ""
            elif part:
                self.cur = (self.cur + part)[-self.MAX:]
        if final and (self.cur or self.shown):
            out.append(self.cur or self.shown)
            self.cur = self.shown = ""
        return out

    def partial(self) -> str:
        return self.cur or self.shown


class _Passthrough:
    """Our stdout, written byte for byte as the child produces it (no newline needed: prompts show at once). When the
    reader goes away (`| head`, a closed terminal) we stop writing but keep draining the child, so it never blocks on a
    full pipe and its exit code is still reported."""

    def __init__(self) -> None:
        self.stream = sys.stdout
        self.broken = False
        try:
            self.stream.flush()   # anything printed before goes first
        except (OSError, ValueError):
            pass

    def write(self, data: bytes) -> None:
        if self.broken:
            return
        try:
            buf = getattr(self.stream, "buffer", None)
            if buf is not None:
                buf.write(data)
                buf.flush()
            else:
                self.stream.write(data.decode("utf-8", "replace"))
                self.stream.flush()
        except (OSError, ValueError):   # BrokenPipeError, EIO on a hung-up tty, a closed stream
            self.broken = True
            try:   # no "Exception ignored ... BrokenPipeError" when Python flushes stdout at exit
                devnull = os.open(os.devnull, os.O_WRONLY)
                os.dup2(devnull, self.stream.fileno())
                os.close(devnull)
            except (OSError, ValueError, AttributeError):
                pass


def _signame(n: int) -> str:
    try:
        return signal.Signals(n).name
    except ValueError:
        return "signal %d" % n


def _execute(hub: Hub, args: argparse.Namespace, command: Any, tid: Optional[int], beat_every: float,
             env: Optional[Dict[str, str]] = None) -> int:
    """Run the command, pass its output through byte for byte, beat the task, report the exit code (passed through;
    killed by signal n -> 128+n). Neither the hub nor a closed stdout can stop the command or leave the task hanging.
    With the task log off (args.task_log False) no output line is ever sent - only heartbeats, status, rc and note."""
    log_on = getattr(args, "task_log", True)
    failing: List[bool] = []

    def call(path: str, body: Dict[str, Any]) -> None:
        if tid is None:
            return
        if not log_on:
            body.pop("log", None)
        if hub.try_call(path % tid, body, quiet=bool(failing)) is None:
            failing.append(True)   # say it once, not on every beat
        else:
            failing[:] = []

    tail = _Tail()
    pending: deque = deque(maxlen=40)   # complete lines not yet sent
    lock = threading.Lock()
    t0 = time.time()
    try:
        proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, shell=isinstance(command, str),
                                bufsize=0, env=env)
    except OSError as e:
        call("/tasks/%d/finish", {"status": "failed", "rc": 127,
                                  "note": "cannot start: %s" % (e if log_on else (e.strerror or e.__class__.__name__))})
        _say("avm: cannot start command: %s\n" % e)
        return 127
    call("/tasks/%d/beat", {"pid": proc.pid})

    stop = threading.Event()

    def beater() -> None:
        sent_partial = ""
        while not stop.wait(beat_every):
            with lock:
                lines = list(pending)
                pending.clear()
                part = tail.partial()
            if part and part != sent_partial:   # a progress bar mid-redraw: its latest state, once per beat
                lines.append(part)
                sent_partial = part
            call("/tasks/%d/beat", {"log": lines})

    th = threading.Thread(target=beater, daemon=True)
    th.start()

    def forward(signum, frame):  # pass Ctrl-C / SIGTERM / SIGHUP to the child; we report what happens
        try:
            proc.send_signal(signum)
        except Exception:
            pass

    previous = {}
    for name in ("SIGINT", "SIGTERM", "SIGHUP"):   # SIGHUP: an ssh drop finishes the task instead of stalling it
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            previous[sig] = signal.signal(sig, forward)
        except (ValueError, OSError):
            pass
    out = _Passthrough()
    try:
        assert proc.stdout is not None
        fd = proc.stdout.fileno()
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            out.write(chunk)
            with lock:
                pending.extend(tail.feed(chunk))
        rc = proc.wait()
    finally:
        for sig, handler in previous.items():
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError, TypeError):
                pass
    stop.set()
    with lock:
        pending.extend(tail.feed(b"", final=True))
        lines = list(pending)
        pending.clear()
    killed = -rc if (rc < 0 and not IS_WINDOWS) else 0
    if killed:
        rc = 128 + killed   # like a shell: 143 for SIGTERM, 129 for SIGHUP
    status = "done" if rc == 0 else "failed"
    took = _fmt_dur(time.time() - t0)
    note = ("killed by %s after %s" % (_signame(killed), took)) if killed else "%s after %s" % (status, took)
    if getattr(args, "queue_note", None):   # F-07: ran without admission control
        note += "; " + args.queue_note
    call("/tasks/%d/finish", {"status": status, "rc": rc, "log": lines, "note": note})
    _say("avm: task %s %s (rc=%s, %s)\n" % ("#%d" % tid if tid is not None else "(untracked)", status, rc,
                                            note if killed else took))
    return rc


def _try_start(hub: Hub, body: Dict[str, Any]) -> Optional[int]:
    """Create the dashboard task, but never let the hub block a job: None (reason in hub.last_error) when it can't."""
    r = hub.try_call("/tasks", body, quiet=True)
    try:
        return int(r["id"])   # type: ignore[index]
    except (TypeError, KeyError, ValueError):
        if r is not None:
            hub.last_error = "the hub answered without a task id"
        return None


def _run_without_queue(args: argparse.Namespace, hub: Hub, command: Any, res: Dict[str, Any], body: Dict[str, Any],
                       meta: Dict[str, Any], env: Dict[str, str], beat_every: float, why: str, tid: Optional[int]) -> int:
    """F-07: the shared queue dir can't be used safely (another user created /tmp/avm-queue first, a symlink, a file
    planted in it...). No ticket goes there, but that must not cost anyone their job: like an untracked run when the
    hub is down (E-08), the command runs now - without admission control - and says so on stderr and on the task.
    `tid`: the task already created while queued, if any."""
    why = " ".join(why.split())
    _say("avm: WARNING: queue disabled: %s; running without admission control\n" % why)
    note = "queue disabled (%s): ran without admission control" % why[:300]
    args.queue_note = note   # appended to the finish note by _execute
    meta["queue"] = {"disabled": why[:300]}
    meta["note"] = "%s; %s" % (args.note, note) if args.note and getattr(args, "task_log", True) else note
    code = _code_identity(os.getcwd())
    if code:
        meta["code"] = code
        env["AVM_CODE_SHA"] = code["sha"] + ("-dirty" if code["dirty"] else "")
    env["AVM_STARTED_AT"] = "%d" % time.time()
    if tid is None:
        tid = _try_start(hub, dict(body, expect_beat=beat_every * 2, meta=meta))
    else:
        hub.try_call("/tasks/%d/beat" % tid, {"status": "running", "note": note,
                                              "meta": {k: meta[k] for k in ("queue", "code", "note") if k in meta}})
    if tid is not None:
        env["AVM_TASK_ID"] = str(tid)
        _say("avm: task #%d '%s' started [%s] without the queue\n" % (tid, args.name, _res_text(res)))
    else:
        _say("avm: untracked: %s - running '%s' [%s] anyway\n" % (hub.last_error, args.name, _res_text(res)))
    return _execute(hub, args, command, tid, beat_every, env)


def _queued(since: float, why: str, pos: Any, max_wait: Optional[float], log_on: bool) -> Dict[str, Any]:
    """meta.queue of a waiting task. The reason names what it waits behind (other jobs): with the task log off it stays
    here (I-01), like the note."""
    q = {"since": since, "position": pos, "max_wait": max_wait}
    if log_on:
        q["reason"] = why
    return q


def _run_queued(args: argparse.Namespace, hub: Hub, command: Any, shown: str, res: Dict[str, Any], max_wait: Optional[float]) -> int:
    """avm run with resource tags and/or --wait: declare, queue until it fits, then run exactly like a plain avm run.
    A queue dir that can't be used safely never stops the command: it runs without admission control (F-07)."""
    beat_every = max(MIN_BEAT, float(args.beat))
    log_on = getattr(args, "task_log", True)
    meta: Dict[str, Any] = {"hostname": socket.gethostname(), "resources": res}
    if args.note and log_on:   # I-01: with the task log off, --note and the queue's reasons stay here
        meta["note"] = args.note
    body = _task_body(args.name, "run", None, shown, None, None, log_on, meta)
    body["meta"] = meta   # the same dict: queue position and code identity are added to it below
    env = dict(os.environ)
    if not log_on:
        env["AVM_TASK_LOG"] = "off"   # nested avm calls in the job stay quiet too
    try:
        q = Queue(hub.cfg)
    except QueueUnsafe as e:
        if args.wait:
            why = _impossible(res, _headroom(hub.cfg))   # a property of the host, not of the queue dir
            if why:
                sys.stderr.write("avm: '%s' can never start on this host: %s\n" % (args.name, why))
                return 2
        return _run_without_queue(args, hub, command, res, body, meta, env, beat_every, str(e), None)

    parent = os.environ.get("AVM_QUEUE_HOLD")
    if parent and q._alive(parent):   # nested inside a queued job: use its reservation, never wait behind it
        sys.stderr.write("avm: inside a queued job's reservation (%s): starting at once\n" % parent)
        meta["queue"] = {"inherited": parent}
        tid = _try_start(hub, dict(body, expect_beat=beat_every * 2))
        if tid is not None:
            env["AVM_TASK_ID"] = str(tid)
        else:
            _say("avm: untracked: %s - running the command anyway\n" % hub.last_error)
        return _execute(hub, args, command, tid, beat_every, env)

    if args.wait:
        why = q.impossible(res)
        if why:
            sys.stderr.write("avm: '%s' can never start on this host: %s\n" % (args.name, why))
            return 2
    tid: Optional[int] = None
    try:
        try:   # F-07: a file planted in the queue dir (or a dir that turned unsafe) -> run without admission
            q.register({"name": args.name, "state": "waiting" if args.wait else "running", **res,
                        **({} if args.wait else {"started_at": time.time()})})
            if args.wait:
                ok, why, pos = q.try_admit()
                if not ok:
                    since = q.me["queued_at"] if q.me else time.time()
                    meta["queue"] = _queued(since, why, pos, max_wait, log_on)
                    tid = _try_start(hub, dict(body, status="queued", expect_beat=max(QUEUE_BEAT, beat_every) * 2))
                    sys.stderr.write("avm: '%s' queued (%s)%s\n" % (args.name, why, "; task #%d" % tid if tid is not None else ""))

                    def interrupted(signum, frame):
                        raise KeyboardInterrupt
                    previous = {}
                    for name in ("SIGTERM", "SIGHUP"):   # killed or hung up while waiting: finish the task as cancelled
                        sig = getattr(signal, name, None)
                        if sig is None:
                            continue
                        try:
                            previous[sig] = signal.signal(sig, interrupted)
                        except (ValueError, OSError):
                            pass
                    last_beat, last_why = time.time(), why
                    try:
                        while True:
                            time.sleep(QUEUE_POLL)
                            ok, why, pos = q.try_admit()
                            if ok:
                                break
                            waited = time.time() - since
                            if max_wait is not None and waited >= max_wait:
                                note = "gave up after waiting %s (--max-wait)" % _fmt_dur(waited)
                                if tid is not None:
                                    hub.try_call("/tasks/%d/finish" % tid, {"status": "failed", "rc": EXIT_GAVE_UP,
                                                                            "note": note + (": " + why if log_on else "")})
                                note += ": " + why
                                sys.stderr.write("avm: '%s' %s\n" % (args.name, note))
                                return EXIT_GAVE_UP
                            if why != last_why:
                                sys.stderr.write("avm: '%s' still queued: %s\n" % (args.name, why))
                                last_why = why
                            if time.time() - last_beat >= QUEUE_BEAT:
                                last_beat = time.time()
                                meta["queue"] = _queued(since, why, pos, max_wait, log_on)
                                if tid is None:   # the hub was unreachable when we queued: try again
                                    tid = _try_start(hub, dict(body, status="queued", expect_beat=max(QUEUE_BEAT, beat_every) * 2))
                                else:
                                    hub.try_call("/tasks/%d/beat" % tid, {"status": "queued", "meta": {"queue": meta["queue"]}})
                    except KeyboardInterrupt:
                        if tid is not None:
                            hub.try_call("/tasks/%d/finish" % tid, {"status": "failed", "rc": 130, "note": "cancelled while queued"})
                        sys.stderr.write("avm: '%s' cancelled while queued\n" % args.name)
                        return 130
                    finally:
                        for sig, handler in previous.items():
                            try:
                                signal.signal(sig, handler)
                            except (ValueError, OSError, TypeError):
                                pass
                    meta["queue"] = {"since": since, "waited": time.time() - since}
                    sys.stderr.write("avm: '%s' admitted after %s\n" % (args.name, _fmt_dur(time.time() - since)))
                else:
                    meta["queue"] = {"since": q.me["queued_at"] if q.me else time.time(), "waited": 0}
        except (QueueUnsafe, OSError) as e:
            q.release()
            return _run_without_queue(args, hub, command, res, body, meta, env, beat_every, str(e), tid)
        code = _code_identity(os.getcwd())
        if code:
            meta["code"] = code
            env["AVM_CODE_SHA"] = code["sha"] + ("-dirty" if code["dirty"] else "")
        env["AVM_STARTED_AT"] = "%d" % time.time()
        if q.me:
            env["AVM_QUEUE_HOLD"] = q.me["id"]
        if tid is None:
            tid = _try_start(hub, dict(body, expect_beat=beat_every * 2, meta=meta))
        else:
            hub.try_call("/tasks/%d/beat" % tid, {"status": "running", "meta": {k: meta[k] for k in ("queue", "code") if k in meta}})
        if tid is not None:
            env["AVM_TASK_ID"] = str(tid)
            _say("avm: task #%d '%s' started [%s] (%s)\n" % (tid, args.name, _res_text(res), shown[:80]))
        else:
            _say("avm: untracked: %s - running '%s' [%s] anyway\n" % (hub.last_error, args.name, _res_text(res)))
        return _execute(hub, args, command, tid, beat_every, env)
    finally:
        q.release()


# ------------------------------------------------------------------- check
def _check_body(args: argparse.Namespace) -> Dict[str, Any]:
    body: Dict[str, Any] = {}
    if getattr(args, "label", None) is not None:
        body["label"] = args.label
    if getattr(args, "ttl", None) is not None:
        body["ttl_sec"] = _duration(args.ttl)
    if getattr(args, "notify", None) is not None:
        body["notify"] = args.notify
        if args.notify == "self":
            me = _this_session()
            if not me.get("key"):
                raise ValueError("--notify self only works inside a Claude Code session")
            body["session"] = {k: v for k, v in me.items() if v is not None}
    if getattr(args, "cwd", None) is not None:
        body["notify_cwd"] = os.path.abspath(os.path.expanduser(args.cwd))
    if getattr(args, "with_log", False):
        body["notify_log"] = True
    if getattr(args, "no_log", False):
        body["notify_log"] = False
    if getattr(args, "cooldown", None) is not None:
        body["cooldown_min"] = (_duration(args.cooldown) or 0) / 60
    if getattr(args, "daily_cap", None) is not None:
        body["daily_cap"] = args.daily_cap
    return body


def _check_line(c: Dict[str, Any]) -> str:
    state = c.get("state") or "?"
    mark = {"ok": "✓", "failing": "✗", "unknown": "?"}.get(state, " ")
    resp = c.get("notify") or "-"
    if c.get("open_job"):
        resp += " (alert #%s open)" % c["open_job"]
    elif c.get("pending"):
        resp += " (pending)"
    exp = (" · expires in %s" % _fmt_dur(c["expires"] - time.time())) if c.get("expires") else ""
    detail = ((c.get("result") or {}).get("detail") or c.get("detail") or "")[:60]
    return "%s %4d  %-9s %-36s %-18s %-22s %s%s" % (mark, c["id"], c["kind"], c["target"][:36], (c.get("label") or "")[:18],
                                                  resp[:22], detail, exp)


def cmd_check(args: argparse.Namespace, hub: Hub) -> int:
    try:
        if args.op == "list":
            rows = hub.call("/agent/checks", method="GET").get("checks", [])
            if args.json:
                print(json.dumps(rows, indent=1))
                return 0
            if not rows:
                print("no checks on this host (avm check add KIND TARGET)")
            for c in rows:
                print(_check_line(c))
            return 0
        if args.op == "add":
            body = dict(_check_body(args), kind=args.kind, target=os.path.expanduser(args.target) if args.kind in ("file", "mount", "content", "nonempty") else args.target)
            c = hub.call("/agent/checks", body)["check"]
            print("check #%d: %s %s%s" % (c["id"], c["kind"], c["target"], " -> notifies %s" % c["notify"] if c.get("notify") else ""))
            return 0
        if args.op == "set":
            body = _check_body(args)
            if args.target is not None:
                body["target"] = args.target
            if not body:
                sys.stderr.write("avm check set: nothing to change\n")
                return 2
            c = hub.call("/agent/checks/%d" % args.id, body, method="PATCH")["check"]
            print(_check_line(c))
            return 0
        if args.op == "rm":
            cid = None
            if len(args.what) == 1 and args.what[0].isdigit():
                cid = int(args.what[0])
            elif len(args.what) == 2:
                kind, target = args.what
                for c in hub.call("/agent/checks", method="GET").get("checks", []):
                    if c["kind"] == kind and c["target"] in (target, os.path.expanduser(target)):
                        cid = c["id"]
            if cid is None:
                sys.stderr.write("avm check rm: no such check (give its ID, or KIND TARGET)\n")
                return 1
            hub.call("/agent/checks/%d" % cid, method="DELETE")
            print("removed check #%d" % cid)
            return 0
    except ValueError as e:
        sys.stderr.write("avm check: %s\n" % e)
        return 2
    return 2


def cmd_queue(args: argparse.Namespace, cfg: Dict[str, str]) -> int:
    try:
        q = Queue(cfg)
    except QueueUnsafe as e:
        sys.stderr.write("avm: %s\n" % e)
        return 2
    flag = os.path.join(q.dir, "DRAIN")
    if args.op == "drain":
        if not q.draining():
            try:   # never through a planted DRAIN symlink
                _write_atomic(q.dir, flag, ("drained by %s at %s" % (_user(), time.strftime("%Y-%m-%d %H:%M"))).encode("utf-8"))
            except OSError as e:
                sys.stderr.write("avm: cannot drain: %s\n" % e)
                return 1
        print("draining: queued jobs will not start; running ones finish (`avm queue resume` to reopen)")
        if args.wait:
            while True:
                running = [t for t in q.tickets() if t.get("state") == "running"]
                if not running:
                    print("idle: no queued or declared job is running")
                    return 0
                time.sleep(QUEUE_POLL)
        return 0
    if args.op == "resume":
        try:
            os.remove(flag)
            print("resumed: queued jobs may start again")
        except FileNotFoundError:
            print("not draining")
        except OSError as e:   # sticky dir: only its owner (or root) can remove another user's DRAIN
            sys.stderr.write("avm: cannot resume: %s (%s; ask whoever drained it, or root)\n" % (e.strerror, q.draining() or "DRAIN"))
            return 1
        return 0
    ts = q.tickets()
    avail, total = _mem_gb()
    vfree, vtotal = _vram_gb()
    if args.json:
        print(json.dumps({"draining": q.draining(), "tickets": ts, "mem_available_gb": avail, "mem_total_gb": total,
                          "vram_free_gb": vfree, "vram_total_gb": vtotal, "headroom_gb": q.headroom, "dir": q.dir}, indent=1))
        return 0
    now = time.time()
    print("RAM %s free of %s GB  ·  VRAM %s  ·  headroom %g GB%s" % (
        "%.1f" % avail if avail is not None else "?", "%.0f" % total if total else "?",
        "%.1f of %.0f GB free" % (vfree, vtotal) if vfree is not None else "n/a", q.headroom,
        "  ·  DRAINING (%s)" % q.draining() if q.draining() else ""))
    if not ts:
        print("queue empty")
    for t in ts:
        running = t.get("state") == "running"
        since = t.get("started_at") if running else t.get("queued_at")
        print("%s %-8s %-30s %-10s %-26s %s%s" % ("▶" if running else "…", "running" if running else "waiting", (t.get("name") or "")[:30],
                                                (t.get("user") or "")[:10], _res_text(t)[:26], _fmt_dur(now - (since or now)),
                                                "" if running else "  " + (t.get("reason") or "")))
    return 0


# ------------------------------------------------------------------- watch
def _pid_alive(pid: int) -> bool:
    if IS_WINDOWS:
        out = subprocess.run(["tasklist", "/FI", "PID eq %d" % pid, "/NH"], capture_output=True, text=True).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        with open("/proc/%d/stat" % pid) as f:
            return f.read().split(")")[-1].split()[0] != "Z"
    except OSError:
        return True


def cmd_watch(args: argparse.Namespace, hub: Hub) -> int:
    if not _pid_alive(args.pid):
        sys.stderr.write("avm watch: pid %d is not running\n" % args.pid)
        return 1
    if not args.foreground and not os.environ.get("AVM_WATCH_CHILD"):
        # detach: re-run ourselves in the background so the caller's shell returns at once
        env = dict(os.environ, AVM_WATCH_CHILD="1")
        kwargs: Dict[str, Any] = {}
        if IS_WINDOWS:
            kwargs["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        argv = [sys.executable, os.path.abspath(__file__), "watch", args.name, "--pid", str(args.pid), "--beat", str(args.beat)]
        if args.rc_file:
            argv.append("--rc-file=" + os.path.abspath(args.rc_file))
        if args.note:
            argv.append("--note=" + args.note)   # one token: a note starting with "-" stays a note
        subprocess.Popen(argv, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kwargs)
        sys.stderr.write("avm: watching pid %d as '%s' in the background\n" % (args.pid, args.name))
        return 0
    beat_every = max(MIN_BEAT, float(args.beat))
    tid = _start(hub, args.name, "watch", beat_every * 2, None, args.pid, args.note, task_log_on(hub.cfg))
    last = time.time()
    while _pid_alive(args.pid):   # look often, beat every beat_every: the end is reported promptly
        time.sleep(min(1.0, beat_every))
        if time.time() - last >= beat_every:
            last = time.time()
            hub.try_call("/tasks/%d/beat" % tid, {})
    rc: Optional[int] = None
    status, note = "done", "process exited (exit code unknown)"
    if args.rc_file:
        rc, why = _read_rc(args.rc_file)
        if rc is None:   # it died before writing it (crash, OOM kill): that is not a success
            status, note = "failed", "process exited; exit code unknown (%s)" % why
        else:
            status, note = ("done" if rc == 0 else "failed"), "process exited"
    hub.try_call("/tasks/%d/finish" % tid, {"status": status, "rc": rc, "note": note})
    return 0


def _read_rc(path: str, grace: Optional[float] = None) -> Tuple[Optional[int], str]:
    """(exit code, why not): waits up to WATCH_RC_GRACE for a wrapper to write it after the process is gone."""
    deadline = time.time() + (WATCH_RC_GRACE if grace is None else grace)
    while True:
        try:
            with open(path) as f:
                return int(f.read().strip()), ""
        except FileNotFoundError:
            why = "rc file missing"
        except ValueError:
            why = "rc file holds no exit code"
        except OSError as e:
            return None, "rc file unreadable: %s" % (e.strerror or e)
        if time.time() >= deadline:
            return None, why
        time.sleep(0.2)


# -------------------------------------------------------------------- task
def cmd_task(args: argparse.Namespace, hub: Hub) -> int:
    log_on = task_log_on(hub.cfg)
    if not log_on and (getattr(args, "log", None) or getattr(args, "note", None)):   # I-01: --note too
        sys.stderr.write("avm: the task log is off here (AVM_TASK_LOG / AVM_TASK_LOG_OFF_DIRS): --log lines and --note not sent\n")
        args.log = args.note = None
    if args.op == "start":
        tid = _start(hub, args.name, "task", args.expect, None, None, args.note, log_on)
        print(tid)
        return 0
    if args.op == "list":
        r = hub.call("/tasks/mine" + ("?status=%s" % args.status if args.status else ""), method="GET")
        for t in r.get("tasks", []):
            mark = {"running": "▶", "done": "✓", "failed": "✗", "stalled": "!", "lost": "?"}.get(t["status"], " ")
            print("%s %5d  %-10s %-32s %s  %s" % (mark, t["id"], t["status"], t["name"][:32], _fmt_dur(t["elapsed"]),
                                               ("rc=%s" % t["rc"]) if t.get("rc") is not None else ""))
        return 0
    tid = int(args.id)
    if args.op == "beat":
        body: Dict[str, Any] = {}
        if args.progress is not None:
            body["progress"] = args.progress
        if args.log:
            body["log"] = args.log
        if args.note:
            body["note"] = args.note
        hub.call("/tasks/%d/beat" % tid, body)
    elif args.op == "done":
        hub.call("/tasks/%d/finish" % tid, {"status": "done", "rc": 0, "note": args.note, "log": args.log or []})
    elif args.op == "fail":
        hub.call("/tasks/%d/finish" % tid, {"status": "failed", "rc": args.rc, "note": args.note, "log": args.log or []})
    return 0


def cmd_results(args: argparse.Namespace, hub: Hub) -> int:
    with open(args.file, "rb") as f:
        raw = f.read()
    try:
        doc = json.loads(raw)
    except ValueError as e:
        sys.stderr.write("avm: %s is not valid JSON: %s\n" % (args.file, e))
        return 2
    if isinstance(doc, dict) and doc.get("project") not in (None, args.project):
        sys.stderr.write("avm: %s says project %r, not %r\n" % (args.file, doc.get("project"), args.project))
        return 2
    r = hub.call("/projects/%s/results" % args.project, raw=raw, timeout=30.0)  # sent byte-for-byte
    print("pushed %s results #%s (%d bytes)" % (args.project, r.get("id"), len(raw)))
    return 0


# ------------------------------------------------------------------- agent
def _this_session() -> Dict[str, Any]:
    """Identify the Claude Code session avm is running inside, from the environment Claude Code sets.
    Background agents are keyed by their stable short id (the jobs dir name); interactive ones by session id."""
    sid = os.environ.get("CLAUDE_CODE_SESSION_ID") or ""
    job_dir = os.environ.get("CLAUDE_JOB_DIR") or ""
    info: Dict[str, Any] = {"session_id": sid or None, "cwd": os.getcwd()}
    if job_dir:
        info.update(key=os.path.basename(job_dir.rstrip("/\\")), kind="background")
    elif sid:
        info.update(key=sid, kind="desktop" if os.environ.get("CLAUDE_CODE_ENTRYPOINT") == "claude-desktop" else "interactive")
    pid = os.environ.get("CLAUDE_PID")
    if pid:  # Claude Code's own record of this session carries its display name and start directory
        try:
            with open(os.path.join(os.path.expanduser("~"), ".claude", "sessions", "%s.json" % int(pid)), encoding="utf-8") as f:
                rec = json.load(f)
            info["name"] = rec.get("name") or None
            info["cwd"] = rec.get("cwd") or info["cwd"]
        except (OSError, ValueError):
            pass
    return info


def cmd_agent(args: argparse.Namespace, hub: Hub) -> int:
    if args.op == "list":
        for t in hub.call("/agent/claude/tracked", method="GET").get("tracked", []):
            print("%s %-36s %-11s %-8s %s" % ("●" if t.get("live") else "○", (t.get("name") or "")[:36], t.get("kind") or "",
                                             t.get("state") or "", t["key"]))
        return 0
    me = _this_session()
    if not me.get("key"):
        sys.stderr.write("avm: not inside a Claude Code session (no CLAUDE_CODE_SESSION_ID / CLAUDE_JOB_DIR)\n")
        return 2
    if args.op == "untrack":
        hub.call("/agent/claude/untrack", {"key": me["key"]})
        print("untracked %s" % me["key"])
        return 0
    if args.name:
        me["name"] = args.name
    if args.note:
        me["note"] = args.note
    r = hub.call("/agent/claude/track", {k: v for k, v in me.items() if v is not None})
    row = r.get("row") or {}
    print("tracking %s (%s) on the Agents page" % (row.get("name") or me["key"], me["key"]))
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="avm", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version="avm " + AVM_VERSION)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("run", help="run a command as a tracked task")
    p.add_argument("name")
    p.add_argument("--beat", default=30, type=float,
                   help="heartbeat seconds (default 30, at least 5); the dashboard shows it stalled after 3x this with no beat")
    p.add_argument("--note", default=None)
    lg = p.add_mutually_exclusive_group()
    lg.add_argument("--log", dest="log_force", action="store_const", const=True, default=None,
                    help="send output lines to the hub even where AVM_TASK_LOG / AVM_TASK_LOG_OFF_DIRS say off")
    lg.add_argument("--no-log", dest="log_force", action="store_const", const=False,
                    help="send no output lines (nor command line / cwd) to the hub for this run")
    p.add_argument("--gpu", action="store_true",
                   help="uses the GPU; light job: may share it, also with a running --exclusive job (add --vram to be admitted on free VRAM)")
    p.add_argument("--exclusive", action="store_true",
                   help="heavy GPU job (implies --gpu): waits only for another --exclusive job; does not hold back light --gpu jobs")
    p.add_argument("--mem", default=None, help="RAM it expects to use, e.g. 19G")
    p.add_argument("--vram", default=None, help="VRAM it expects to use, e.g. 6G")
    p.add_argument("--wait", action="store_true", help="queue until it fits (first come, first served)")
    p.add_argument("--max-wait", default=None, help="give up after this long in the queue, e.g. 8h (exit 124)")
    p.add_argument("command", nargs="*", help="-- command args...")

    p = sub.add_parser("watch", help="track an already-running pid")
    p.add_argument("name")
    p.add_argument("--pid", required=True, type=int)
    p.add_argument("--beat", default=30, type=float, help="heartbeat seconds (default 30); stalled after 3x this with no beat")
    p.add_argument("--rc-file", default=None, help="file the process writes its exit code to (optional; missing at the end = failed)")
    p.add_argument("--note", default=None)
    p.add_argument("--foreground", action="store_true")

    p = sub.add_parser("task", help="manual lifecycle: start / beat / done / fail / list")
    tp = p.add_subparsers(dest="op", required=True)
    s = tp.add_parser("start"); s.add_argument("name"); s.add_argument("--expect", type=float, default=None, help="expected seconds between beats; stalled after 1.5x this with no beat"); s.add_argument("--note")
    s = tp.add_parser("beat"); s.add_argument("id"); s.add_argument("--progress", type=float); s.add_argument("--log", action="append"); s.add_argument("--note")
    s = tp.add_parser("done"); s.add_argument("id"); s.add_argument("--note"); s.add_argument("--log", action="append")
    s = tp.add_parser("fail"); s.add_argument("id"); s.add_argument("--rc", type=int, default=1); s.add_argument("--note"); s.add_argument("--log", action="append")
    s = tp.add_parser("list"); s.add_argument("--status")

    p = sub.add_parser("results", help="publish a results document to the dashboard's Research page")
    rp = p.add_subparsers(dest="op", required=True)
    s = rp.add_parser("push"); s.add_argument("project", help="project slug, e.g. myproject"); s.add_argument("file", help="results JSON")

    p = sub.add_parser("agent", help="track this Claude Code session on the dashboard's Agents page")
    ap2 = p.add_subparsers(dest="op", required=True)
    s = ap2.add_parser("track"); s.add_argument("--name", help="display name (default: the session's own name)"); s.add_argument("--note")
    ap2.add_parser("untrack")
    ap2.add_parser("list")

    p = sub.add_parser("check", help="this host's checks (and responders): list / add / set / rm")
    cp = p.add_subparsers(dest="op", required=True)
    s = cp.add_parser("list"); s.add_argument("--json", action="store_true")

    def check_opts(s: argparse.ArgumentParser) -> None:
        s.add_argument("--label")
        s.add_argument("--ttl", help="remove it after this long, e.g. 12h")
        s.add_argument("--notify", choices=("self", "new", "none"), help="who is prompted when it starts failing")
        s.add_argument("--cwd", help="working dir for a --notify new session")
        s.add_argument("--with-log", action="store_true", help="include related avm task log tails in the alert")
        s.add_argument("--cooldown", help="minimum time between alerts, e.g. 30m (default 30m)")
        s.add_argument("--daily-cap", type=int, help="alerts per rolling 24 h at most (default 6)")
    s = cp.add_parser("add"); s.add_argument("kind", choices=("process", "file", "tcp", "mount", "content", "marker", "nonempty", "unit", "job", "memory"))
    s.add_argument("target"); check_opts(s)
    s = cp.add_parser("set"); s.add_argument("id", type=int); s.add_argument("--target"); s.add_argument("--no-log", action="store_true"); check_opts(s)
    s = cp.add_parser("rm"); s.add_argument("what", nargs="+", help="ID, or KIND TARGET")

    p = sub.add_parser("queue", help="this host's resource queue: status / drain / resume")
    qp = p.add_subparsers(dest="op", required=True)
    s = qp.add_parser("status"); s.add_argument("--json", action="store_true")
    s = qp.add_parser("drain"); s.add_argument("--wait", action="store_true", help="return once nothing queued/declared is running")
    qp.add_parser("resume")

    argv = list(sys.argv[1:] if argv is None else argv)
    command: List[str] = []
    if "--" in argv:  # everything after `--` is the command, untouched by argparse
        i = argv.index("--")
        argv, command = argv[:i], argv[i + 1:]
    argv = [{"--gpu=exclusive": "--exclusive", "--gpu=shared": "--gpu"}.get(a, a) for a in argv]   # accept --gpu=exclusive
    args = ap.parse_args(argv)
    if args.cmd == "run":
        args.command = command or [c for c in (args.command or []) if c != "--"]
    if args.cmd == "queue":   # host-local: works without (or before) any hub config
        return cmd_queue(args, load_config(required=False))
    hub = Hub(load_config(required=args.cmd != "run"))   # avm run works without a hub (untracked): the command always runs
    try:
        return {"run": cmd_run, "watch": cmd_watch, "task": cmd_task, "results": cmd_results, "agent": cmd_agent, "check": cmd_check}[args.cmd](args, hub)
    except urllib.error.HTTPError as e:
        sys.stderr.write("avm: hub rejected the request: HTTP %s %s\n" % (e.code, e.read().decode(errors="replace")[:200]))
        return 1
    except Exception as e:
        sys.stderr.write("avm: %s\n" % e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
