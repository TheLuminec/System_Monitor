"""Hub-side safety net for hosts whose files must not leave them (data-use agreements).

The agent's own switches (AVM_TASK_LOG, AVM_JOB_TRANSCRIPT, AVM_SESSION_SNIPPETS, AVM_CONTENT_ROOTS) keep data on the
host; this makes a forgotten agent.conf line fail safe. A host is restricted when its hub setting AVM_DATA_USE is
"restricted" (set on the host page, kept by the hub) OR its agent reports a transcript level below "full". For a
restricted host the hub drops, before anything is stored or broadcast: task output lines, commands, working
directories and notes (of a task's meta only `keep_meta` is stored: never avm's meta.note or queue reason); prompt
tool inputs/results and intermediate text; Claude session snippets and process arguments; tracked sessions' text and
all but the last part of their working directory; and content-check text.

Sticky: the first report whose policy is restricted, from a host whose AVM_DATA_USE is unset (empty, or a value that
is neither on nor off), makes the hub store AVM_DATA_USE=restricted for it (see `sticky`) - so an agent.conf that
later loses its lines doesn't quietly un-restrict the host: the hub raises a "data-use policy" alert instead and keeps
scrubbing. Storing it also clears what the hub already holds from the host's unrestricted days (db.datause_purge).
Only an operator lifts it, by setting an explicit off value (OFF) on the host page; the hub never
overwrites that (while the agent still reports a restricted policy, the host stays restricted through it anyway).
"""
from __future__ import annotations

from typing import Any, Optional

KEY = "AVM_DATA_USE"
ON = ("restricted", "on", "true", "1", "yes")
OFF = ("off", "normal", "none", "false", "0", "no")   # an operator lifted it: never auto-set over this
SNIPPET_KEYS = ("first_prompt", "last_user", "last_assistant", "last_said", "detail", "args")
WITHHELD = "(withheld: data-use host)"
CHECK_NAME = "data-use policy"   # hub rule check, failing while the agent's policy no longer matches AVM_DATA_USE
# A data-use host's task meta keeps only these (avm sends hostname, resources, queue, code and note): key -> allowed
# subkeys, None = scalars as is. Never meta.note or meta.queue.reason/.disabled - free text that can name the data.
META_KEEP = {"hostname": None, "resources": None, "queue": ("since", "position", "waited", "max_wait", "inherited"),
             "code": ("sha", "dirty", "at")}


def setting(host: Any) -> str:
    """The host's AVM_DATA_USE, normalised ("" = unset)."""
    return str(((getattr(host, "check_config", None) or {}).get(KEY) or "")).strip().lower()


def policy_restricted(policy: Any) -> Optional[bool]:
    """What an agent's claude.policy (agent 1.9+) says: True = it keeps transcripts/snippets on the host; None = no
    policy reported (older agent, or no claude block)."""
    if not isinstance(policy, dict):
        return None
    return policy.get("transcript") in ("summary", "off") or policy.get("snippets") is False


def _policy(s: Any) -> Any:
    return ((s or {}).get("claude") or {}).get("policy") if isinstance(s, dict) else None


def restricted(host: Any, sample: Optional[dict] = None) -> bool:
    if setting(host) in ON:
        return True
    for s in (sample, getattr(host, "last_sample", None)):
        verdict = policy_restricted(_policy(s))
        if verdict is not None:
            return verdict
    return False


def sticky(host: Any, claude: Optional[dict]) -> tuple[bool, Optional[str]]:
    """For one report's claude block: (store AVM_DATA_USE=restricted now, alert text if the agent's policy no longer
    matches the host's setting). Only a report that carries a policy counts: an older agent's (none) changes nothing."""
    reported = policy_restricted(_policy({"claude": claude}))
    flag = setting(host)
    if reported and flag not in ON + OFF:
        return True, None
    if reported is False and flag in ON:
        pol = (claude or {}).get("policy") or {}
        return False, (f"data-use host ({KEY}={flag}), but its agent now reports transcript={pol.get('transcript')}, "
                       f"snippets {'off' if pol.get('snippets') is False else 'on'}: did its agent.conf lose AVM_JOB_TRANSCRIPT / "
                       "AVM_SESSION_SNIPPETS? The hub keeps scrubbing. Restore those lines, or set "
                       f"{KEY}=off in the host settings if it no longer holds agreement data")
    return False, None


def keep_meta(meta: Optional[dict]) -> Optional[dict]:
    """A task meta reduced to META_KEEP (task start and beat, and stored tasks when the flag is first set). A None
    value stays: on a beat it removes that key (db.task_update)."""
    if not isinstance(meta, dict):
        return None
    out: dict = {}
    for k, sub in META_KEEP.items():
        v = meta.get(k)
        if k not in meta or isinstance(v, list):
            continue
        if isinstance(v, dict):
            out[k] = {kk: vv for kk, vv in v.items() if (sub is None or kk in sub) and not isinstance(vv, (dict, list))}
        elif sub is None or v is None:
            out[k] = v
    return out or None


def last_part(path: Optional[str]) -> Optional[str]:
    """A working directory's last component: what a tracked session on a data-use host keeps of it."""
    if not path:
        return path
    return path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1] or path


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
