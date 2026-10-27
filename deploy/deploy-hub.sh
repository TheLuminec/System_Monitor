#!/usr/bin/env bash
# Deploy a committed hub version WITHOUT sudo, for routine code-only releases.
#
#   deploy/deploy-hub.sh [--force] [--skip-tests] <git-ref>   deploy that commit (branch, tag or sha) of this repo
#   deploy/deploy-hub.sh --rollback    put back what was live before the last deploy
#   deploy/deploy-hub.sh --status      show what is deployed
#
# Only ships a commit that is on origin/main (fetched first), whose tests/run.sh passes (run on that exact commit;
# --skip-tests skips it, loudly), and whose __version__ is not lower than the deployed one (--force allows a
# deliberate downgrade). Copies server/ from the commit (via `git archive`, so only committed code ships) into
# /opt/avalon-monitor, restarts the service (allowed by deploy/polkit/50-avalon-monitor-deploy.rules), waits for
# /healthz to report the new version and smoke-tests /, /api/v1/hosts and /api/v1/stats. If it does not come back,
# the previous code is restored and restarted automatically. agent/ (which every host self-updates from) is put in
# place only after that, so a release that gets rolled back never reaches the fleet.
#
# Refuses, and tells you to run `sudo ./deploy/upgrade.sh` instead, whenever a release needs root:
# changed Python dependencies, a changed systemd unit, or code dirs it cannot write.
# agent/ is deployed only if /opt/avalon-monitor/agent is writable by you: chown it to root to keep
# agent releases (code every enrolled host runs) behind sudo.
#
# Test hooks (tests/test_deploy_hub.py): AVM_DEPLOY_ROOT, AVM_DEPLOY_SERVICE, AVM_DEPLOY_UNIT_DIR,
# AVM_DEPLOY_HEALTH_URL, AVM_DEPLOY_HEALTH_TRIES, AVM_TEST_PYTHON.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_DIR="${AVM_DEPLOY_ROOT:-/opt/avalon-monitor}"
UNIT="${AVM_DEPLOY_SERVICE:-avalon-monitor}.service"
UNIT_FILE="${AVM_DEPLOY_UNIT_DIR:-/etc/systemd/system}/$UNIT"
HUB="${AVM_DEPLOY_HEALTH_URL:-http://127.0.0.1:${AVM_DEPLOY_PORT:-8787}}"; HUB="${HUB%/}"
TRIES="${AVM_DEPLOY_HEALTH_TRIES:-30}"
TEST_PY="${AVM_TEST_PYTHON:-$REPO_DIR/.venv/bin/python}"
STATE="${XDG_STATE_HOME:-$HOME/.local/state}/avalon-monitor-deploy"
EXCLUDES=(--exclude .venv --exclude __pycache__ --exclude '*.db*' --exclude .env)
mkdir -p "$STATE"

die() { echo "deploy: $*" >&2; exit 1; }
log() { echo "$(date -Is) $*" >> "$STATE/deploy.log"; logger -t avalon-monitor-deploy -- "$*" 2>/dev/null || true; }

version_in() { sed -nE 's/^__version__ = "([^"]+)".*/\1/p' "$1/server/avalon_monitor/__init__.py" 2>/dev/null || true; }
version_lt() { [[ "$1" != "$2" ]] && printf '%s\n%s\n' "$1" "$2" | sort -V -C; }  # $1 < $2

wait_healthy() {  # $1 = expected version
  local i body
  for i in $(seq 1 "$TRIES"); do
    body=$(curl -fsS -m 2 "$HUB/healthz" 2>/dev/null || true)
    if [[ -n "$body" && "$body" == *"\"version\":\"$1\""* ]]; then return 0; fi
    sleep 1
  done
  return 1
}

json_check() {  # $1 = file, $2 = hosts|stats: the shape the dashboard relies on
  python3 - "$1" "$2" 2>/dev/null <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
ok = isinstance(d.get("hosts"), list) if sys.argv[2] == "hosts" else isinstance(d.get("raw_rows"), int)
sys.exit(0 if ok else 1)
PY
}

smoke() {  # /healthz touches neither the DB nor auth nor the UI: read what a browser reads (loopback = viewer)
  local f="$STAGE.smoke" ct
  ct=$(curl -fsS -m 5 -o "$f" -w '%{content_type}' "$HUB/" 2>/dev/null) && [[ "$ct" == text/html* ]] \
    || { echo "deploy: smoke test: GET / did not return the dashboard page (${ct:-error})" >&2; return 1; }
  curl -fsS -m 5 -o "$f" "$HUB/api/v1/hosts" 2>/dev/null && json_check "$f" hosts \
    || { echo "deploy: smoke test: GET /api/v1/hosts did not return the host list" >&2; return 1; }
  curl -fsS -m 5 -o "$f" "$HUB/api/v1/stats" 2>/dev/null && json_check "$f" stats \
    || { echo "deploy: smoke test: GET /api/v1/stats failed (database not reachable?)" >&2; return 1; }
}

restart() {
  # --no-ask-password: fail fast instead of prompting when the polkit rule is missing
  systemctl --no-ask-password restart "$UNIT"
}

sync_dir() {  # $1 = source root, $2 = server|agent
  if [[ "$2" == server ]]; then rsync -a --delete "${EXCLUDES[@]}" "$1/server/" "$APP_DIR/server/"
  else rsync -a --delete --exclude __pycache__ "$1/agent/" "$APP_DIR/agent/"; fi
}

restore() {  # $1 = server|agent: put the copy taken before the deploy back
  sync_dir "$STATE/rollback" "$1" \
    || die "restoring $APP_DIR/$1 failed - it may be a MIX of old and new code. Fix by hand from $STATE/rollback/$1 before the hub restarts"
}

put() {  # $1 = source root, $2 = server|agent; a copy that fails part-way is undone, never left half-old half-new
  sync_dir "$1" "$2" && return 0
  echo "deploy: copying $2/ failed part-way - restoring the previous $2/" >&2
  restore "$2"
  return 1
}

case "${1:-}" in
  --status)
    echo "live:    $(curl -fsS -m 2 "$HUB/healthz" 2>/dev/null || echo unreachable)"
    echo "commit:  $(cat "$STATE/current" 2>/dev/null || echo 'unknown (last deployed with sudo upgrade.sh?)')"
    echo "log:     $STATE/deploy.log"; tail -n 5 "$STATE/deploy.log" 2>/dev/null || true
    exit 0 ;;
  --rollback)
    [[ -d "$STATE/rollback/server" ]] || die "no rollback copy in $STATE/rollback"
    # agent/ goes back too: hosts must not stay on an agent newer than the hub that serves it
    restore server
    [[ -d "$STATE/rollback/agent" && -w "$APP_DIR/agent" ]] && restore agent
    restart || die "restart refused - is the polkit rule installed? (see deploy/polkit/)"
    want=$(version_in "$STATE/rollback")
    wait_healthy "$want" || die "hub did not come back healthy after rollback - check: journalctl -u $UNIT -n 50"
    cp "$STATE/rollback.commit" "$STATE/current" 2>/dev/null || true
    log "rollback to $(cat "$STATE/current" 2>/dev/null || echo '?') (v$want)"
    echo "rolled back; hub v$want is live"; exit 0 ;;
  ""|-h|--help)
    awk 'NR > 1 && !/^#/ { exit } NR > 1 { sub(/^# ?/, ""); print }' "$0"; exit 0 ;;
esac

FORCE=no; SKIP_TESTS=no; REF=""
for arg in "$@"; do
  case "$arg" in
    --force) FORCE=yes ;;
    --skip-tests) SKIP_TESTS=yes ;;
    -*) die "unknown option: $arg (see --help)" ;;
    *) [[ -z "$REF" ]] || die "one ref only, got: $REF $arg"; REF="$arg" ;;
  esac
done
[[ -n "$REF" ]] || die "no ref given (see --help)"
[[ "$REF" =~ ^[A-Za-z0-9._/@^~-]+$ ]] || die "bad ref: $REF"

# --- only pushed history ships: a stale local main or an unpushed branch is refused (fetch first, so origin/... is current)
git -C "$REPO_DIR" fetch --quiet origin 2>/dev/null \
  || echo "deploy: warning: cannot fetch origin - checking against the last fetched origin/main" >&2
COMMIT=$(git -C "$REPO_DIR" rev-parse --verify --quiet "$REF^{commit}") || die "no such commit: $REF"
SHORT=${COMMIT:0:7}
git -C "$REPO_DIR" rev-parse --verify --quiet "origin/main^{commit}" >/dev/null || die "no origin/main in $REPO_DIR to check $SHORT against"
git -C "$REPO_DIR" merge-base --is-ancestor "$COMMIT" origin/main \
  || die "$SHORT ($REF) is not on origin/main - merge and push it first (unpushed or side branches are never deployed)"

# --- preflight: anything that needs root goes through sudo ./deploy/upgrade.sh
[[ -w "$APP_DIR/server" ]] || die "$APP_DIR/server is not writable by $(id -un) - use: sudo ./deploy/upgrade.sh"
agent=no; [[ -w "$APP_DIR/agent" ]] && agent=yes

STAGE=$(mktemp -d "$STATE/stage.XXXXXX")
trap 'rm -rf "$STAGE" "$STAGE.smoke"' EXIT
git -C "$REPO_DIR" archive --format=tar "$COMMIT" | tar -x -C "$STAGE"   # the whole tree: tests/run.sh needs it

cmp -s "$STAGE/server/requirements.txt" "$APP_DIR/server/requirements.txt" \
  || die "$SHORT changes server/requirements.txt (new dependencies need root) - use: sudo ./deploy/upgrade.sh"
cmp -s "$STAGE/deploy/avalon-monitor.service" "$UNIT_FILE" \
  || die "$SHORT changes the systemd unit - use: sudo ./deploy/upgrade.sh"
# --strip-trailing-cr: .gitattributes gives *.ps1 CRLF in archives, the sudo install copied LF
if [[ "$agent" == no ]] && ! diff -rq --strip-trailing-cr -x __pycache__ "$STAGE/agent" "$APP_DIR/agent" >/dev/null 2>&1; then
  echo "deploy: note: $SHORT changes agent/, which is root-owned here - deploying the hub only; agent needs sudo ./deploy/upgrade.sh"
fi
python3 -m compileall -q "$STAGE/server" "$STAGE/agent" >/dev/null || die "$SHORT does not compile"
WANT=$(version_in "$STAGE"); [[ -n "$WANT" ]] || die "cannot read __version__ from $SHORT"
HAVE=$(version_in "$APP_DIR")
if [[ -z "$HAVE" ]]; then
  echo "deploy: note: cannot read the deployed __version__ in $APP_DIR - downgrade check skipped" >&2
elif version_lt "$WANT" "$HAVE"; then
  # every host would then self-update DOWN to this commit's agent too
  [[ "$FORCE" == yes ]] || die "$SHORT ($REF) is v$WANT, older than the deployed v$HAVE - refusing a downgrade (stale local branch?). Use --rollback, or --force if you mean it"
  echo "deploy: warning: --force: downgrading v$HAVE -> v$WANT" >&2
elif [[ "$HAVE" == "$WANT" ]]; then
  echo "deploy: note: $SHORT has the same __version__ ($WANT) as the deployed hub - bump it so browsers drop cached JS"
fi

# --- the test suite, on exactly the tree that ships
if [[ "$SKIP_TESTS" == yes ]]; then
  echo "deploy: WARNING: --skip-tests: deploying $SHORT WITHOUT running the test suite" >&2
  log "skip-tests requested for $SHORT ($REF) by $(id -un)"
else
  [[ -f "$STAGE/tests/run.sh" ]] || die "$SHORT has no tests/run.sh - re-run with --skip-tests if you must deploy it"
  [[ -x "$TEST_PY" ]] || die "no Python for the test suite at $TEST_PY (create $REPO_DIR/.venv) - or re-run with --skip-tests"
  echo "deploy: running tests/run.sh on $SHORT ..."
  if ! AVM_TEST_PYTHON="$TEST_PY" bash "$STAGE/tests/run.sh" > "$STATE/tests.log" 2>&1; then
    tail -n 40 "$STATE/tests.log" >&2
    die "the test suite failed on $SHORT - not deploying (full output: $STATE/tests.log)"
  fi
fi

# --- keep what is live now
rm -rf "$STATE/rollback"; mkdir -p "$STATE/rollback"
rsync -a "${EXCLUDES[@]}" "$APP_DIR/server/" "$STATE/rollback/server/"
[[ "$agent" == yes ]] && rsync -a --exclude __pycache__ "$APP_DIR/agent/" "$STATE/rollback/agent/"
cp "$STATE/current" "$STATE/rollback.commit" 2>/dev/null || echo unknown > "$STATE/rollback.commit"

# --- hub first: the old process keeps running from memory until the restart
put "$STAGE" server || die "copying server/ failed; previous code restored, live hub untouched"
if ! restart; then
  restore server
  die "restart refused - is the polkit rule installed? (deploy/polkit/). Code on disk restored; live hub untouched."
fi
if ! wait_healthy "$WANT" || ! smoke; then
  echo "deploy: v$WANT did not become healthy - rolling back (agent/ was not touched)" >&2
  restore server
  restart || true
  log "FAILED $SHORT ($REF, v$WANT) - rolled back"
  wait_healthy "$(version_in "$STATE/rollback")" && die "rolled back; previous version is live. Logs: journalctl -u $UNIT -n 50"
  die "rollback did not come back healthy either - needs a human: journalctl -u $UNIT -n 50"
fi
echo "$COMMIT" > "$STATE/current"

# --- only now agent/: the hub serves it to every host on its next report, so it must never get ahead of a hub
#     that may still be rolled back
if [[ "$agent" == yes ]] && ! put "$STAGE" agent; then
  log "deployed $SHORT ($REF) as v$WANT by $(id -un), but agent/ FAILED to copy (previous agent restored)"
  die "hub v$WANT is live, but copying agent/ failed - the previous agent was restored. Re-run, or sudo ./deploy/upgrade.sh"
fi
log "deployed $SHORT ($REF) as v$WANT by $(id -un)$([[ $agent == yes ]] && echo ' incl. agent' || echo ', hub only')"
echo "deployed $SHORT ($REF): hub v$WANT is live$([[ $agent == yes ]] && echo ' (agent/ updated too: hosts self-update on their next report)')"
