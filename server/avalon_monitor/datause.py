"""Hub-side safety net for hosts whose files must not leave them (data-use agreements).

The agent's own switches (AVM_TASK_LOG, AVM_JOB_TRANSCRIPT, AVM_SESSION_SNIPPETS, AVM_CONTENT_ROOTS) keep data on the
host; this makes a forgotten agent.conf line fail safe. A host is restricted when its hub setting AVM_DATA_USE is
"restricted" (set on the host page, kept by the hub) OR its agent reports a transcript level below "full". For a
restricted host the hub drops, before anything is stored or broadcast: task output lines, commands and working
directories; prompt tool inputs/results and intermediate text; Claude session snippets and process arguments; and
content-check text.
"""
from __future__ import annotations

from typing import Any, Optional

KEY = "AVM_DATA_USE"
SNIPPET_KEYS = ("first_prompt", "last_user", "last_assistant", "last_said", "detail", "args")
WITHHELD = "(withheld: data-use host)"


def restricted(host: Any, sample: Optional[dict] = None) -> bool:
    if str(((getattr(host, "check_config", None) or {}).get(KEY) or "")).strip().lower() in ("restricted", "on", "true", "1", "yes"):
        return True
    for s in (sample, getattr(host, "last_sample", None)):
        policy = ((s or {}).get("claude") or {}).get("policy") if isinstance(s, dict) else None
        if isinstance(policy, dict):
            return policy.get("transcript") in ("summary", "off") or policy.get("snippets") is False
    return False


def scrub_claude(claude: Optional[dict]) -> Optional[dict]:
    if not isinstance(claude, dict):
        return claude
    out = dict(claude)
    for k in ("sessions", "agents", "processes"):
        if isinstance(out.get(k), list):
            out[k] = [{kk: vv for kk, vv in d.items() if kk not in SNIPPET_KEYS} if isinstance(d, dict) else d for d in out[k]]
    return out


def scrub_event(ev: dict) -> dict:
    """Same shape as the agent's AVM_JOB_TRANSCRIPT=summary: tool names and the final answer only."""
    t = ev.get("type")
    if t in ("system", "result"):
        return ev
    if t in ("assistant", "user"):
        parts = [{"type": "tool_use", "name": p.get("name"), "input": ""} for p in ev.get("parts") or []
                 if isinstance(p, dict) and p.get("type") == "tool_use"]
        return {"type": t, "parts": parts, "model": ev.get("model")}
    return {"type": t or "raw"}
