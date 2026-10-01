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

Needs the agent's config (server URL + host token). Looked up in order:
AVM_CONFIG, ~/.config/avalon-agent/agent.conf, /etc/avalon-agent/agent.conf,
%ProgramData%\\AvalonAgent\\agent.conf. Only one dependency: Python 3.8+.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import shlex
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from typing import Any, Dict, List, Optional

AVM_VERSION = "1.3.0"
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


def load_config() -> Dict[str, str]:
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
    if not cfg.get("AVM_SERVER_URL") or not cfg.get("AVM_TOKEN"):
        sys.stderr.write("avm: no agent config found (looked in %s); set AVM_SERVER_URL and AVM_TOKEN\n" % ", ".join(_candidates()))
        sys.exit(2)
    return cfg


class Hub:
    def __init__(self, cfg: Dict[str, str]):
        self.base = cfg["AVM_SERVER_URL"].rstrip("/") + "/api/v1"
        self.token = cfg["AVM_TOKEN"]

    def call(self, path: str, body: Optional[dict] = None, method: str = "POST", timeout: float = 10.0) -> dict:
        req = urllib.request.Request(self.base + path, method=method, data=json.dumps(body).encode() if body is not None else None,
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


# --------------------------------------------------------------------- run
def cmd_run(args: argparse.Namespace, hub: Hub) -> int:
    command = args.command
    if not command:
        sys.stderr.write("avm run: give the command after `--`, e.g. avm run \"name\" -- python train.py\n")
        return 2
    if len(command) == 1 and (" " in command[0]):
        command = command[0] if IS_WINDOWS else shlex.split(command[0])
    shown = command if isinstance(command, str) else " ".join(shlex.quote(c) for c in command)
    beat_every = max(5.0, float(args.beat))
    expect = beat_every * 2
    tid = _start(hub, args.name, "run", expect, shown, None, args.note)
    sys.stderr.write("avm: task #%d '%s' started (%s)\n" % (tid, args.name, shown[:80]))

    tail: deque = deque(maxlen=60)
    pending: List[str] = []
    lock = threading.Lock()
    t0 = time.time()
    try:
        proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, shell=isinstance(command, str),
                                bufsize=1, universal_newlines=True, errors="replace")
    except OSError as e:
        hub.try_call("/tasks/%d/finish" % tid, {"status": "failed", "rc": 127, "log": ["cannot start: %s" % e]})
        sys.stderr.write("avm: cannot start command: %s\n" % e)
        return 127
    hub.try_call("/tasks/%d/beat" % tid, {"pid": proc.pid})

    stop = threading.Event()

    def beater() -> None:
        while not stop.wait(beat_every):
            with lock:
                lines, pending[:] = pending[:], []
            hub.try_call("/tasks/%d/beat" % tid, {"log": lines[-40:]})

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
    hub.try_call("/tasks/%d/finish" % tid, {"status": status, "rc": rc, "log": lines[-40:],
                                             "note": "%s after %s" % (status, _fmt_dur(time.time() - t0))})
    sys.stderr.write("avm: task #%d %s (rc=%s, %s)\n" % (tid, status, rc, _fmt_dur(time.time() - t0)))
    return rc


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


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="avm", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version="avm " + AVM_VERSION)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("run", help="run a command as a tracked task")
    p.add_argument("name")
    p.add_argument("--beat", default=30, type=float, help="heartbeat seconds (default 30; stalled after 2x)")
    p.add_argument("--note", default=None)
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

    argv = list(sys.argv[1:] if argv is None else argv)
    command: List[str] = []
    if "--" in argv:  # everything after `--` is the command, untouched by argparse
        i = argv.index("--")
        argv, command = argv[:i], argv[i + 1:]
    args = ap.parse_args(argv)
    if args.cmd == "run":
        args.command = command or [c for c in (args.command or []) if c != "--"]
    hub = Hub(load_config())
    try:
        return {"run": cmd_run, "watch": cmd_watch, "task": cmd_task}[args.cmd](args, hub)
    except urllib.error.HTTPError as e:
        sys.stderr.write("avm: hub rejected the request: HTTP %s %s\n" % (e.code, e.read().decode(errors="replace")[:200]))
        return 1
    except Exception as e:
        sys.stderr.write("avm: %s\n" % e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
