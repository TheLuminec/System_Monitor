"""Auto mode: Claude Code's `--permission-mode auto` as a job mode. It ranks plan < acceptEdits < auto < full on the
agent (AVM_MAX_JOB_MODE) as on the hub. Before it, the agent didn't know `auto`: a cap of auto fell back to plan and
an auto job was refused as an unknown mode."""
import io

import pytest

import avalon_agent as a
from test_agent import _Runner
from test_round_f import _ActionRunner, _restart


@pytest.mark.parametrize("cap,mode,allowed", [
    ("auto", "auto", True), ("auto", "acceptEdits", True), ("auto", "plan", True), ("auto", "full", False),
    ("acceptEdits", "auto", False), ("plan", "auto", False), ("full", "auto", True),
])
def test_auto_is_ranked_between_accept_edits_and_full(cap, mode, allowed):
    """Auto mode: a cap of auto takes auto and below, never full; caps below auto refuse it."""
    why = _Runner(AVM_MAX_JOB_MODE=cap)._refusal({"id": 1, "prompt": "x", "mode": mode})
    assert (why is None) is allowed, why


def test_auto_cap_is_reported():
    """Auto mode: the policy the hub sees says auto (it was reported as plan, the unknown-value fallback)."""
    assert _Runner(AVM_MAX_JOB_MODE="auto").policy()["max_mode"] == "auto"


def test_auto_job_runs_with_permission_mode_auto(monkeypatch, tmp_path):
    """Auto mode: a job in auto mode starts claude with `--permission-mode auto`, never the bypass flag."""
    seen = {}

    class P:
        def __init__(self, args, **kw):
            seen["args"] = args
            self.stdout, self.stderr = io.StringIO(""), io.StringIO("")

        def wait(self):
            return 0

    monkeypatch.setattr(a.subprocess, "Popen", P)
    _Runner(AVM_MAX_JOB_MODE="auto")._run_job({"id": 3, "prompt": "hi", "mode": "auto", "cwd": str(tmp_path)})
    args = seen["args"]
    assert args[args.index("--permission-mode") + 1] == "auto" and "--dangerously-skip-permissions" not in args


def test_resume_at_an_auto_cap_uses_permission_mode_auto():
    """Auto mode: a restart-with-prompt resumes at the host's cap, here auto."""
    args = _restart(_ActionRunner(AVM_MAX_JOB_MODE="auto"), {"session_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"},
                    prompt="continue")[0]
    assert args[args.index("--permission-mode") + 1] == "auto" and "--dangerously-skip-permissions" not in args
