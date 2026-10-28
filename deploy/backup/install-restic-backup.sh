#!/usr/bin/env bash
# Install (or update) a nightly restic backup that shows up on Avalon Monitor - the host's Backups card, an
# "<name>" task for every run, and an alert when it fails or goes stale. Linux + systemd.
#
#   sudo deploy/backup/install-restic-backup.sh deploy/backup/<host>.json [--now]
#
# The JSON config names the repository, the paths, excludes, retention and schedule (see avalon.json, and
# docs/HOSTS.md "Backups"). This script:
#   - installs restic (apt) if missing; creates the repository password (printed ONCE, to the terminal only) and
#     initialises the repository when they don't exist yet - an existing repository and password are kept
#   - copies the runner to /usr/local/sbin/<unit>, the config to /etc/avalon-backup/config.json and a root-owned copy
#     of avm (the root service never runs a file a user account can edit)
#   - writes <unit>.service (oneshot, lowest CPU/IO priority, wrapped in `avm run`) + <unit>.timer and enables it
#   - seeds the status file from the repository's latest snapshot, so the card has data before the next run
#   --now  also starts a backup right away
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }
HERE="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"; REPO_ROOT="$(dirname "$(dirname "$HERE")")"
CONF_SRC="${1:?usage: install-restic-backup.sh <config.json> [--now]}"; NOW="${2:-}"
exec 3>/dev/tty || exec 3>&2

# config: validate, fill the unit/timer names, and read the fields this script needs
CONF=$(python3 -I - "$CONF_SRC" <<'EOF'
import json, sys
c = json.load(open(sys.argv[1]))
for k in ("name", "repository", "password_file", "paths"):
    assert c.get(k), f"config is missing {k!r}"
c.setdefault("unit", "avalon-backup"); c.setdefault("schedule", "*-*-* 03:30"); c.setdefault("max_run_sec", 43200)
c["timer"] = c["unit"] + ".timer"
print(json.dumps(c, indent=2))
EOF
)
get() { python3 -I -c 'import json,sys; v=json.loads(sys.argv[1]).get(sys.argv[2]); print("" if v is None else v)' "$CONF" "$1"; }
NAME=$(get name); REPO=$(get repository); PASS=$(get password_file); UNIT=$(get unit); SCHED=$(get schedule); MNT=$(get require_mount); MAXRUN=$(get max_run_sec)
AVM_CONF="/home/${SUDO_USER:-root}/.config/avalon-agent/agent.conf"; [ -f "$AVM_CONF" ] || AVM_CONF=/etc/avalon-agent/agent.conf
[ -z "$MNT" ] || mountpoint -q "$MNT" || { echo "$MNT is not mounted - mount it first"; exit 1; }

command -v restic >/dev/null || apt-get install -y -o Acquire::Retries=3 restic
echo "restic: $(restic version)"
if [ ! -s "$PASS" ]; then
  (umask 077; openssl rand -base64 30 > "$PASS"); NEWPASS=1
  # shown right away (terminal only, never the log): a later step failing must not leave a password nobody has seen
  { echo; echo "  RESTIC PASSWORD for $REPO - save it in your password manager NOW:"; echo; echo "      $(cat "$PASS")"; echo
    echo "  Without it the backup cannot be restored if this machine's disk dies."; echo; } >&3
fi
chmod 600 "$PASS"
if [ -f "$REPO/config" ]; then echo "repository: $REPO (existing, kept)"
else RESTIC_PASSWORD_FILE="$PASS" restic init --repo "$REPO"; fi

install -d -m 755 /etc/avalon-backup /usr/local/lib/avalon-backup /var/lib/avalon-backup
install -m 755 -o root -g root "$HERE/restic-backup.py" "/usr/local/sbin/$UNIT"
install -m 644 -o root -g root "$REPO_ROOT/agent/avm.py" /usr/local/lib/avalon-backup/avm.py
printf '%s\n' "$CONF" > /etc/avalon-backup/config.json; chmod 644 /etc/avalon-backup/config.json
echo "installed /usr/local/sbin/$UNIT and /etc/avalon-backup/config.json"

cat > "/etc/systemd/system/$UNIT.service" <<EOF
[Unit]
Description=$NAME (restic backup, shown on Avalon Monitor)
${MNT:+RequiresMountsFor=$MNT}

[Service]
Type=oneshot
Environment=AVM_CONFIG=$AVM_CONF
ExecStart=/usr/bin/python3 -I /usr/local/lib/avalon-backup/avm.py run --beat 60 "$NAME" -- /usr/local/sbin/$UNIT
Nice=19
IOSchedulingClass=idle
CPUSchedulingPolicy=batch
TimeoutStartSec=$MAXRUN
EOF
cat > "/etc/systemd/system/$UNIT.timer" <<EOF
[Unit]
Description=$NAME ($SCHED)

[Timer]
OnCalendar=$SCHED
RandomizedDelaySec=15min
Persistent=true

[Install]
WantedBy=timers.target
EOF
systemctl daemon-reload
systemctl enable --now "$UNIT.timer"
systemctl list-timers "$UNIT.timer" --no-pager --no-legend || true   # no `| head`: under pipefail its SIGPIPE ended the script

echo "== status file"
# keeps a status from a real run (a failure stays visible); `/usr/local/sbin/$UNIT --seed --force` overwrites it
"/usr/local/sbin/$UNIT" --seed || echo "(no snapshot yet - the card fills in after the first run)"
if [ "$NOW" = "--now" ]; then systemctl start --no-block "$UNIT.service"; echo "backup started: watch the '$NAME' task"; fi

if [ -n "${NEWPASS:-}" ]; then
  echo "  (reminder: the new restic password for $REPO was printed above - save it before closing this terminal)" >&3
fi
