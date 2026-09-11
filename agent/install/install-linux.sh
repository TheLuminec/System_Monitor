#!/usr/bin/env bash
# Install the Avalon Monitor agent as a systemd service on any Linux (Debian/Ubuntu/Mint,
# Fedora, Arch, Raspberry Pi OS, ...).
#
#   sudo ./install-linux.sh --server http://avalon:8787 --token avm_xxx [--name miami] [--interval 5]
#   sudo ./install-linux.sh --upgrade        # re-copy the script, keep config
#
# Rootless variant (no sudo; venv under ~/.local, systemd --user unit, survives logout/reboot
# once `loginctl enable-linger $USER` has been run - the same pattern as a user-level queue runner):
#   ./install-linux.sh --user --server http://avalon:8787 --token avm_xxx
#
# Copy this directory (agent/) to the target machine first, e.g.
#   scp -r agent/ user@miami:~/avalon-agent && ssh user@miami 'sudo ~/avalon-agent/install/install-linux.sh --server ... --token ...'
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_DIR=/opt/avalon-agent
ETC_DIR=/etc/avalon-agent
SERVER="" TOKEN="" NAME="" INTERVAL="" UPGRADE=0 USER_MODE=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --server) SERVER="$2"; shift 2;;
    --token) TOKEN="$2"; shift 2;;
    --name) NAME="$2"; shift 2;;
    --interval) INTERVAL="$2"; shift 2;;
    --upgrade) UPGRADE=1; shift;;
    --user) USER_MODE=1; shift;;
    -h|--help) sed -n '2,12p' "$0"; exit 0;;
    *) echo "unknown option $1"; exit 1;;
  esac
done

if [[ $USER_MODE -eq 1 ]]; then
  [[ $EUID -ne 0 ]] || { echo "--user mode must run as the target user, not root"; exit 1; }
  APP_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/avalon-agent"
  ETC_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/avalon-agent"
  UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
  SYSTEMCTL="systemctl --user"
else
  [[ $EUID -eq 0 ]] || { echo "run as root: sudo $0 ...   (or use --user for a rootless install)"; exit 1; }
  UNIT_DIR=/etc/systemd/system
  SYSTEMCTL="systemctl"
fi
if [[ $UPGRADE -eq 0 && ( -z "$SERVER" || -z "$TOKEN" ) ]]; then
  echo "--server and --token are required (or --upgrade)"; exit 1
fi
command -v python3 >/dev/null || { echo "python3 is required (apt install python3 python3-venv)"; exit 1; }

echo "==> installing to $APP_DIR"
mkdir -p "$APP_DIR" "$ETC_DIR"
cp "$SRC_DIR/avalon_agent.py" "$APP_DIR/avalon_agent.py"

if [[ ! -x "$APP_DIR/venv/bin/python" ]]; then
  if python3 -m venv "$APP_DIR/venv" 2>/dev/null; then :; else
    echo "python3 -m venv failed; install the venv module first (apt install python3-venv) or use --system-site-packages"
    exit 1
  fi
fi
"$APP_DIR/venv/bin/pip" install --quiet --upgrade pip >/dev/null
"$APP_DIR/venv/bin/pip" install --quiet -r "$SRC_DIR/requirements.txt"

if [[ $UPGRADE -eq 0 ]]; then
  cat > "$ETC_DIR/agent.conf" <<CONF
AVM_SERVER_URL=$SERVER
AVM_TOKEN=$TOKEN
AVM_INTERVAL=${INTERVAL:-5}
${NAME:+AVM_HOSTNAME=$NAME}
AVM_PROCESSES=8
# Liveness checks - see agent.conf.example for AVM_WATCH_PROCESSES / AVM_WATCH_FILES / AVM_WATCH_TCP
CONF
  chmod 600 "$ETC_DIR/agent.conf"
fi

echo "==> checking connectivity"
if ! "$APP_DIR/venv/bin/python" "$APP_DIR/avalon_agent.py" --config "$ETC_DIR/agent.conf" --check; then
  echo "    (the service will keep retrying; fix $ETC_DIR/agent.conf and restart it)"
fi

echo "==> systemd ($SYSTEMCTL)"
mkdir -p "$UNIT_DIR"
# Rewrite the unit's paths for this install location; user units drop the root-only hardening keys.
sed -e "s|/opt/avalon-agent|$APP_DIR|g" -e "s|/etc/avalon-agent|$ETC_DIR|g" \
  "$SRC_DIR/install/avalon-agent.service" > "$UNIT_DIR/avalon-agent.service"
if [[ $USER_MODE -eq 1 ]]; then
  sed -i -e '/^ProtectHome=/d' -e '/^ProtectSystem=/d' -e '/^PrivateTmp=/d' -e '/^# Runs as root/d' \
         -e 's/^WantedBy=multi-user.target/WantedBy=default.target/' "$UNIT_DIR/avalon-agent.service"
fi
if [[ "${AVM_SKIP_SYSTEMD:-0}" != "1" ]]; then
  $SYSTEMCTL daemon-reload
  $SYSTEMCTL enable --now avalon-agent.service
  $SYSTEMCTL restart avalon-agent.service
  sleep 1
  $SYSTEMCTL --no-pager --lines=3 status avalon-agent.service || true
fi
echo
if [[ $USER_MODE -eq 1 ]]; then
  if loginctl show-user "$USER" 2>/dev/null | grep -q '^Linger=yes'; then
    echo "Linger is enabled: the agent will keep running after logout and start at boot."
  else
    echo "NOTE: run 'sudo loginctl enable-linger $USER' once so the agent survives logout and starts at boot."
  fi
  echo "Done. Logs: journalctl --user -u avalon-agent -f   Config: $ETC_DIR/agent.conf"
else
  echo "Done. Logs: journalctl -u avalon-agent -f   Config: $ETC_DIR/agent.conf"
fi
