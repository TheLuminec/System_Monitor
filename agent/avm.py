#!/usr/bin/env python3
"""avm - report tasks (training runs, services, anything long) to Avalon Monitor.

One command when you set something off:

    avm run "seed 3 training" -- python train.py --seed 3
        Runs the command, streams its last lines and a heartbeat to the dashboard,
        reports the exit code; non-zero -> the task shows red. Exit code is passed through.

    avm watch "nightly backup" --pid 4242 [--rc-file /path]
        Tracks an already-running process until it exits (detaches into the background).

    avm task start "data prep" [--expect 300] [--note "..."]   -> prints TASK_ID
    avm task beat  TASK_ID [--progress 0.4] [--log "epoch 3 done"]
    avm task done  TASK_ID [--note "..."]
    avm task fail  TASK_ID [--rc 1] [--note "why"]
    avm task list  [--status running]

    avm results push PROJECT FILE
        Publishes a results document (JSON, "<name>-results/<n>" schema) to the project's
        Research page; every push is kept as history. Aggregates only - the hub renders what you send.

    avm agent track [--name "XRSec Coordinator"] [--note "..."]
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
        --gpu / --exclusive  uses the GPU / is a heavy GPU job (one exclusive job at a time; light ones may share)
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

Needs the agent's config (server URL + host token). Looked up in order:
AVM_CONFIG, ~/.config/avalon-agent/agent.conf, /etc/avalon-agent/agent.conf,
%ProgramData%\\AvalonAgent\\agent.conf. Only one dependency: Python 3.8+.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import shlex
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from typing import Any, Dict, List, Optional, Tuple

AVM_VERSION = "1.8.0"
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


def load_config(required: bool = True) -> Dict[str, str]:
    cfg: Dict[str, str] = {}
    for path in _candidates():
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    for raw in f:
                        line = raw.strip()
                        if line and not line.startswith("#") and "=" in line:
                            k, _, v = line.partition("=")
                            cfg.setdefault(k.strip(), v.strip().strip('"').strip("'"))
            except OSError:
                continue
            if cfg.get("AVM_SERVER_URL") and cfg.get("AVM_TOKEN"):
                cfg["_path"] = path
                break
    for k in ("AVM_SERVER_URL", "AVM_TOKEN"):
        if os.environ.get(k):
            cfg[k] = os.environ[k]
    if required and (not cfg.get("AVM_SERVER_URL") or not cfg.get("AVM_TOKEN")):
        sys.stderr.write("avm: no agent config found (looked in %s); set AVM_SERVER_URL and AVM_TOKEN\n" % ", ".join(_candidates()))
        sys.exit(2)
    return cfg


class Hub:
    def __init__(self, cfg: Dict[str, str]):
        self.cfg = cfg
        self.base = cfg["AVM_SERVER_URL"].rstrip("/") + "/api/v1"
        self.token = cfg["AVM_TOKEN"]

    def call(self, path: str, body: Optional[dict] = None, method: str = "POST", timeout: float = 10.0, raw: Optional[bytes] = None) -> dict:
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request(self.base + path, method=method, data=data,
                                     headers={"Content-Type": "application/json", "Authorization": "Bearer " + self.token,
                                              "User-Agent": "avm/" + AVM_VERSION})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8") or "{}")

    def try_call(self, path: str, body: Optional[dict] = None, method: str = "POST") -> Optional[dict]:
        try:
            return self.call(path, body, method)
        except urllib.error.HTTPError as e:
            sys.stderr.write("avm: hub rejected %s: HTTP %s %s\n" % (path, e.code, e.read().decode(errors="replace")[:160]))
        except Exception as e:
            sys.stderr.write("avm: cannot reach hub for %s: %s\n" % (path, e))
        return None


def _start(hub: Hub, name: str, source: str, expect: Optional[float], command: Optional[str], pid: Optional[int], note: Optional[str]) -> int:
    body = {"name": name, "source": source, "expect_beat": expect, "command": command, "cwd": os.getcwd(), "pid": pid,
            "user": _user(), "meta": {"hostname": socket.gethostname(), "note": note} if note else {"hostname": socket.gethostname()}}
    r = hub.call("/tasks", body)
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


class Queue:
    def __init__(self, cfg: Dict[str, str]):
        self.dir = self._dir(cfg)
        self.headroom = float(cfg.get("AVM_QUEUE_HEADROOM_GB") or 2)
        self.settle = float(cfg.get("AVM_QUEUE_SETTLE_SEC") or 180)
        self.me: Optional[Dict[str, Any]] = None
        self._lockf = None

    @staticmethod
    def _dir(cfg: Dict[str, str]) -> str:
        d = os.environ.get("AVM_QUEUE_DIR") or cfg.get("AVM_QUEUE_DIR") or (
            os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"), "AvalonAgent", "queue") if IS_WINDOWS else "/tmp/avm-queue")
        try:
            os.makedirs(d, exist_ok=True)
            if not IS_WINDOWS and os.stat(d).st_uid == os.getuid():
                os.chmod(d, 0o1777)   # shared by every user on the host; sticky: only owners remove their tickets
            probe = os.path.join(d, ".probe-%d" % os.getpid())
            open(probe, "w").close()
            os.remove(probe)
        except OSError:
            d = os.path.join(tempfile.gettempdir(), "avm-queue-" + _user())
            os.makedirs(d, exist_ok=True)
        return d

    def _path(self, tid: str, ext: str) -> str:
        return os.path.join(self.dir, tid + ext)

    def _alive(self, tid: str) -> bool:
        try:
            f = open(self._path(tid, ".lock"), "rb")
        except OSError:
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
                with open(self._path(tid, ".json"), encoding="utf-8") as f:
                    out.append(json.load(f))
            except (OSError, ValueError):
                continue   # being rewritten right now
        return sorted(out, key=lambda t: t.get("seq", ""))

    def register(self, info: Dict[str, Any]) -> None:
        tid = "%019d-%d" % (int(time.time() * 1e6), os.getpid())
        self._lockf = open(self._path(tid, ".lock"), "w")   # lock first, then publish: a visible ticket is always locked
        self._lockf.write("x")
        self._lockf.flush()
        if not _try_lock(self._lockf):
            raise OSError("cannot lock queue ticket in %s" % self.dir)
        try:
            os.chmod(self._path(tid, ".lock"), 0o644)
        except OSError:
            pass
        self.me = dict(info, id=tid, seq=tid, pid=os.getpid(), user=_user(), queued_at=time.time())
        self.save()

    def save(self) -> None:
        if not self.me:
            return
        path = self._path(self.me["id"], ".json")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.me, f)
        try:
            os.chmod(tmp, 0o644)
        except OSError:
            pass
        for attempt in range(20):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:   # Windows: someone is reading it right now
                time.sleep(0.05)

    def release(self) -> None:
        if self.me:
            for ext in (".json", ".json.tmp"):
                try:
                    os.remove(self._path(self.me["id"], ext))
                except OSError:
                    pass
        if self._lockf:
            _unlock(self._lockf)
            self._lockf.close()
            try:
                os.remove(self._path(self.me["id"], ".lock"))
            except OSError:
                pass
        self.me, self._lockf = None, None

    def draining(self) -> Optional[str]:
        try:
            with open(os.path.join(self.dir, "DRAIN"), encoding="utf-8") as f:
                return f.read().strip() or "draining"
        except OSError:
            return None

    def impossible(self, res: Dict[str, Any]) -> Optional[str]:
        """Reject at submit what could never fit, instead of queueing it forever."""
        _, total = _mem_gb()
        if res.get("mem_gb") and total and res["mem_gb"] + self.headroom > total:
            return "asks for %g GB RAM but this host has %.1f GB (%.1f GB after %g GB headroom)" % (
                res["mem_gb"], total, total - self.headroom, self.headroom)
        if res.get("vram_gb"):
            _, vtotal = _vram_gb()
            if vtotal is None:
                return "asks for VRAM but no NVIDIA GPU was found (nvidia-smi)"
            if res["vram_gb"] > vtotal:
                return "asks for %g GB VRAM but the GPU has %.1f GB" % (res["vram_gb"], vtotal)
        return None

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
        if me.get("gpu") == "exclusive":
            holder = next((t for t in running if t.get("gpu") == "exclusive"), None)
            if holder:
                return False, "GPU in exclusive use by '%s'" % holder.get("name"), 1
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
            lockf = open(os.path.join(self.dir, "admit.lock"), "a+")
        except OSError:
            lockf = open(os.path.join(self.dir, "admit.lock"), "r")
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
    if res or args.wait:
        return _run_queued(args, hub, command, shown, res, max_wait)
    beat_every = max(5.0, float(args.beat))
    expect = beat_every * 2
    tid = _start(hub, args.name, "run", expect, shown, None, args.note)
    sys.stderr.write("avm: task #%d '%s' started (%s)\n" % (tid, args.name, shown[:80]))
    return _execute(hub, args, command, tid, beat_every)


def _execute(hub: Hub, args: argparse.Namespace, command: Any, tid: Optional[int], beat_every: float,
             env: Optional[Dict[str, str]] = None) -> int:
    """Run the command, stream its output through untouched, beat the task, report the exit code (passed through)."""
    def call(path: str, body: dict) -> None:
        if tid is not None:
            hub.try_call(path % tid, body)

    tail: deque = deque(maxlen=60)
    pending: List[str] = []
    lock = threading.Lock()
    t0 = time.time()
    try:
        proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, shell=isinstance(command, str),
                                bufsize=1, universal_newlines=True, errors="replace", env=env)
    except OSError as e:
        call("/tasks/%d/finish", {"status": "failed", "rc": 127, "log": ["cannot start: %s" % e]})
        sys.stderr.write("avm: cannot start command: %s\n" % e)
        return 127
    call("/tasks/%d/beat", {"pid": proc.pid})

    stop = threading.Event()

    def beater() -> None:
        while not stop.wait(beat_every):
            with lock:
                lines, pending[:] = pending[:], []
            call("/tasks/%d/beat", {"log": lines[-40:]})

    th = threading.Thread(target=beater, daemon=True)
    th.start()

    def forward(signum, frame):  # pass Ctrl-C / SIGTERM to the child; we report what happens
        try:
            proc.send_signal(signum)
        except Exception:
            pass

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, forward)
        except (ValueError, OSError):
            pass
    assert proc.stdout is not None
    for line in proc.stdout:
        sys.stdout.write(line)
        sys.stdout.flush()
        line = line.rstrip("\n")
        with lock:
            tail.append(line)
            pending.append(line)
    rc = proc.wait()
    stop.set()
    with lock:
        lines, pending[:] = pending[:], []
    status = "done" if rc == 0 else "failed"
    call("/tasks/%d/finish", {"status": status, "rc": rc, "log": lines[-40:],
                              "note": "%s after %s" % (status, _fmt_dur(time.time() - t0))})
    sys.stderr.write("avm: task %s %s (rc=%s, %s)\n" % ("#%d" % tid if tid is not None else "(untracked)", status, rc, _fmt_dur(time.time() - t0)))
    return rc


def _try_start(hub: Hub, body: Dict[str, Any]) -> Optional[int]:
    """Create the dashboard task, but never let the hub block a queued job: None when it can't be reached."""
    r = hub.try_call("/tasks", body)
    return int(r["id"]) if r and r.get("id") is not None else None


def _run_queued(args: argparse.Namespace, hub: Hub, command: Any, shown: str, res: Dict[str, Any], max_wait: Optional[float]) -> int:
    """avm run with resource tags and/or --wait: declare, queue until it fits, then run exactly like a plain avm run."""
    q = Queue(hub.cfg)
    beat_every = max(5.0, float(args.beat))
    meta: Dict[str, Any] = {"hostname": socket.gethostname(), "resources": res}
    if args.note:
        meta["note"] = args.note
    body = {"name": args.name, "source": "run", "command": shown, "cwd": os.getcwd(), "pid": None, "user": _user(), "meta": meta}
    env = dict(os.environ)

    parent = os.environ.get("AVM_QUEUE_HOLD")
    if parent and q._alive(parent):   # nested inside a queued job: use its reservation, never wait behind it
        sys.stderr.write("avm: inside a queued job's reservation (%s): starting at once\n" % parent)
        meta["queue"] = {"inherited": parent}
        tid = _try_start(hub, dict(body, expect_beat=beat_every * 2))
        if tid is not None:
            env["AVM_TASK_ID"] = str(tid)
        return _execute(hub, args, command, tid, beat_every, env)

    if args.wait:
        why = q.impossible(res)
        if why:
            sys.stderr.write("avm: '%s' can never start on this host: %s\n" % (args.name, why))
            return 2
    q.register({"name": args.name, "state": "waiting" if args.wait else "running", **res,
                **({} if args.wait else {"started_at": time.time()})})
    tid: Optional[int] = None
    try:
        if args.wait:
            ok, why, pos = q.try_admit()
            if not ok:
                since = q.me["queued_at"] if q.me else time.time()
                meta["queue"] = {"since": since, "reason": why, "position": pos, "max_wait": max_wait}
                tid = _try_start(hub, dict(body, status="queued", expect_beat=max(QUEUE_BEAT, beat_every) * 2))
                sys.stderr.write("avm: '%s' queued (%s)%s\n" % (args.name, why, "; task #%d" % tid if tid is not None else ""))

                def interrupted(signum, frame):
                    raise KeyboardInterrupt
                try:
                    signal.signal(signal.SIGTERM, interrupted)
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
                            note = "gave up after waiting %s (--max-wait): %s" % (_fmt_dur(waited), why)
                            if tid is not None:
                                hub.try_call("/tasks/%d/finish" % tid, {"status": "failed", "rc": EXIT_GAVE_UP, "note": note})
                            sys.stderr.write("avm: '%s' %s\n" % (args.name, note))
                            return EXIT_GAVE_UP
                        if why != last_why:
                            sys.stderr.write("avm: '%s' still queued: %s\n" % (args.name, why))
                            last_why = why
                        if time.time() - last_beat >= QUEUE_BEAT:
                            last_beat = time.time()
                            meta["queue"] = {"since": since, "reason": why, "position": pos, "max_wait": max_wait}
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
                    try:
                        signal.signal(signal.SIGTERM, signal.SIG_DFL)
                    except (ValueError, OSError):
                        pass
                meta["queue"] = {"since": since, "waited": time.time() - since}
                sys.stderr.write("avm: '%s' admitted after %s\n" % (args.name, _fmt_dur(time.time() - since)))
            else:
                meta["queue"] = {"since": q.me["queued_at"] if q.me else time.time(), "waited": 0}
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
        sys.stderr.write("avm: task %s '%s' started [%s] (%s)\n" % ("#%d" % tid if tid is not None else "(hub unreachable: untracked)",
                                                                   args.name, _res_text(res), shown[:80]))
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
    q = Queue(cfg)
    flag = os.path.join(q.dir, "DRAIN")
    if args.op == "drain":
        if not q.draining():
            with open(flag, "w", encoding="utf-8") as f:
                f.write("drained by %s at %s" % (_user(), time.strftime("%Y-%m-%d %H:%M")))
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
        subprocess.Popen([sys.executable, os.path.abspath(__file__), "watch", args.name, "--pid", str(args.pid), "--beat", str(args.beat)]
                         + (["--rc-file", args.rc_file] if args.rc_file else []), env=env, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kwargs)
        sys.stderr.write("avm: watching pid %d as '%s' in the background\n" % (args.pid, args.name))
        return 0
    beat_every = max(5.0, float(args.beat))
    tid = _start(hub, args.name, "watch", beat_every * 2, None, args.pid, args.note)
    while _pid_alive(args.pid):
        time.sleep(beat_every)
        hub.try_call("/tasks/%d/beat" % tid, {})
    rc: Optional[int] = None
    if args.rc_file and os.path.isfile(args.rc_file):
        try:
            with open(args.rc_file) as f:
                rc = int(f.read().strip())
        except (OSError, ValueError):
            rc = None
    status = "failed" if (rc is not None and rc != 0) else "done"
    hub.try_call("/tasks/%d/finish" % tid, {"status": status, "rc": rc, "note": "process exited" + ("" if rc is not None else " (exit code unknown)")})
    return 0


# -------------------------------------------------------------------- task
def cmd_task(args: argparse.Namespace, hub: Hub) -> int:
    if args.op == "start":
        tid = _start(hub, args.name, "task", args.expect, None, None, args.note)
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
    p.add_argument("--beat", default=30, type=float, help="heartbeat seconds (default 30; stalled after 2x)")
    p.add_argument("--note", default=None)
    p.add_argument("--gpu", action="store_true", help="uses the GPU (light jobs may share it)")
    p.add_argument("--exclusive", action="store_true", help="heavy GPU job: one exclusive job at a time (implies --gpu)")
    p.add_argument("--mem", default=None, help="RAM it expects to use, e.g. 19G")
    p.add_argument("--vram", default=None, help="VRAM it expects to use, e.g. 6G")
    p.add_argument("--wait", action="store_true", help="queue until it fits (first come, first served)")
    p.add_argument("--max-wait", default=None, help="give up after this long in the queue, e.g. 8h (exit 124)")
    p.add_argument("command", nargs="*", help="-- command args...")

    p = sub.add_parser("watch", help="track an already-running pid")
    p.add_argument("name")
    p.add_argument("--pid", required=True, type=int)
    p.add_argument("--beat", default=30, type=float)
    p.add_argument("--rc-file", default=None, help="file the process writes its exit code to (optional)")
    p.add_argument("--note", default=None)
    p.add_argument("--foreground", action="store_true")

    p = sub.add_parser("task", help="manual lifecycle: start / beat / done / fail / list")
    tp = p.add_subparsers(dest="op", required=True)
    s = tp.add_parser("start"); s.add_argument("name"); s.add_argument("--expect", type=float, default=None, help="seconds between beats before it counts as stalled"); s.add_argument("--note")
    s = tp.add_parser("beat"); s.add_argument("id"); s.add_argument("--progress", type=float); s.add_argument("--log", action="append"); s.add_argument("--note")
    s = tp.add_parser("done"); s.add_argument("id"); s.add_argument("--note"); s.add_argument("--log", action="append")
    s = tp.add_parser("fail"); s.add_argument("id"); s.add_argument("--rc", type=int, default=1); s.add_argument("--note"); s.add_argument("--log", action="append")
    s = tp.add_parser("list"); s.add_argument("--status")

    p = sub.add_parser("results", help="publish a results document to the dashboard's Research page")
    rp = p.add_subparsers(dest="op", required=True)
    s = rp.add_parser("push"); s.add_argument("project", help="project slug, e.g. xrsec"); s.add_argument("file", help="results JSON")

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
        s.add_argument("--daily-cap", type=int, help="alerts per day at most (default 6)")
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
    hub = Hub(load_config())
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
