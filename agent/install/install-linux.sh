#!/usr/bin/env bash
# Install the Avalon Monitor agent as a systemd service on any Linux (Debian/Ubuntu/Mint,
# Fedora, Arch, Raspberry Pi OS, ...).
#
#   sudo ./install-linux.sh --server http://avalon:8787 --token avm_xxx [--name miami] [--interval 5]
#   sudo ./install-linux.sh --upgrade        # re-copy the script, keep config
#
# Copy this directory (agent/) to the target machine first, e.g.
#   scp -r agent/ user@miami:~/avalon-agent && ssh user@miami 'sudo ~/avalon-agent/install/install-linux.sh --server ... --token ...'
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_DIR=/opt/avalon-agent
ETC_DIR=/etc/avalon-agent
SERVER="" TOKEN="" NAME="" INTERVAL="" UPGRADE=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --server) SERVER="$2"; shift 2;;
    --token) TOKEN="$2"; shift 2;;
    --name) NAME="$2"; shift 2;;
    --interval) INTERVAL="$2"; shift 2;;
    --upgrade) UPGRADE=1; shift;;
    -h|--help) sed -n '2,12p' "$0"; exit 0;;
    *) echo "unknown option $1"; exit 1;;
  esac
done

[[ $EUID -eq 0 ]] || { echo "run as root: sudo $0 ..."; exit 1; }
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

echo "==> systemd"
cp "$SRC_DIR/install/avalon-agent.service" /etc/systemd/system/avalon-agent.service
systemctl daemon-reload
systemctl enable --now avalon-agent.service
systemctl restart avalon-agent.service
sleep 1
systemctl --no-pager --lines=3 status avalon-agent.service || true
echo
echo "Done. Logs: journalctl -u avalon-agent -f   Config: $ETC_DIR/agent.conf"
