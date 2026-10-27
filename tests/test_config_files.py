"""Config-file parsing shared by the hub (.env), the agent and avm (agent.conf), and where the agent keeps state.
Found while documenting audit round E: inline comments were kept as part of the value, so a pasted
`AVM_INTERVAL=5   # seconds` crashed the agent on float()."""
import os

import avalon_agent as a
import avm
from avalon_monitor import config as hub_config

LINES = 'AVM_INTERVAL=5   # seconds between samples\nAVM_TAGS="gpu # not a comment"\nAVM_TOKEN=avm_abc#def\n# full-line comment\n'


def test_agent_strips_inline_comments(tmp_path):
    p = tmp_path / "agent.conf"
    p.write_text(LINES)
    cfg = a.load_config(str(p))
    assert cfg["AVM_INTERVAL"] == "5" and float(cfg["AVM_INTERVAL"]) == 5.0
    assert cfg["AVM_TAGS"] == "gpu # not a comment"      # quoted: kept as written
    assert cfg["AVM_TOKEN"] == "avm_abc#def"             # '#' without a space before it is part of the value


def test_avm_strips_inline_comments(tmp_path):
    p = tmp_path / "agent.conf"
    p.write_text(LINES)
    vals = avm._read_conf(str(p))
    assert vals["AVM_INTERVAL"] == "5" and vals["AVM_TAGS"] == "gpu # not a comment" and vals["AVM_TOKEN"] == "avm_abc#def"


def test_hub_env_file_strips_inline_comments(tmp_path, monkeypatch):
    p = tmp_path / "monitor.env"
    p.write_text("AVM_TEST_RETENTION=60   # days\nAVM_TEST_QUOTED='a # b'\n")
    monkeypatch.delenv("AVM_TEST_RETENTION", raising=False)
    monkeypatch.delenv("AVM_TEST_QUOTED", raising=False)
    hub_config._load_env_file(str(p))
    assert os.environ["AVM_TEST_RETENTION"] == "60" and os.environ["AVM_TEST_QUOTED"] == "a # b"


def test_state_dir_falls_back_to_the_agent_dir(tmp_path, monkeypatch):
    """Root installs: the unit makes /etc read-only and only the agent's own dir writable (E-15 fix must still apply)."""
    ro = tmp_path / "etc"
    ro.mkdir()
    (ro / "agent.conf").write_text("")
    ro.chmod(0o555)
    own = tmp_path / "opt"
    own.mkdir()
    monkeypatch.setattr(a, "_SELF_PATH", str(own / "avalon_agent.py"))
    try:
        cfg = dict(a.DEFAULTS)
        if os.access(str(ro), os.W_OK):   # running as root: permissions don't bite, nothing to test
            return
        assert a.state_path(cfg, str(ro / "agent.conf"), "remote-config.json") == str(own / "remote-config.json")
        assert a.state_path(dict(cfg, AVM_STATE_DIR=str(tmp_path)), None, "x") == str(tmp_path / "x")
    finally:
        ro.chmod(0o755)


def test_local_update_keys_add_to_the_built_in_ones():
    """A host trusting an extra key must still accept releases signed with the built-in (rotated) key."""
    keys = a.release_keys(dict(a.DEFAULTS, AVM_UPDATE_PUBKEYS="11" * 32))
    assert bytes.fromhex("11" * 32) in keys and all(bytes.fromhex(k) in keys for k in a.RELEASE_PUBKEYS)
