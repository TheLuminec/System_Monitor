#!/usr/bin/env bash
# Deploy a committed hub version WITHOUT sudo, for routine code-only releases.
#
#   deploy/deploy-hub.sh <git-ref>     deploy that commit (branch, tag or sha) of this repo
#   deploy/deploy-hub.sh --rollback    put back what was live before the last deploy
#   deploy/deploy-hub.sh --status      show what is deployed
#
# Copies server/ and agent/ from the commit (via `git archive`, so only committed code ships)
# into /opt/avalon-monitor, restarts the service (allowed by deploy/polkit/50-avalon-monitor-deploy.rules),
# and waits for /healthz to report the new version. If it does not come back, the previous code is
# restored and restarted automatically.
#
# Refuses, and tells you to run `sudo ./deploy/upgrade.sh` instead, whenever a release needs root:
# changed Python dependencies, a changed systemd unit, or code dirs it cannot write.
# agent/ is deployed only if /opt/avalon-monitor/agent is writable by you: chown it to root to keep
# agent releases (code every enrolled host runs) behind sudo.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_DIR=/opt/avalon-monitor
UNIT=avalon-monitor.service
PORT="${AVM_DEPLOY_PORT:-8787}"
STATE="${XDG_STATE_HOME:-$HOME/.local/state}/avalon-monitor-deploy"
EXCLUDES=(--exclude .venv --exclude __pycache__ --exclude '*.db*' --exclude .env)
mkdir -p "$STATE"

die() { echo "deploy: $*" >&2; exit 1; }
log() { echo "$(date -Is) $*" >> "$STATE/deploy.log"; logger -t avalon-monitor-deploy -- "$*" 2>/dev/null || true; }

version_in() { sed -nE 's/^__version__ = "([^"]+)".*/\1/p' "$1/server/avalon_monitor/__init__.py"; }

wait_healthy() {  # $1 = expected version
  local i body
  for i in $(seq 1 30); do
    body=$(curl -fsS -m 2 "http://127.0.0.1:$PORT/healthz" 2>/dev/null || true)
    if [[ -n "$body" && "$body" == *"\"version\":\"$1\""* ]]; then return 0; fi
    sleep 1
  done
  return 1
}

restart() {
  # --no-ask-password: fail fast instead of prompting when the polkit rule is missing
  systemctl --no-ask-password restart "$UNIT"
}

sync_dirs() {  # $1 = source root containing server/ (and agent/ when $2 = yes)
  rsync -a --delete "${EXCLUDES[@]}" "$1/server/" "$APP_DIR/server/"
  if [[ "$2" == yes ]]; then rsync -a --delete --exclude __pycache__ "$1/agent/" "$APP_DIR/agent/"; fi
}

case "${1:-}" in
  --status)
    echo "live:    $(curl -fsS -m 2 "http://127.0.0.1:$PORT/healthz" 2>/dev/null || echo unreachable)"
    echo "commit:  $(cat "$STATE/current" 2>/dev/null || echo 'unknown (last deployed with sudo upgrade.sh?)')"
    echo "log:     $STATE/deploy.log"; tail -n 5 "$STATE/deploy.log" 2>/dev/null || true
    exit 0 ;;
  --rollback)
    [[ -d "$STATE/rollback/server" ]] || die "no rollback copy in $STATE/rollback"
    agent=no; [[ -d "$STATE/rollback/agent" && -w "$APP_DIR/agent" ]] && agent=yes
    sync_dirs "$STATE/rollback" "$agent"
    restart || die "restart refused - is the polkit rule installed? (see deploy/polkit/)"
    want=$(version_in "$STATE/rollback")
    wait_healthy "$want" || die "hub did not come back healthy after rollback - check: journalctl -u $UNIT -n 50"
    cp "$STATE/rollback.commit" "$STATE/current" 2>/dev/null || true
    log "rollback to $(cat "$STATE/current" 2>/dev/null || echo '?') (v$want)"
    echo "rolled back; hub v$want is live"; exit 0 ;;
  ""|-h|--help)
    sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
esac

REF="$1"
[[ "$REF" =~ ^[A-Za-z0-9._/@^~-]+$ ]] || die "bad ref: $REF"
COMMIT=$(git -C "$REPO_DIR" rev-parse --verify --quiet "$REF^{commit}") || die "no such commit: $REF"
SHORT=${COMMIT:0:7}

# --- preflight: anything that needs root goes through sudo ./deploy/upgrade.sh
[[ -w "$APP_DIR/server" ]] || die "$APP_DIR/server is not writable by $(id -un) - use: sudo ./deploy/upgrade.sh"
agent=no; [[ -w "$APP_DIR/agent" ]] && agent=yes

STAGE=$(mktemp -d "$STATE/stage.XXXXXX")
trap 'rm -rf "$STAGE"' EXIT
git -C "$REPO_DIR" archive --format=tar "$COMMIT" server agent deploy/avalon-monitor.service | tar -x -C "$STAGE"

cmp -s "$STAGE/server/requirements.txt" "$APP_DIR/server/requirements.txt" \
  || die "$SHORT changes server/requirements.txt (new dependencies need root) - use: sudo ./deploy/upgrade.sh"
cmp -s "$STAGE/deploy/avalon-monitor.service" /etc/systemd/system/$UNIT \
  || die "$SHORT changes the systemd unit - use: sudo ./deploy/upgrade.sh"
# --strip-trailing-cr: .gitattributes gives *.ps1 CRLF in archives, the sudo install copied LF
if [[ "$agent" == no ]] && ! diff -rq --strip-trailing-cr -x __pycache__ "$STAGE/agent" "$APP_DIR/agent" >/dev/null 2>&1; then
  echo "deploy: note: $SHORT changes agent/, which is root-owned here - deploying the hub only; agent needs sudo ./deploy/upgrade.sh"
fi
python3 -m compileall -q "$STAGE/server" "$STAGE/agent" >/dev/null || die "$SHORT does not compile"
WANT=$(version_in "$STAGE"); [[ -n "$WANT" ]] || die "cannot read __version__ from $SHORT"
HAVE=$(curl -fsS -m 2 "http://127.0.0.1:$PORT/healthz" 2>/dev/null | sed -nE 's/.*"version":"([^"]+)".*/\1/p')
if [[ -n "$HAVE" && "$HAVE" == "$WANT" ]]; then
  echo "deploy: note: $SHORT has the same __version__ ($WANT) as the live hub - bump it so browsers drop cached JS"
fi

# --- keep what is live now, then swap and restart
rm -rf "$STATE/rollback"; mkdir -p "$STATE/rollback"
rsync -a "${EXCLUDES[@]}" "$APP_DIR/server/" "$STATE/rollback/server/"
[[ "$agent" == yes ]] && rsync -a --exclude __pycache__ "$APP_DIR/agent/" "$STATE/rollback/agent/"
cp "$STATE/current" "$STATE/rollback.commit" 2>/dev/null || echo unknown > "$STATE/rollback.commit"

sync_dirs "$STAGE" "$agent"
if ! restart; then
  sync_dirs "$STATE/rollback" "$agent"
  die "restart refused - is the polkit rule installed? (deploy/polkit/). Code on disk restored; live hub untouched."
fi
if ! wait_healthy "$WANT"; then
  echo "deploy: v$WANT did not become healthy - rolling back" >&2
  sync_dirs "$STATE/rollback" "$agent"
  restart || true
  log "FAILED $SHORT ($REF, v$WANT) - rolled back"
  wait_healthy "$(version_in "$STATE/rollback")" && die "rolled back; previous version is live. Logs: journalctl -u $UNIT -n 50"
  die "rollback did not come back healthy either - needs a human: journalctl -u $UNIT -n 50"
fi
echo "$COMMIT" > "$STATE/current"
log "deployed $SHORT ($REF) as v$WANT by $(id -un)$([[ $agent == yes ]] && echo ' incl. agent' || echo ', hub only')"
echo "deployed $SHORT ($REF): hub v$WANT is live$([[ $agent == yes ]] && echo ' (agent/ updated too: hosts self-update on their next report)')"
