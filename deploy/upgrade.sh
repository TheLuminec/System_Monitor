#!/usr/bin/env bash
# One-shot upgrade for AVALON: hub to the checked-out version, and (optionally) move AVALON's
# own agent from the root system unit to a rootless --user install so Claude prompts run as you.
#
#   sudo ./deploy/upgrade.sh                     # upgrade hub + keep current agent setup
#   sudo ./deploy/upgrade.sh --rootless-agent     # also migrate AVALON's agent to systemd --user (as $SUDO_USER)
#
# Other machines need nothing: their agents self-update from the hub on their next report.
set -euo pipefail
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
[[ $EUID -eq 0 ]] || { echo "run as root: sudo $0"; exit 1; }
TARGET_USER="${SUDO_USER:-}"
ROOTLESS=0
[[ "${1:-}" == "--rootless-agent" ]] && ROOTLESS=1

echo "=== 1/3 hub"
"$REPO_DIR/deploy/install-server.sh"

if [[ $ROOTLESS -eq 1 ]]; then
  [[ -n "$TARGET_USER" && "$TARGET_USER" != "root" ]] || { echo "--rootless-agent needs sudo from your own account (SUDO_USER)"; exit 1; }
  echo "=== 2/3 migrating AVALON's agent to a rootless install for $TARGET_USER"
  USER_HOME="$(getent passwd "$TARGET_USER" | cut -d: -f6)"
  CONF_SRC=/etc/avalon-agent/agent.conf
  CONF_DST="$USER_HOME/.config/avalon-agent/agent.conf"
  if [[ -f "$CONF_SRC" ]]; then
    sudo -u "$TARGET_USER" mkdir -p "$(dirname "$CONF_DST")"
    if [[ ! -f "$CONF_DST" ]]; then
      # carry the token + settings over; drop AVM_WATCH_UNITS=system:... lines that need root? (they work for user too via system scope)
      install -o "$TARGET_USER" -g "$(id -gn "$TARGET_USER")" -m 600 "$CONF_SRC" "$CONF_DST"
      echo "    copied $CONF_SRC -> $CONF_DST"
    fi
  else
    echo "    no $CONF_SRC found - the agent was never installed system-wide; continuing"
  fi
  if systemctl is-enabled avalon-agent.service >/dev/null 2>&1; then
    systemctl disable --now avalon-agent.service
    echo "    stopped + disabled the root avalon-agent.service"
  fi
  loginctl enable-linger "$TARGET_USER"
  # run the user installer inside the user's session so systemctl --user works
  sudo -u "$TARGET_USER" XDG_RUNTIME_DIR="/run/user/$(id -u "$TARGET_USER")" DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$(id -u "$TARGET_USER")/bus" \
    bash -c "cd '$REPO_DIR/agent' && ./install/install-linux.sh --user --upgrade"
  echo "    rootless agent installed; logs: journalctl --user -u avalon-agent -f   (as $TARGET_USER)"
else
  echo "=== 2/3 agent: left as-is (re-run with --rootless-agent to migrate AVALON's agent to systemd --user)"
fi

echo "=== 3/3 status"
sleep 2
avalon-monitor-manage agents || true
cat <<MSG

Done. Dashboard: http://$(tailscale ip -4 2>/dev/null | head -1 || echo '<avalon>'):$(grep -E '^AVM_BIND_PORT=' /etc/avalon-monitor/monitor.env 2>/dev/null | cut -d= -f2 || echo 8787)/
Hard-refresh the browser once (Ctrl+Shift+R). Other agents update themselves within one report interval.
MSG
