#!/usr/bin/env bash
# The whole automated test suite, one entry point. deploy/deploy-hub.sh runs it on the exact commit it deploys
# (a `git archive` of it, with the repo's .venv) and refuses the deploy if it fails.
#
#   tests/run.sh     pytest tests/, bash -n on every *.sh, node --check on the dashboard JS,
#                    py_compile on the agent and the backup scripts
#
# Runs every check, then exits non-zero if any failed. AVM_TEST_PYTHON picks the interpreter for pytest
# (default: .venv/bin/python in the repo root). AVM_TEST_SKIP_PYTEST=1 runs only the static checks (what
# deploy-hub.sh --skip-tests still runs).
set -uo pipefail   # no -e: one failing check must not hide the others

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${AVM_TEST_PYTHON:-$ROOT/.venv/bin/python}"
cd "$ROOT"
shopt -s nullglob
failed=()

each() {  # $1 = label, $2 = command (word-split on purpose), rest = files: run the command on every file
  local label=$1 cmd=$2 f bad=0; shift 2
  (($#)) || { echo "FAIL  $label: no files found" >&2; failed+=("$label"); return; }
  for f in "$@"; do $cmd "$f" || { echo "      in $f" >&2; bad=1; }; done
  if ((bad)); then echo "FAIL  $label" >&2; failed+=("$label"); else echo "ok    $label ($# files)"; fi
}

if [[ "${AVM_TEST_SKIP_PYTEST:-}" == 1 ]]; then
  echo "skip  pytest (AVM_TEST_SKIP_PYTEST=1)"
elif [[ -x "$PY" ]]; then
  if "$PY" -m pytest tests -q -p no:warnings; then echo "ok    pytest"; else echo "FAIL  pytest" >&2; failed+=(pytest); fi
else
  echo "FAIL  pytest: no Python at $PY (create .venv, or set AVM_TEST_PYTHON)" >&2; failed+=(pytest)
fi

scripts=()
while IFS= read -r f; do scripts+=("$f"); done < <(
  find . \( -name .git -o -name .venv -o -name venv -o -name .claude -o -name node_modules \) -prune \
    -o -name '*.sh' -type f -print | sort)
each "bash -n" "bash -n" "${scripts[@]}"

if command -v node >/dev/null; then
  each "node --check" "node --check" server/avalon_monitor/static/*.js
else
  echo "FAIL  node --check: node is not installed" >&2; failed+=("node --check")
fi
each "py_compile" "python3 -m py_compile" agent/*.py deploy/backup/*.py

if ((${#failed[@]})); then
  echo "tests/run.sh: ${#failed[@]} check(s) FAILED: ${failed[*]}" >&2
  exit 1
fi
if [[ "${AVM_TEST_SKIP_PYTEST:-}" == 1 ]]; then echo "tests/run.sh: all static checks passed (pytest SKIPPED)"; exit 0; fi
echo "tests/run.sh: all checks passed"
