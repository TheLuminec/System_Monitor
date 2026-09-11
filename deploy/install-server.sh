#!/usr/bin/env bash
# Install / upgrade the Avalon Monitor hub on AVALON (Ubuntu/Mint/Debian).
#   sudo ./deploy/install-server.sh
# Idempotent: re-run after `git pull` to upgrade. Config lives in /etc/avalon-monitor/monitor.env.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_DIR=/opt/avalon-monitor
ETC_DIR=/etc/avalon-monitor
DATA_DIR=/var/lib/avalon-monitor
SVC_USER=avalon-monitor

[[ $EUID -eq 0 ]] || { echo "run as root: sudo $0"; exit 1; }
command -v python3 >/dev/null || { echo "python3 is required"; exit 1; }
python3 -c 'import venv' 2>/dev/null || { echo "python3-venv is required: apt install python3-venv"; exit 1; }

echo "==> service user"
id -u "$SVC_USER" >/dev/null 2>&1 || useradd --system --home-dir "$DATA_DIR" --shell /usr/sbin/nologin "$SVC_USER"

echo "==> copying code to $APP_DIR"
mkdir -p "$APP_DIR"
rsync -a --delete --exclude '.venv' --exclude '__pycache__' --exclude '*.db*' --exclude '.env' "$REPO_DIR/server/" "$APP_DIR/server/"
cp "$REPO_DIR/agent/avalon_agent.py" "$APP_DIR/"  # handy for running the hub's own agent

echo "==> python venv"
[[ -x "$APP_DIR/venv/bin/python" ]] || python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/venv/bin/pip" install --quiet -r "$APP_DIR/server/requirements.txt" psutil

echo "==> config + data dirs"
mkdir -p "$ETC_DIR" "$DATA_DIR"
if [[ ! -f "$ETC_DIR/monitor.env" ]]; then
  cp "$REPO_DIR/server/.env.example" "$ETC_DIR/monitor.env"
  echo "    created $ETC_DIR/monitor.env - edit it to add your Cloudflare Access settings"
fi
chown -R "$SVC_USER:$SVC_USER" "$DATA_DIR"
chmod 750 "$DATA_DIR"
chgrp "$SVC_USER" "$ETC_DIR/monitor.env"; chmod 640 "$ETC_DIR/monitor.env"

echo "==> systemd"
cp "$REPO_DIR/deploy/avalon-monitor.service" /etc/systemd/system/avalon-monitor.service
cat > /usr/local/bin/avalon-monitor-manage <<'WRAP'
#!/usr/bin/env bash
# Run the host-enrollment CLI with the service's environment and DB permissions.
exec sudo -u avalon-monitor env AVM_ENV_FILE=/etc/avalon-monitor/monitor.env \
  /opt/avalon-monitor/venv/bin/python /opt/avalon-monitor/server/manage.py "$@"
WRAP
chmod +x /usr/local/bin/avalon-monitor-manage
systemctl daemon-reload
systemctl enable --now avalon-monitor.service
systemctl restart avalon-monitor.service
sleep 1
systemctl --no-pager --lines=5 status avalon-monitor.service || true

PORT=$(grep -E '^AVM_BIND_PORT=' "$ETC_DIR/monitor.env" | cut -d= -f2)
PORT=${PORT:-8787}
TS_IP=$(tailscale ip -4 2>/dev/null | head -1 || true)
cat <<MSG

Avalon Monitor hub installed.
  Dashboard (tailnet):  http://${TS_IP:-<tailscale-ip>}:${PORT}/
  Enroll a host:        avalon-monitor-manage add-host <name>
  Logs:                 journalctl -u avalon-monitor -f
  Config:               $ETC_DIR/monitor.env  (then: systemctl restart avalon-monitor)

Next: docs/CLOUDFLARE.md to publish it at monitor.avalontech.xyz behind Cloudflare Access.
MSG
