#!/usr/bin/env python3
"""Host enrollment CLI for Avalon Monitor.

    manage.py add-host NAME [--display "Pretty Name"] [--notes "..."]
    manage.py list-hosts
    manage.py rotate-token NAME
    manage.py disable NAME | enable NAME
    manage.py remove NAME
    manage.py stats
    manage.py checks NAME [--set AVM_WATCH_PROCESSES=nginx,sshd ...] [--clear]
    manage.py respawn NAME|all        # agent re-execs itself on its next push

Uses the same AVM_* environment (or .env) as the server, so run it with the
server's environment file, e.g.:

    AVM_ENV_FILE=/etc/avalon-monitor/monitor.env ./manage.py add-host desktop-c
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from avalon_monitor.config import settings  # noqa: E402
from avalon_monitor.db import Database  # noqa: E402


def _age(ts):
    if not ts:
        return "never"
    d = int(time.time() - ts)
    if d < 90:
        return f"{d}s ago"
    if d < 5400:
        return f"{d // 60}m ago"
    if d < 172800:
        return f"{d // 3600}h ago"
    return f"{d // 86400}d ago"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("add-host", help="enroll a new host and print its token")
    p.add_argument("name", help="short unique name, e.g. desktop-c")
    p.add_argument("--display", default=None)
    p.add_argument("--notes", default=None)

    sub.add_parser("list-hosts", help="list enrolled hosts")
    sub.add_parser("stats", help="database statistics")
    for c in ("rotate-token", "disable", "enable", "remove"):
        sub.add_parser(c).add_argument("name")
    p = sub.add_parser("checks", help="show or set a host's remote check settings")
    p.add_argument("name")
    p.add_argument("--set", action="append", default=[], metavar="KEY=VALUE")
    p.add_argument("--clear", action="store_true", help="remove all remote settings (agent falls back to its file)")
    sub.add_parser("respawn", help="ask an agent (or all) to re-exec itself").add_argument("name")

    args = ap.parse_args(argv)
    db = Database(settings.db_path)
    try:
        if args.cmd == "add-host":
            host, token = db.create_host(args.name, args.display, args.notes)
            print(f"Enrolled host '{host.name}' (id {host.id}).\n")
            print("Agent token (shown once, store it in the agent's config):\n")
            print(f"    {token}\n")
            print("Agent config lines:")
            print(f"    AVM_SERVER_URL=http://<avalon-tailscale-ip-or-name>:{settings.bind_port}")
            print(f"    AVM_TOKEN={token}")
        elif args.cmd == "list-hosts":
            hosts = db.list_hosts()
            if not hosts:
                print("no hosts enrolled")
            for h in hosts:
                flag = "" if h.enabled else " (disabled)"
                print(f"{h.id:>3}  {h.name:<20} last seen {_age(h.last_seen):<10} from {h.last_ip or '-'}{flag}")
        elif args.cmd == "rotate-token":
            print(db.rotate_token(args.name))
        elif args.cmd == "disable":
            db.set_enabled(args.name, False)
            print(f"disabled {args.name}")
        elif args.cmd == "enable":
            db.set_enabled(args.name, True)
            print(f"enabled {args.name}")
        elif args.cmd == "remove":
            db.delete_host(args.name)
            print(f"removed {args.name} and its history")
        elif args.cmd == "checks":
            from avalon_monitor.main import REMOTE_KEYS
            host = db.host_by_name(args.name)
            if not host:
                raise KeyError(args.name)
            cfg = dict(host.check_config or {})
            if args.clear:
                cfg = {}
            for item in args.set:
                k, _, v = item.partition("=")
                if k not in REMOTE_KEYS:
                    print(f"error: {k} is not remotely settable ({', '.join(REMOTE_KEYS)})", file=sys.stderr)
                    return 1
                cfg[k] = v.strip()
            if args.set or args.clear:
                rev = db.set_check_config(args.name, cfg)
                print(f"saved revision {rev}; the agent applies it on its next push")
            for k, v in cfg.items():
                print(f"{k}={v}")
            if not cfg:
                print("(no remote settings; agent uses its local agent.conf)")
        elif args.cmd == "respawn":
            targets = [h.name for h in db.list_hosts()] if args.name == "all" else [args.name]
            for n in targets:
                db.set_command(n, "respawn")
                print(f"queued respawn for {n}")
        elif args.cmd == "stats":
            for k, v in db.stats().items():
                print(f"{k:<14} {v}")
    except KeyError as e:
        print(f"error: no such host {e}", file=sys.stderr)
        return 1
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
