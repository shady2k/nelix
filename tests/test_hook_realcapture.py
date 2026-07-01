"""Real-capture hook-event replay — regression-lock the 3 screen-scraping bugs by REPLAYING recorded
hook sequences (ground truth) through the REAL Session.on_hook + _loop drain.

Each `.jsonl` fixture under tests/golden/claude/_hooks/ is a sequence of raw Claude hook payloads. A
LIVE hook recording was not available to the author, so the payloads are SYNTHESIZED from the real
event ORDER documented in the design spec (§5 "Hook -> state map"/§1) and the sibling
tests/test_bg_subagent_realcapture.py capture — each fixture's header comment states this provenance
(honesty about synthesis). `replay_hooks` feeds them to a live Session in order — exactly the path a
real `curl` from a hook drives — and we assert the control_state the ENGINE derives, plus (the
auto-`exit` regression) that reaching `idle` NEVER writes to the PTY.

  done_idle   -> the s-326ae16b bug: a finished turn read as waiting_for_user + `exit` typed into the
                 box. Hooks make it `idle` (non-respondable) and the Session types NOTHING.
  modal_ask   -> a numbered AskUserQuestion modal misread as free_text. Hooks make it waiting_for_user.
  bg_subagent -> the s-039a61b4 flap: a running background subagent read as waiting_for_user ~35x.
                 Hooks keep it `busy` for every mid-flight event; it goes `idle` only on the final Stop.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests._session_replay import replay_session, replay_hooks   # noqa: E402

_HOOKS = Path(__file__).resolve().parent / "golden" / "claude" / "_hooks"
# A neutral live "working" frame. hook_mode goes active on the first hook, so the screen path is
# suppressed regardless — the frame only has to keep the handle alive and never itself publish.
_WORKING_FRAME = "✦ Working… (esc to interrupt)"


def _sess(tmp_path):
    # step=0.0: the injected FakeClock never advances across the replay, so no lost-Stop / grace
    # window can fire — the assertions isolate the hook state machine, not a timeout.
    return replay_session(tmp_path, [_WORKING_FRAME], step=0.0)


def test_done_idle_sequence_yields_idle_not_exit(tmp_path):
    # UserPromptSubmit -> PreToolUse/PostToolUse (tool work) -> Stop.
    sess, ev = _sess(tmp_path)
    trail = replay_hooks(sess, _HOOKS / "done_idle.jsonl")
    snap = sess.snapshot()
    assert snap["control_state"] == "idle"
    assert snap["decision"]["kind"] == "idle"
    assert snap["decision"]["requires_response"] is False
    assert snap["pending"] is False
    # The regression that started this whole plan: reaching idle must type NOTHING (never `exit`/quit).
    assert sess._handle.writes == []
    assert trail[-1] == ("Stop", "idle")
    assert not sess._closing                       # a completed session STAYS ALIVE


def test_modal_ask_sequence_yields_waiting(tmp_path):
    # ... -> PreToolUse[AskUserQuestion] (modal up, turn PAUSED — no Stop while the question pends).
    sess, ev = _sess(tmp_path)
    replay_hooks(sess, _HOOKS / "modal_ask.jsonl")
    snap = sess.snapshot()
    assert snap["control_state"] == "awaiting_user"
    assert snap["decision"]["kind"] == "waiting_for_user"
    assert snap["decision"]["requires_response"] is True
    assert snap["decision"]["prompt_kind"] == "modal_choice"
    assert sess._handle.writes == []               # a pause types nothing either


def test_bg_subagent_never_idle_midflight(tmp_path):
    # working spans (main agent + background subagent), NO Stop/AskUserQuestion until the end.
    sess, ev = _sess(tmp_path)
    trail = replay_hooks(sess, _HOOKS / "bg_subagent.jsonl")
    # The LAST event is the final Stop -> idle; EVERY earlier event stays busy (never idle/waiting):
    # the bg-subagent flap locked out by construction.
    assert trail[-1] == ("Stop", "idle")
    mid = [state for (_, state) in trail[:-1]]
    assert len(mid) >= 8, mid                       # a meaningful mid-flight span, not a 1-event stub
    assert all(state == "busy" for state in mid), mid
    assert not any(state in ("idle", "awaiting_user") for state in mid)
    assert sess._handle.writes == []               # never typed anything mid-flight
