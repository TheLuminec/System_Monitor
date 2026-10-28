#!/usr/bin/env bash
# Deploy a committed hub version WITHOUT sudo, for routine code-only releases.
#
#   deploy/deploy-hub.sh [--force] [--skip-tests] <git-ref>   deploy that commit (branch, tag or sha) of this repo
#   deploy/deploy-hub.sh [--force] --rollback    put back what was live before the last deploy
#   deploy/deploy-hub.sh --status      show what is deployed
#   deploy/deploy-hub.sh --record-install   (run by sudo ./deploy/upgrade.sh) record the commit that install put live
#
# Only ships a commit that is on origin/main (fetched first), whose tests/run.sh passes (run on that exact commit;
# --skip-tests skips its pytest run, loudly - the static checks still run), and whose __version__ is not lower than
# the deployed one (--force allows a deliberate downgrade). Copies server/ from the commit (via `git archive`, so only
# committed code ships) into /opt/avalon-monitor, restarts the service (allowed by
# deploy/polkit/50-avalon-monitor-deploy.rules), waits for /healthz to report the new version and smoke-tests /,
# /api/v1/hosts and /api/v1/stats. If it does not come back, the previous code is restored and restarted
# automatically. agent/ (which every host self-updates from) is put in place only after that, so a release that gets
# rolled back never reaches the fleet.
#
# --rollback restores the copy taken before the last successful deploy, and only while that deploy is what is live
# (its commit in $STATE/current, its version on disk and on /healthz); --force restores it anyway. A deploy that
# fails, or is interrupted (Ctrl-C, a dropped ssh: the previous code is put back as after a failed health check), keeps
# the previous rollback copy, and so does redeploying the commit that is already live.
#
# Refuses, and tells you to run `sudo ./deploy/upgrade.sh` instead, whenever a release needs root:
# changed Python dependencies, a changed systemd unit, or code dirs it cannot write.
# agent/ is deployed only if /opt/avalon-monitor/agent is writable by you: chown it to root to keep
# agent releases (code every enrolled host runs) behind sudo.
#
# Test hooks (tests/test_deploy_hub.py): AVM_DEPLOY_ROOT, AVM_DEPLOY_SERVICE, AVM_DEPLOY_UNIT_DIR,
# AVM_DEPLOY_HEALTH_URL, AVM_DEPLOY_HEALTH_TRIES, AVM_DEPLOY_FETCH_TIMEOUT, AVM_DEPLOY_TESTS_TIMEOUT,
# AVM_DEPLOY_BACKUP_AVM, AVM_DEPLOY_BACKUP_CONF, AVM_DEPLOY_BACKUP_SBIN, AVM_TEST_PYTHON.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_DIR="${AVM_DEPLOY_ROOT:-/opt/avalon-monitor}"
UNIT="${AVM_DEPLOY_SERVICE:-avalon-monitor}.service"
UNIT_FILE="${AVM_DEPLOY_UNIT_DIR:-/etc/systemd/system}/$UNIT"
HUB="${AVM_DEPLOY_HEALTH_URL:-http://127.0.0.1:${AVM_DEPLOY_PORT:-8787}}"; HUB="${HUB%/}"
TRIES="${AVM_DEPLOY_HEALTH_TRIES:-30}"
FETCH_TIMEOUT="${AVM_DEPLOY_FETCH_TIMEOUT:-30}"
TESTS_TIMEOUT="${AVM_DEPLOY_TESTS_TIMEOUT:-600}"   # per attempt; the whole suite takes ~1-2 min on AVALON
# the root backup service's own copy of avm (deploy/backup/install-restic-backup.sh); deploys cannot refresh it
BACKUP_AVM="${AVM_DEPLOY_BACKUP_AVM:-/usr/local/lib/avalon-backup/avm.py}"
BACKUP_CONF="${AVM_DEPLOY_BACKUP_CONF:-/etc/avalon-backup/config.json}"   # its "unit" names the runner, <sbin>/<unit>
BACKUP_SBIN="${AVM_DEPLOY_BACKUP_SBIN:-/usr/local/sbin}"
TEST_PY="${AVM_TEST_PYTHON:-$REPO_DIR/.venv/bin/python}"
STATE="${XDG_STATE_HOME:-$HOME/.local/state}/avalon-monitor-deploy"
SNAP="$STATE/rollback"   # the copy restore() puts back; a deploy takes its own (rollback.new) and promotes it on success
EXCLUDES=(--exclude .venv --exclude __pycache__ --exclude '*.db*' --exclude .env)
mkdir -p "$STATE"

die() { echo "deploy: $*" >&2; exit 1; }
log() { echo "$(date -Is) $*" >> "$STATE/deploy.log"; logger -t avalon-monitor-deploy -- "$*" 2>/dev/null || true; }

version_in() { sed -nE 's/^__version__ = "([^"]+)".*/\1/p' "$1/server/avalon_monitor/__init__.py" 2>/dev/null || true; }
version_lt() { [[ "$1" != "$2" ]] && printf '%s\n%s\n' "$1" "$2" | sort -V -C; }  # $1 < $2
avm_version() { sed -nE 's/^AVM_VERSION = "([^"]+)".*/\1/p' "$1" 2>/dev/null || true; }
agent_version() { sed -nE 's/^AGENT_VERSION = "([^"]+)".*/\1/p' "$1/agent/avalon_agent.py" 2>/dev/null || true; }
live_version() { curl -fsS -m 2 "$HUB/healthz" 2>/dev/null | sed -nE 's/.*"version":"([^"]+)".*/\1/p' || true; }
meta() { sed -nE "s/^$1=//p" "$SNAP/meta" 2>/dev/null || true; }  # a field of the rollback copy's provenance record

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
  local f rc=0
  f=$(mktemp "$STATE/smoke.XXXXXX")
  smoke_get "$f" || rc=$?
  rm -f "$f"
  return "$rc"
}

smoke_get() {  # $1 = scratch file
  local f=$1 ct
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
  # agent/: the *.sig files first. One rsync renames files in name order, so avalon_agent.py went live before its
  # .sig and a host fetching in between got new code with the old signature (refused, but noise - F-22). This way the
  # window serves the old code with the new .sig, which a host already running that old code never downloads.
  # '*.sig*': .sig2 too, the only signature agents 1.10+ read (I-08)
  else rsync -a -m --include '*/' --include '*.sig*' --exclude '*' "$1/agent/" "$APP_DIR/agent/" \
         && rsync -a --delete --exclude __pycache__ "$1/agent/" "$APP_DIR/agent/"; fi
}

restore() {  # $1 = server|agent: put the copy taken before the deploy back
  sync_dir "$SNAP" "$1" \
    || die "restoring $APP_DIR/$1 failed - it may be a MIX of old and new code. Fix by hand from $SNAP/$1 before the hub restarts"
}

put() {  # $1 = source root, $2 = server|agent; a copy that fails part-way is undone, never left half-old half-new
  sync_dir "$1" "$2" && return 0
  echo "deploy: copying $2/ failed part-way - restoring the previous $2/" >&2
  restore "$2"
  return 1
}

rollback() {
  [[ -d "$SNAP/server" ]] || die "no rollback copy in $SNAP"
  # F-08: the copy is the way back from ONE deploy. Restoring it over anything else (a later sudo upgrade.sh, a deploy
  # that was already rolled back) would put back a release nobody chose.
  local undoes uver cur disk live want problem=""
  undoes=$(meta for_commit); uver=$(meta for_version); want=$(version_in "$SNAP")
  cur=$(cat "$STATE/current" 2>/dev/null || echo unknown); disk=$(version_in "$APP_DIR"); live=$(live_version)
  # I-05: a copy of the very release it undoes (an older deploy-hub.sh took one when the live commit was redeployed)
  # would "roll back" to what is live; --force cannot make that useful
  [[ -z "$undoes" || "$(meta commit)" != "$undoes" ]] \
    || die "refusing --rollback: the rollback copy holds ${undoes:0:7} (v$want), the same commit it undoes - it cannot go back further. Deploy the commit you want instead"
  if [[ -z "$undoes" ]]; then problem="it does not record which deploy it undoes (taken by an older deploy-hub.sh)"
  elif [[ "$cur" != "$undoes" ]]; then problem="it undoes ${undoes:0:7} (v$uver), but the last deploy is ${cur:0:7} (already rolled back?)"
  elif [[ "$disk" != "$uver" ]]; then problem="it undoes v$uver, but $APP_DIR has v${disk:-?} (sudo upgrade.sh since?)"
  elif [[ -n "$live" && "$live" != "$uver" ]]; then problem="it undoes v$uver, but the live hub reports v$live"
  fi   # an unreachable hub is no reason to refuse: that is when a rollback is wanted most
  if [[ -n "$problem" ]]; then
    [[ "$FORCE" == yes ]] || die "refusing --rollback to v$want: the rollback copy does not belong to what is live now - $problem. Use --rollback --force to restore it anyway"
    echo "deploy: warning: --force: rolling back although $problem" >&2
  fi
  # the same order as a deploy (E-13, F-09): hub first, agent/ only once that hub is healthy
  restore server
  restart || die "restart refused - is the polkit rule installed? (see deploy/polkit/)"
  wait_healthy "$want" || die "hub did not come back healthy after rollback (agent/ not restored) - check: journalctl -u $UNIT -n 50; once fixed, --rollback --force finishes it"
  # I-17: the same smoke test as a deploy - /healthz alone touches neither the DB nor the UI
  smoke || die "hub v$want is back on /healthz but fails the smoke test (agent/ not restored) - check: journalctl -u $UNIT -n 50; once fixed, --rollback --force finishes it"
  # agent/ goes back too: hosts must not stay on an agent newer than the hub that serves it
  local anew aold
  anew=$(agent_version "$APP_DIR"); aold=$(agent_version "$SNAP")
  if [[ -d "$SNAP/agent" && -w "$APP_DIR/agent" ]]; then restore agent; else aold=""; fi
  meta commit > "$STATE/current"; [[ -s "$STATE/current" ]] || echo unknown > "$STATE/current"
  log "rollback to $(cat "$STATE/current") (v$want) by $(id -un)${problem:+ (--force: $problem)}"
  echo "rolled back; hub v$want is live"
  # I-14: agents 1.10+ refuse a release older than the one they run (F-12), so the fleet stays on the newer agent
  if [[ -n "$aold" && -n "$anew" ]] && version_lt "$aold" "$anew"; then
    echo "note: agent/ is back at $aold, but hosts already on agent $anew (1.10+) will NOT follow it down: they refuse an older release and show a self-update error until a release above $anew ships, or a host's own agent.conf sets AVM_ALLOW_DOWNGRADE=1"
  fi
}

run_suite() {  # $1 = a label for the log; rest = env assignments. tests/run.sh with a time limit, output to tests.log
  local label=$1 rc=0; shift
  echo "=== $(date -Is) $label" >> "$STATE/tests.log"
  # timeout: a hung test must not hold the deploy (or the terminal) forever (F-11); -k: then KILL what ignores TERM
  env "$@" AVM_TEST_PYTHON="$TEST_PY" timeout -k 10 "$TESTS_TIMEOUT" bash "$STAGE/tests/run.sh" >> "$STATE/tests.log" 2>&1 || rc=$?
  ((rc != 124 && rc != 137)) || echo "=== timed out after ${TESTS_TIMEOUT}s" >> "$STATE/tests.log"
  return "$rc"
}

retry_failed_pytest() {  # one more go at the files of the tests that failed, when nothing else failed
  tail -n 1 "$STATE/tests.log" | grep -qx 'tests/run.sh: 1 check(s) FAILED: pytest' || return 1
  local failed f ids=() files=() rc=0
  failed=$(grep -E '^(FAILED|ERROR) ' "$STATE/tests.log" | cut -d' ' -f2 | sort -u | tr '\n' ' ' || true)
  # I-07: whole files, in their own order - a failed test rerun alone passes when it failed only because of what an
  # earlier test in its file did (shared module state), and that is a real bug, not a flaky one
  read -ra ids <<< "$failed" || true
  for f in "${ids[@]}"; do
    f=${f%%::*}
    if [[ "$f" =~ ^tests/[A-Za-z0-9_./-]+\.py$ && "$f" != *..* && -f "$STAGE/$f" && " ${files[*]} " != *" $f "* ]]; then files+=("$f"); fi
  done
  ((${#files[@]})) || { echo "deploy: pytest failed but the log names no failed test file - not retrying" >&2; return 1; }
  echo "deploy: pytest failed (${failed% }) - retrying the files with failed tests once: ${files[*]} ..." >&2
  echo "=== $(date -Is) retry of the files with failed tests: ${files[*]} (failed: ${failed% })" >> "$STATE/tests.log"
  (cd "$STAGE" && timeout -k 10 "$TESTS_TIMEOUT" "$TEST_PY" -m pytest "${files[@]}" -q -p no:warnings) \
    >> "$STATE/tests.log" 2>&1 || rc=$?
  if ((rc == 0)); then
    FLAKY="pytest failed on $SHORT (${failed% }), a rerun of ${files[*]} passed (both in $STATE/tests.log)"
    log "flaky: $FLAKY"
  fi
  return "$rc"
}

on_signal() {  # $1 = the signal. I-06: an interrupt between `put server` and a passed smoke test undoes the deploy
  # exactly as a failed health check does: the previous server/ back (and restarted, if the new one was); current and
  # the rollback copy untouched
  trap '' INT TERM HUP   # a second Ctrl-C must not stop the restore half-way
  echo "deploy: interrupted ($1) - putting the previous server/ back (agent/ was not touched)" >&2
  restore server
  log "INTERRUPTED ($1) $SHORT ($REF, v$WANT) - rolled back"
  [[ "$RESTARTED" == yes ]] || die "interrupted before the restart; previous code restored, live hub untouched"
  restart || true
  wait_healthy "$(version_in "$SNAP")" && die "interrupted; rolled back, previous version is live"
  die "interrupted, and the rollback did not come back healthy either - needs a human: journalctl -u $UNIT -n 50"
}

record_install() {  # I-15: sudo upgrade.sh (install-server.sh) copied this checkout's working tree live
  local head="" disk
  disk=$(version_in "$APP_DIR")
  # only a clean tree is a commit; and only if the version on disk is this tree's (the install did go through)
  if [[ -z "$(git -C "$REPO_DIR" status --porcelain -- server agent deploy/avalon-monitor.service 2>/dev/null || echo dirty)" \
        && -n "$disk" && "$disk" == "$(version_in "$REPO_DIR")" ]]; then
    head=$(git -C "$REPO_DIR" rev-parse --verify --quiet 'HEAD^{commit}' || true)
  fi
  if [[ -n "$head" ]]; then
    echo "$head" > "$STATE/current"
    log "sudo upgrade.sh installed ${head:0:7} (v$disk)"
    echo "deploy-hub: recorded the installed commit ${head:0:7} (v$disk)"
  else
    rm -f "$STATE/current"
    log "sudo upgrade.sh installed a working tree that is not a clean commit (v${disk:-?}) - deployed commit unknown"
    echo "deploy-hub: the installed tree is not a clean commit - deployed commit recorded as unknown"
  fi
}

MODE=deploy; FORCE=no; SKIP_TESTS=no; REF=""; FLAKY=""; RESTARTED=no
(($#)) || MODE=help
for arg in "$@"; do
  case "$arg" in
    --status) MODE=status ;;
    --rollback) MODE=rollback ;;
    --record-install) MODE=record-install ;;
    -h|--help) MODE=help ;;
    --force) FORCE=yes ;;
    --skip-tests) SKIP_TESTS=yes ;;
    -*) die "unknown option: $arg (see --help)" ;;
    *) [[ -z "$REF" ]] || die "one ref only, got: $REF $arg"; REF="$arg" ;;
  esac
done
case "$MODE" in
  status)
    echo "live:    $(curl -fsS -m 2 "$HUB/healthz" 2>/dev/null || echo unreachable)"
    echo "commit:  $(cat "$STATE/current" 2>/dev/null || echo 'unknown (last deployed with sudo upgrade.sh?)')"
    [[ ! -f "$SNAP/meta" ]] || echo "rollback: to v$(meta version) ($(meta commit | cut -c1-7)), undoes v$(meta for_version) ($(meta for_commit | cut -c1-7))"
    echo "log:     $STATE/deploy.log"; tail -n 5 "$STATE/deploy.log" 2>/dev/null || true
    exit 0 ;;
  rollback)
    [[ -z "$REF" && "$SKIP_TESTS" == no ]] || die "--rollback takes no ref and no --skip-tests"
    rollback; exit 0 ;;
  record-install)
    [[ -z "$REF" && "$FORCE" == no && "$SKIP_TESTS" == no ]] || die "--record-install takes no ref and no options"
    record_install; exit 0 ;;
  help)
    awk 'NR > 1 && !/^#/ { exit } NR > 1 { sub(/^# ?/, ""); print }' "$0"; exit 0 ;;
esac
[[ -n "$REF" ]] || die "no ref given (see --help)"
[[ "$REF" =~ ^[A-Za-z0-9._/@^~-]+$ ]] || die "bad ref: $REF"

# --- only pushed history ships: a stale local main or an unpushed branch is refused (fetch first, so origin/... is current)
# F-10: never prompt (GIT_TERMINAL_PROMPT=0; an ssh passphrase prompt in timeout's background process group just stops
# until the time is up) and never hang - a fetch that fails or runs out of time falls back to the last fetched
# origin/main, as a failed one always did
GIT_TERMINAL_PROMPT=0 timeout -k 5 "$FETCH_TIMEOUT" git -C "$REPO_DIR" fetch --quiet origin </dev/null 2>/dev/null \
  || echo "deploy: warning: cannot fetch origin (failed, or no answer in ${FETCH_TIMEOUT}s) - checking against the last fetched origin/main" >&2
COMMIT=$(git -C "$REPO_DIR" rev-parse --verify --quiet "$REF^{commit}") || die "no such commit: $REF"
SHORT=${COMMIT:0:7}
git -C "$REPO_DIR" rev-parse --verify --quiet "origin/main^{commit}" >/dev/null || die "no origin/main in $REPO_DIR to check $SHORT against"
git -C "$REPO_DIR" merge-base --is-ancestor "$COMMIT" origin/main \
  || die "$SHORT ($REF) is not on origin/main - merge and push it first (unpushed or side branches are never deployed)"

# --- preflight: anything that needs root goes through sudo ./deploy/upgrade.sh
[[ -w "$APP_DIR/server" ]] || die "$APP_DIR/server is not writable by $(id -un) - use: sudo ./deploy/upgrade.sh"
agent=no; [[ -w "$APP_DIR/agent" ]] && agent=yes

STAGE=$(mktemp -d "$STATE/stage.XXXXXX")
trap 'rm -rf "$STAGE"' EXIT
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

# --- the test suite, on exactly the tree that ships (both attempts, when there are two, end up in tests.log)
: > "$STATE/tests.log"
if [[ "$SKIP_TESTS" == yes ]]; then
  echo "deploy: WARNING: --skip-tests: deploying $SHORT WITHOUT running the test suite (pytest); static checks still run" >&2
  log "skip-tests requested for $SHORT ($REF) by $(id -un)"
  # bash -n / node --check / py_compile take seconds and catch a release that cannot even start (F-11)
  if [[ -f "$STAGE/tests/run.sh" ]] && grep -q AVM_TEST_SKIP_PYTEST "$STAGE/tests/run.sh"; then
    echo "deploy: running the static checks of tests/run.sh on $SHORT ..."
    if ! run_suite "static checks (--skip-tests)" AVM_TEST_SKIP_PYTEST=1; then
      tail -n 40 "$STATE/tests.log" >&2
      die "the static checks failed on $SHORT - not deploying, not even with --skip-tests (full output: $STATE/tests.log)"
    fi
  else
    echo "deploy: warning: $SHORT's tests/run.sh cannot skip pytest alone - its static checks are skipped too" >&2
  fi
else
  [[ -f "$STAGE/tests/run.sh" ]] || die "$SHORT has no tests/run.sh - re-run with --skip-tests if you must deploy it"
  [[ -x "$TEST_PY" ]] || die "no Python for the test suite at $TEST_PY (create $REPO_DIR/.venv) - or re-run with --skip-tests"
  echo "deploy: running tests/run.sh on $SHORT ..."
  # a failure that is pytest's alone gets one retry of the files with failed tests: a flaky test (timing, a busy box)
  # must not block a release, a real failure fails twice. A timeout or a failed static check is never retried.
  if ! run_suite "tests/run.sh" && ! retry_failed_pytest; then
    tail -n 40 "$STATE/tests.log" >&2
    grep -q '^=== timed out' "$STATE/tests.log" \
      && die "the test suite timed out after ${TESTS_TIMEOUT}s on $SHORT - not deploying (full output: $STATE/tests.log)"
    die "the test suite failed on $SHORT - not deploying (full output: $STATE/tests.log)"
  fi
fi

# --- keep what is live now, in a new copy: a deploy that fails must leave the previous rollback copy alone (F-08), or a
#     later --rollback would put back what is live anyway. meta records what the copy is and which deploy it undoes.
#     I-05: redeploying the live commit (the re-run after a failed agent/ copy) uses the new copy only to undo itself;
#     the rollback copy stays the way back from that commit's first deploy
LIVE=$(cat "$STATE/current" 2>/dev/null || echo unknown)
REDEPLOY=no; [[ "$LIVE" == "$COMMIT" && "$HAVE" == "$WANT" ]] && REDEPLOY=yes
SNAP="$STATE/rollback.new"
rm -rf "$SNAP"; mkdir -p "$SNAP"
rsync -a "${EXCLUDES[@]}" "$APP_DIR/server/" "$SNAP/server/"
[[ "$agent" == yes ]] && rsync -a --exclude __pycache__ "$APP_DIR/agent/" "$SNAP/agent/"
printf 'commit=%s\nversion=%s\nfor_commit=%s\nfor_version=%s\n' "$LIVE" \
  "${HAVE:-unknown}" "$COMMIT" "$WANT" > "$SNAP/meta"

# --- hub first: the old process keeps running from memory until the restart
# I-06: from here until the smoke test passes, Ctrl-C / SIGTERM / a dropped ssh (HUP) rolls back like a failed check
trap 'on_signal INT' INT; trap 'on_signal TERM' TERM; trap 'on_signal HUP' HUP
put "$STAGE" server || die "copying server/ failed; previous code restored, live hub untouched"
RESTARTED=yes   # set first: a restart cut short may still have happened, and restarting the old code again is harmless
if ! restart; then
  trap '' INT TERM HUP
  restore server
  die "restart refused - is the polkit rule installed? (deploy/polkit/). Code on disk restored; live hub untouched."
fi
if ! wait_healthy "$WANT" || ! smoke; then
  trap '' INT TERM HUP   # the rollback below is what an interrupt would do; let it finish
  echo "deploy: v$WANT did not become healthy - rolling back (agent/ was not touched)" >&2
  restore server
  restart || true
  log "FAILED $SHORT ($REF, v$WANT) - rolled back"
  wait_healthy "$(version_in "$SNAP")" && die "rolled back; previous version is live. Logs: journalctl -u $UNIT -n 50"
  die "rollback did not come back healthy either - needs a human: journalctl -u $UNIT -n 50"
fi
trap '' INT TERM HUP   # the new hub is live: record it in one go, an interrupt now must not split current and the copy
echo "$COMMIT" > "$STATE/current"
if [[ "$REDEPLOY" == yes ]]; then
  # I-05: the same release again - the existing copy (if any) still undoes it; this one would only hold it
  rm -rf "$SNAP"; SNAP="$STATE/rollback"
  echo "deploy: note: $SHORT was already live - kept the rollback copy$([[ -f "$SNAP/meta" ]] && echo " (to v$(meta version))")"
else
  # the new hub is live: from now on the way back from it is the copy taken above
  rm -rf "$STATE/rollback.old"; [[ ! -d "$STATE/rollback" ]] || mv "$STATE/rollback" "$STATE/rollback.old"
  mv "$SNAP" "$STATE/rollback"; SNAP="$STATE/rollback"
  rm -rf "$STATE/rollback.old" "$STATE/rollback.commit"   # rollback.commit: the provenance file before meta
fi
trap - INT TERM HUP

# --- only now agent/: the hub serves it to every host on its next report, so it must never get ahead of a hub
#     that may still be rolled back
if [[ "$agent" == yes ]] && ! put "$STAGE" agent; then
  log "deployed $SHORT ($REF) as v$WANT by $(id -un), but agent/ FAILED to copy (previous agent restored)"
  die "hub v$WANT is live, but copying agent/ failed - the previous agent was restored. Re-run, or sudo ./deploy/upgrade.sh"
fi
log "deployed $SHORT ($REF) as v$WANT by $(id -un)$([[ $agent == yes ]] && echo ' incl. agent' || echo ', hub only')"
echo "deployed $SHORT ($REF): hub v$WANT is live$([[ $agent == yes ]] && echo ' (agent/ updated too: hosts self-update on their next report)')"

# --- F-05: the root backup unit runs its own root-owned avm (no self-update, and this script cannot write it). Only a
#     warning: an older avm still reports the backup; re-running the installer with the installed config keeps the
#     repository, password and schedule and only refreshes the copies (and re-seeds, which keeps a real run's status)
stale=no
if [[ -f "$BACKUP_AVM" && -f "$STAGE/agent/avm.py" ]]; then
  bav=$(avm_version "$BACKUP_AVM"); nav=$(avm_version "$STAGE/agent/avm.py")
  if [[ "$bav" != "$nav" ]]; then
    echo "deploy: warning: the backup service runs its own copy of avm ($BACKUP_AVM, v${bav:-?}), not this release's v${nav:-?}." >&2
    stale=yes
  fi
fi
# I-13: and its own copy of the runner, /usr/local/sbin/<unit> (the installed config's "unit", default avalon-backup)
if [[ -f "$BACKUP_CONF" && -f "$STAGE/deploy/backup/restic-backup.py" ]]; then
  bunit=$(python3 -I -c 'import json,sys; print(json.load(open(sys.argv[1])).get("unit") or "avalon-backup")' "$BACKUP_CONF" 2>/dev/null || true)
  if [[ "$bunit" =~ ^[A-Za-z0-9_.@-]+$ && -f "$BACKUP_SBIN/$bunit" ]] \
     && ! cmp -s "$BACKUP_SBIN/$bunit" "$STAGE/deploy/backup/restic-backup.py"; then
    echo "deploy: warning: the backup service runs its own copy of the runner ($BACKUP_SBIN/$bunit), not this release's deploy/backup/restic-backup.py." >&2
    stale=yes
  fi
fi
[[ "$stale" == no ]] || echo "deploy: refresh it from a checkout of $SHORT with: sudo $REPO_DIR/deploy/backup/install-restic-backup.sh /etc/avalon-backup/config.json" >&2

# I-07: a deploy that needed the retry says so where it is seen, not only in deploy.log
[[ -z "$FLAKY" ]] || echo "FLAKY: $FLAKY"
