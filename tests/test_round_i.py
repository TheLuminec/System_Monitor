"""Audit round I, agent side: I-02 (AVM_TASK_LOG_OFF_DIRS fails closed: unknown cwds, every cwd a transcript
recorded, dashboard prompts that move into an off dir) and I-01's avm part (with the task log off, --note text and
queue reasons never leave the host). The hub side of I-01 / I-03 is in the hub round-I tests."""
import io
import json
import os

import pytest

import avalon_agent as a
import avm
from test_agent import EV_FINAL, _finish, _Runner
from test_avm import CHATTY, Env, live_hub, sh   # noqa: F401  (live_hub: the fixture env needs)
from test_round_f import _ActionRunner


@pytest.fixture
def env(tmp_path, monkeypatch, host, live_hub):   # noqa: F811
    return Env(tmp_path, monkeypatch, host, live_hub)


# ------------------------------------------------------------------ I-02: off dirs fail closed
@pytest.fixture
def world(tmp_path):
    """A Claude home with an off dir and an ordinary project next to it."""
    home = tmp_path / "home"
    off, near = home / "dua", home / "proj"
    off.mkdir(parents=True)
    near.mkdir()
    return {"home": home, "off": off, "near": near, "projects": home / ".claude" / "projects" / "slug"}


SID = "11111111-2222-3333-4444-555555555555"


def _line(kind, cwd, text):
    msg = {"role": kind, "content": text if kind == "user" else [{"type": "text", "text": text}]}
    return json.dumps({"type": kind, "cwd": cwd, "sessionId": SID, "timestamp": "2026-10-27T10:00:00Z", "message": msg})


def _transcript(world, cwds, pad=0, sid=SID):
    """A transcript whose entries run in `cwds` in turn. `pad` filler entries (in ~) after the first entry and again at
    the end put the later cwds past the 64 KB head and before the 64 KB tail the summary reads: the middle."""
    world["projects"].mkdir(parents=True, exist_ok=True)
    lines = [_line("user", cwds[0], "SECRET-first prompt")]
    lines += [_line("assistant", str(world["home"]), "filler %d %s" % (i, "x" * 200)) for i in range(pad)]
    for c in cwds[1:]:
        lines += [_line("user", c, "SECRET-user in %s" % c), _line("assistant", c, "SECRET-answer")]
    lines += [_line("assistant", str(world["home"]), "filler %d %s" % (i, "y" * 200)) for i in range(pad)]
    fp = world["projects"] / (sid + ".jsonl")
    fp.write_text("\n".join(lines) + "\n")
    return fp


def _runner(world, **cfg):
    return _ActionRunner(AVM_CLAUDE_HOME=str(world["home"]), AVM_TASK_LOG_OFF_DIRS=str(world["off"]),
                         AVM_SESSION_SNIPPETS="on", **cfg)


@pytest.mark.parametrize("cwds,pad", [
    (["HOME", "OFF"], 0),            # started in ~, then worked in the off dir (tail)
    (["HOME", "OFF"], 400),          # ... with the off-dir part past the head and before the tail: the middle
    (["OFF", "HOME"], 400),          # started in the off dir, ended in ~
])
def test_session_that_worked_in_an_off_dir_is_stripped(world, cwds, pad):
    """I-02: every cwd a session's transcript recorded counts, not just the first - a session started in ~ that later
    (or only for a while) works in an off dir reports no snippets."""
    names = {"HOME": str(world["home"]), "OFF": str(world["off"] / "sub")}
    _transcript(world, [names[c] for c in cwds], pad)
    r = _runner(world)
    r.agents, r.processes = (lambda: []), (lambda: [])
    rep = r.report()
    s = rep["sessions"][0]
    assert s["session_id"] == SID and s["redacted"] is True and "SECRET" not in json.dumps(rep)
    assert "_cwds" not in s


def test_session_that_stayed_out_of_off_dirs_keeps_snippets(world):
    """I-02: the same transcript entirely outside the off dir keeps its snippets (and no internal key leaks)."""
    _transcript(world, [str(world["home"]), str(world["near"])], 400)
    r = _runner(world)
    r.agents, r.processes = (lambda: []), (lambda: [])
    s = r.report()["sessions"][0]
    assert "SECRET" in json.dumps(s) and not s.get("redacted") and "_cwds" not in s


def test_unknown_cwd_rows_are_stripped_only_when_off_dirs_are_set(world):
    """I-02a: with off dirs set, a row whose directory is unknown (a process whose cwd psutil can't read, a session or
    agent without one) is stripped and marked; without off dirs nothing changes."""
    rows = {"sessions": [{"session_id": "s", "cwd": "", "first_prompt": "SECRET-1"}],
            "agents": [{"key": "k", "name": "SECRET-name", "cwd": "", "last_said": "SECRET-2"}],
            "processes": [{"pid": 7, "cwd": "", "args": "-p SECRET-3"}]}
    for cfg, stripped in (({"AVM_TASK_LOG_OFF_DIRS": str(world["off"])}, True), ({}, False)):
        r = _ActionRunner(AVM_SESSION_SNIPPETS="on", **cfg)
        r.sessions, r.agents, r.processes = (lambda: rows["sessions"]), (lambda: rows["agents"]), (lambda: rows["processes"])
        rep = r.report()
        assert ("SECRET" in json.dumps(rep)) is not stripped
        assert [rep[k][0].get("redacted") for k in ("sessions", "agents", "processes")] == [stripped or None] * 3


def test_agent_rows_use_every_transcript_cwd_and_hide_the_name_and_process(world):
    """I-02: a listed session whose listing says ~ but whose transcript later works in the off dir is stripped, its
    name (Claude Code may make it from the first prompt) is left out, and its claude process' arguments too."""
    _transcript(world, [str(world["home"]), str(world["off"])])
    r = _runner(world)
    r._agents = r._shape_agents([{"sessionId": SID, "kind": "interactive", "name": "SECRET-name", "cwd": str(world["home"]),
                                  "pid": 4242}])
    r.sessions = lambda: []
    r.processes = lambda: [{"pid": 4242, "cwd": str(world["home"]), "args": "-p SECRET-prompt"},
                           {"pid": 1, "cwd": str(world["near"]), "args": "kept"}]
    r._agents_at = 1e18   # no background refresh
    rep = r.report()
    ag = rep["agents"][0]
    assert ag["redacted"] is True and "name" not in ag and "_cwds" not in ag and ag["session_id"] == SID
    assert "SECRET" not in json.dumps(rep) and rep["processes"][1]["args"] == "kept"
    assert "_cwds" in r._agents[0]   # the cache itself is not modified by report()


def test_seen_cwds_reads_appends_and_waits_for_whole_lines(world):
    """I-02: the cwd scan reads a transcript once, then only what was appended - and never half a line."""
    fp = _transcript(world, [str(world["home"])])
    r = _runner(world)
    assert r._seen_cwds(str(fp)) == {str(world["home"])}
    with open(fp, "a") as f:
        f.write(_line("user", str(world["off"]), "x")[:30])   # still being written
    assert r._seen_cwds(str(fp)) == {str(world["home"])}
    with open(fp, "a") as f:
        f.write(_line("user", str(world["off"]), "x")[30:] + "\n")
    assert r._seen_cwds(str(fp)) == {str(world["home"]), str(world["off"])}
    fp.write_text(_line("user", "C:\\Users\\u \"q\"", "x") + "\n")   # rewritten (shorter): read again; JSON escapes
    assert r._seen_cwds(str(fp)) == {"C:\\Users\\u \"q\""}


class _P:
    lines = []

    def __init__(self, args, **kw):
        self.stdout, self.stderr = io.StringIO("\n".join(json.dumps(x) for x in self.lines) + "\n"), io.StringIO("")

    def wait(self):
        return 0


def _tool(name, inp, text="SECRET-text"):
    return {"type": "assistant", "message": {"model": "m", "content": [{"type": "text", "text": text},
                                                                         {"type": "tool_use", "name": name, "input": inp}]}}


EV_SYS = {"type": "system", "subtype": "init", "session_id": SID, "cwd": "/somewhere"}
EV_EARLY = _tool("Bash", {"command": "ls"}, "kept-early")


def _job(monkeypatch, world, lines, cwd=None, **job):
    monkeypatch.setattr(_P, "lines", lines)
    monkeypatch.setattr(a.subprocess, "Popen", _P)
    r = _Runner(AVM_CLAUDE_HOME=str(world["home"]), AVM_TASK_LOG_OFF_DIRS=str(world["off"]), AVM_JOB_TRANSCRIPT="full")
    r.OFF_LOOK_SEC = 0.0
    r._run_job(dict({"id": 5, "prompt": "p", "mode": "plan", "cwd": str(cwd or world["near"])}, **job))
    assert _finish(r)["status"] == "done"
    return json.dumps(r.posts)


@pytest.mark.parametrize("inp", [
    lambda w: {"file_path": str(w["off"] / "data.csv")},           # a Read of a file there
    lambda w: {"command": "cd %s && head data.csv" % w["off"]},     # a Bash cd there
    lambda w: {"command": "cat ~/dua/data.csv"},                    # written with ~ (the agent's home here)
])
def test_running_prompt_that_reaches_an_off_dir_drops_to_summary(monkeypatch, world, inp):
    """I-02: a dashboard prompt started outside the off dirs is checked while it runs: from the first event whose tool
    input names a path in an off dir, it sends a summary transcript (tool names and the final answer only)."""
    monkeypatch.setenv("HOME", str(world["home"]))
    monkeypatch.setenv("USERPROFILE", str(world["home"]))
    later = {"type": "user", "message": {"content": [{"type": "tool_result", "content": "SECRET,1,2"}]}}
    sent = _job(monkeypatch, world, [EV_SYS, EV_EARLY, _tool("Read", inp(world)), later, EV_FINAL])
    assert "kept-early" in sent and "SECRET" not in sent and "FINAL-ANSWER" in sent and "dua/data.csv" not in sent


def test_running_prompt_whose_transcript_records_an_off_dir_cwd_drops_to_summary(monkeypatch, world):
    """I-02: a move the tool inputs don't show (a relative cd) is caught from the cwd the session's transcript records."""
    _transcript(world, [str(world["near"]), str(world["off"])])
    sent = _job(monkeypatch, world, [EV_SYS, _tool("Bash", {"command": "cd ../dua"}, "SECRET-after"), EV_FINAL])
    assert "SECRET" not in sent and "FINAL-ANSWER" in sent


def test_prompt_outside_off_dirs_keeps_its_full_transcript(monkeypatch, world):
    """I-02: no off-dir path, no off-dir cwd: unchanged (full)."""
    _transcript(world, [str(world["near"])])
    sent = _job(monkeypatch, world, [EV_SYS, _tool("Read", {"file_path": str(world["near"] / "x")}, "kept-text"), EV_FINAL])
    assert "kept-text" in sent and str(world["near"] / "x") in sent


def test_resuming_a_session_that_worked_in_an_off_dir_sends_a_summary(monkeypatch, world):
    """I-02: a prompt resuming (or forking) a session whose transcript worked in an off dir is summary from the start."""
    _transcript(world, [str(world["near"]), str(world["off"])])
    sent = _job(monkeypatch, world, [EV_SYS, _tool("Bash", {"command": "ls"}, "SECRET-text"), EV_FINAL], resume_session=SID)
    assert "SECRET" not in sent and "FINAL-ANSWER" in sent


# ------------------------------------------------------------------ I-01: avm keeps notes and queue reasons
def _bodies(env):
    return json.dumps([c["body"] for c in env.calls])


@pytest.mark.parametrize("conf,where", [({"AVM_TASK_LOG": "off"}, ""), ({"AVM_TASK_LOG_OFF_DIRS": "OFF"}, "off")])
def test_task_cli_note_stays_local_with_the_log_off(env, conf, where):
    """I-01: `avm task start/beat/done/fail --note` with the task log off (config, or a cwd in an off dir) sends no
    note at all - not in meta.note at start, not at beat or finish."""
    off = env.tmp / "off"
    off.mkdir()
    env.write(**{k: (str(off) if v == "OFF" else v) for k, v in conf.items()})
    env.mp.chdir(off if where else env.tmp)
    assert env.run("task", "start", "manual", "--note", "SECRET-start") == 0
    tid = env.tid()
    assert env.run("task", "beat", str(tid), "--note", "SECRET-beat", "--progress", "0.5") == 0
    assert env.run("task", "fail", str(tid), "--note", "SECRET-fail") == 0
    assert "SECRET" not in _bodies(env)
    t = env.task(tid)
    assert t["status"] == "failed" and "SECRET" not in json.dumps(t, default=str)


def test_task_cli_note_still_sent_with_the_log_on(env):
    """I-01: the log on, notes go as before."""
    assert env.run("task", "start", "manual", "--note", "kept-start") == 0
    assert env.run("task", "done", str(env.tid()), "--note", "kept-done") == 0
    assert "kept-start" in _bodies(env) and "kept-done" in _bodies(env)


def test_run_note_stays_local_with_the_log_off(env):
    """I-01: `avm run --note` (plain and queued) with the log off: no meta.note at start."""
    env.write(AVM_TASK_LOG="off")
    assert env.run("run", "plain", "--note", "SECRET-plain", "--beat", "0.2", "--", *sh("true")) == 0
    assert env.run("run", "gpu", "--gpu", "--note", "SECRET-gpu", "--beat", "0.2", "--", *sh("true")) == 0
    assert "SECRET" not in _bodies(env)


def test_queue_disabled_run_keeps_the_note_local_with_the_log_off(env, monkeypatch):
    """I-01: a queued run falling back to no admission control (F-07) notes that on the task - without --note's text."""
    env.write(AVM_TASK_LOG="off")

    def unsafe(cfg):
        raise avm.QueueUnsafe("refusing queue dir: test")
    monkeypatch.setattr(avm, "Queue", unsafe)
    assert env.run("run", "gpu", "--gpu", "--note", "SECRET-note", "--beat", "0.2", "--", *sh("true")) == 0
    sent = _bodies(env)
    assert "SECRET" not in sent and "without admission control" in sent


def _queue_for(monkeypatch, waits):
    """try_admit says 'no' (with a reason naming another job) `waits` times, then yes; polls fast, beats every poll."""
    n = {"calls": 0}

    def try_admit(self):
        n["calls"] += 1
        if n["calls"] <= waits:
            return False, "SECRET-REASON: waiting behind 'cohort export'", 1
        return True, "", 0
    monkeypatch.setattr(avm.Queue, "try_admit", try_admit)
    monkeypatch.setattr(avm, "QUEUE_POLL", 0.05)
    monkeypatch.setattr(avm, "QUEUE_BEAT", 0.0)


@pytest.mark.parametrize("log_off", [True, False])
def test_queue_reason_stays_local_with_the_log_off(env, monkeypatch, log_off):
    """I-01: meta.queue.reason (what the job waits behind) is not sent at start or on queued beats with the log off;
    position and timing still are. With the log on it is sent as before."""
    if log_off:
        env.write(AVM_TASK_LOG="off")
    _queue_for(monkeypatch, 3)
    assert env.run("run", "gpu", "--gpu", "--wait", "--beat", "0.2", "--", *sh("true")) == 0
    sent = _bodies(env)
    assert ("SECRET-REASON" in sent) is not log_off
    queued = [c["body"]["meta"]["queue"] for c in env.calls if c["path"].endswith("/beat") and c["body"].get("status") == "queued"]
    assert queued and all("position" in q for q in queued)


def test_gave_up_note_has_no_queue_reason_with_the_log_off(env, monkeypatch):
    """I-01: --max-wait giving up finishes the task with a note that leaves the queue reason out (log off)."""
    env.write(AVM_TASK_LOG="off")
    _queue_for(monkeypatch, 10 ** 6)
    assert env.run("run", "gpu", "--gpu", "--wait", "--max-wait", "1s", "--", *sh("true")) == avm.EXIT_GAVE_UP
    fin = [c["body"] for c in env.calls if c["path"].endswith("/finish")]
    assert fin and fin[-1]["note"].startswith("gave up after waiting") and "SECRET" not in _bodies(env)
