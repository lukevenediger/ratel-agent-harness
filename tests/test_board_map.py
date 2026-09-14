"""Golden tests over the map's pure client model.

mapLayout, mapEdges and mapState live inside board.html and are the only parts
of the map the suite can exercise without a browser: they take strings/arrays
and return plain objects. The functions are sliced out of the page at test time
(never copied — a copy drifts) and run under node, so a change that moves a
coordinate or a weight has to be deliberate.

Needs `node`; skipped with a reason when it is absent, the same way the zellij
tests are.
"""
import json
import subprocess
from pathlib import Path

import pytest

import ratel.board as board_mod
from ratel.clan.session import ACTIVITY_STATES

pytestmark = pytest.mark.node

BOARD_HTML = Path(board_mod.__file__).parent / "static" / "board.html"
START = "const MAP_STATES = "
END = "\n/* ---- map paint ---- */"

HARNESS = """
%(model)s
const c = JSON.parse(require("fs").readFileSync(0, "utf8"));
process.stdout.write(JSON.stringify({
  states: MAP_STATES,
  layoutA: mapLayout(c.names),
  layoutB: mapLayout(c.names),
  edges: mapEdges(c.msgs || [], c.names, c.now, c.states || {}),
  state: mapState(c.state, c.confidence),
  label: stateLabel(c.state, c.headless),
}));
"""


def run(case, tmp_path):
    src = BOARD_HTML.read_text()
    model = src[src.index(START):src.index(END, src.index(START)) + len(END)]
    script = tmp_path / "map_harness.js"
    script.write_text(HARNESS % {"model": model})
    p = subprocess.run(["node", str(script)], input=json.dumps(case),
                       capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout)


NOW = "2026-09-11T17:05:00Z"


def _msg(mid, sender, ts, mentions=None, parent=None):
    return {"id": mid, "from": sender, "ts": ts, "mentions": mentions or [],
            "parent": parent, "text": ""}


def test_layout_is_a_pure_function_of_the_names(tmp_path):
    names = ["orchestrator", "developer", "reviewer", "security"]
    got = run({"names": names}, tmp_path)
    assert got["layoutA"] == got["layoutB"]          # identical for the same names
    assert len(got["layoutA"]) == len(names)
    hub = next(p for p in got["layoutA"] if p["hub"])
    assert hub["name"] == "orchestrator" and hub["x"] == 100 and hub["y"] == 100
    assert [p["name"] for p in got["layoutA"]] == names   # clan.toml order


def test_layout_without_an_orchestrator_still_draws_everyone(tmp_path):
    got = run({"names": ["a", "b", "c"]}, tmp_path)
    assert [p["name"] for p in got["layoutA"]] == ["a", "b", "c"]
    assert not any(p["hub"] for p in got["layoutA"])


def test_edges_are_addressing_over_a_thirty_minute_window(tmp_path):
    msgs = [
        _msg("m1", "a", "2026-09-11T17:04:00Z", mentions=["b"]),
        _msg("m2", "c", "2026-09-11T17:03:00Z", parent="m1"),
        _msg("m3", "b", "2026-09-11T17:02:00Z", mentions=["b"]),      # self: dropped
        _msg("m4", "a", "2026-09-11T17:01:00Z", mentions=["ghost"]),  # not a node: dropped
        _msg("m5", "a", "2026-09-11T16:20:00Z", mentions=["b"]),      # 45 min old: dropped
    ]
    got = run({"names": ["a", "b", "c"], "msgs": msgs, "now": NOW,
               "states": {"b": "busy"}}, tmp_path)
    edges = {(e["from"], e["to"]): e for e in got["edges"]}
    assert set(edges) == {("a", "b"), ("c", "a")}
    assert edges[("a", "b")]["weight"] == 1 and edges[("a", "b")]["waiting"] is True
    assert edges[("c", "a")]["waiting"] is False


def test_a_quiet_target_is_not_waiting(tmp_path):
    msgs = [_msg("m1", "a", "2026-09-11T17:04:00Z", mentions=["b"])]
    got = run({"names": ["a", "b"], "msgs": msgs, "now": NOW,
               "states": {"b": "idle"}}, tmp_path)
    assert got["edges"][0]["waiting"] is False


def test_awaiting_operator_owes_a_reply_and_reads_as_two_words(tmp_path):
    """A role blocked on a dialog still owes its mentioner a reply — the edge
    keeps `waiting` — and the operator reads a label, not a slug."""
    msgs = [_msg("m1", "a", "2026-09-11T17:04:00Z", mentions=["b"])]
    got = run({"names": ["a", "b"], "msgs": msgs, "now": NOW,
               "states": {"b": "awaiting-operator"},
               "state": "awaiting-operator", "confidence": "measured", "headless": False},
              tmp_path)
    assert got["edges"][0]["waiting"] is True
    assert got["state"] == {"state": "awaiting-operator", "confidence": "measured",
                            "weak": False}
    assert got["label"] == "awaiting operator"


def test_js_state_vocabulary_equals_the_python_one(tmp_path):
    got = run({"names": ["a"]}, tmp_path)
    assert got["states"] == list(ACTIVITY_STATES)


def test_map_state_drops_an_unknown_state(tmp_path):
    got = run({"names": ["a"], "state": "<script>", "confidence": "certain"}, tmp_path)
    assert got["state"] == {"state": "offline", "confidence": "weak", "weak": True}


def test_idle_reads_as_quiet_for_an_interactive_role(tmp_path):
    """The issue's rule: an interactive role with a tab is never labelled
    'idle' — nothing on disk proves its turn ended. The wire vocabulary stays
    `idle` (pinned by the equality test); only the operator-facing label
    changes, and a headless role keeps 'idle' because its round.pid is real."""
    assert run({"names": ["a"], "state": "idle", "headless": False}, tmp_path)["label"] \
        == "no activity measured"
    assert run({"names": ["a"], "state": "idle", "headless": True}, tmp_path)["label"] == "idle"
    assert run({"names": ["a"], "state": "busy", "headless": False}, tmp_path)["label"] == "busy"
