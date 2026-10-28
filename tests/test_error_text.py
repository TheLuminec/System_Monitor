"""Error text (behavioural, run with node): the dashboard's api() showed a 422 from the check editor as "422 [object
Object]", because FastAPI's `detail` for a validation error is a list of objects (owner report, 2026-10-28). It now
shows "field: message; ...". The hub's own 422 bodies are fed through the real core.js api() with a fake fetch."""
import json
import shutil
from pathlib import Path

import pytest

from conftest import WRITE
from test_alert_mode import _agent_add

STATIC = Path(__file__).resolve().parents[1] / "server" / "avalon_monitor" / "static"
needs_node = pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")

JS = """
import { api, errorText } from %(core)s;
const bodies = %(bodies)s;
const out = {thrown: [], text: {}};
for (const b of bodies) {
  globalThis.fetch = async () => ({ ok: false, status: b.status, statusText: "Unprocessable Entity", json: async () => b.body });
  try { await api("/x"); out.thrown.push(null); } catch (e) { out.thrown.push(e.message); }
}
out.text.str = errorText("plain words");
out.text.obj = errorText({msg: "bad mode"});
out.text.other = errorText({code: 7});
out.text.none = errorText(null);
console.log(JSON.stringify(out));
"""


def _node(js):
    import subprocess
    r = subprocess.run(["node", "--input-type=module", "-e", js], capture_output=True, text=True, timeout=30, check=True)
    return json.loads(r.stdout)


@needs_node
def test_check_editor_422_is_readable(tunnel, agent):
    """Error text: the hub's real 422s for a bad check edit (an unknown alert mode; a wrong field type) come out as
    readable text in the thrown error - never "[object Object]"."""
    c = _agent_add(agent, "/mnt/err1")
    bad_mode = tunnel.patch(f"/api/v1/checklist/{c['id']}", json={"alert_mode": "bogus"}, headers=WRITE)
    bad_type = tunnel.patch(f"/api/v1/checklist/{c['id']}", json={"cooldown_min": "soon"}, headers=WRITE)
    bodies = [{"status": r.status_code, "body": r.json()} for r in (bad_mode, bad_type)]
    assert [b["status"] for b in bodies] == [422, 422], bodies
    out = _node(JS % {"core": json.dumps((STATIC / "core.js").as_uri()), "bodies": json.dumps(bodies)})
    for msg in out["thrown"]:
        assert msg and msg.startswith("422 ") and "[object Object]" not in msg, out
        assert len(msg) > len("422 ") + 3, out
    assert "cooldown_min" in out["thrown"][1], out   # the field the operator got wrong is named


@needs_node
def test_error_text_shapes():
    """Error text: a string passes through, an object uses msg, anything else is JSON, nothing gives ""."""
    out = _node(JS % {"core": json.dumps((STATIC / "core.js").as_uri()), "bodies": "[]"})
    assert out["text"] == {"str": "plain words", "obj": "bad mode", "other": '{"code":7}', "none": ""}
