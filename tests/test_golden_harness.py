"""Tests for the Tier-1 sidecar harness (nelix-5gc Task 3).

Verifies that load_expectation / build_ctx / assert_observation behave correctly
before any real sidecar files exist on disk.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pytest
import yaml
from tests.golden._harness import load_expectation, build_ctx, assert_observation
from daemon.drivers.claude import ClaudeDriver


def test_assert_observation_passes_on_matching_expect(tmp_path):
    frame = "Here is my answer.\n❯ \n⏵⏵ ask mode (shift+tab to cycle)"
    expect = {"prompt_kind": "free_text", "affordances_include": ["accepts_text_input"],
              "semantic_fp_nonempty": True}
    obs = ClaudeDriver().observe(frame, build_ctx({}))
    assert_observation(obs, expect, fixture_id="smoke")  # must not raise


def test_assert_observation_fails_on_wrong_prompt_kind():
    frame = "working… esc to interrupt"
    expect = {"prompt_kind": "free_text"}
    obs = ClaudeDriver().observe(frame, build_ctx({}))
    with pytest.raises(AssertionError):
        assert_observation(obs, expect, fixture_id="smoke")


# ---------------------------------------------------------------------------
# Table-driven: for each documented sidecar key, (a) correct value passes and
# (b) wrong value raises AssertionError.  Uses real ClaudeDriver().observe()
# outputs on small literal frames — no fabricated Observation objects.
# ---------------------------------------------------------------------------

_FREE_TEXT_FRAME = "Hello there\n❯ \nshift+tab to cycle"
_WORKING_FRAME   = "esc to interrupt"
_BASH_FRAME      = "⏺ Bash(ls -la)"
_MODAL_FRAME     = "Pick one:\n❯ 1. Option A\n2. Option B"
_ECHO_FRAME      = "❯ hello world\nshift+tab to cycle"

# Each row: (key, frame, ctx_dict, good_expect, bad_expect)
# bad_expect=None when the wrong direction is not demonstrable with real frames
# (semantic_fp is always a non-empty hex digest, so semantic_fp_nonempty=True never fails).
_KEY_TABLE = [
    (
        "prompt_kind",
        _FREE_TEXT_FRAME, {},
        {"prompt_kind": "free_text"},
        {"prompt_kind": "none"},
    ),
    (
        "submitted_echo_present",
        _ECHO_FRAME, {"last_submitted_text": "hello world"},
        {"submitted_echo_present": True},
        {"submitted_echo_present": False},
    ),
    (
        "busy_reason",
        _BASH_FRAME, {},
        {"busy_reason": "running_command"},
        {"busy_reason": None},
    ),
    (
        "heartbeat_present",
        _WORKING_FRAME, {},
        {"heartbeat_present": True},
        {"heartbeat_present": False},
    ),
    (
        "options_ids",
        _MODAL_FRAME, {},
        {"options_ids": ["1", "2"]},
        {"options_ids": ["1"]},
    ),
    (
        "options",
        _MODAL_FRAME, {},
        {"options": [{"id": "1", "label": "Option A"}, {"id": "2", "label": "Option B"}]},
        {"options": [{"id": "1", "label": "Enrich all three"}, {"id": "2", "label": "Option B"}]},
    ),
    (
        "affordances_include",
        _FREE_TEXT_FRAME, {},
        {"affordances_include": ["accepts_text_input"]},
        {"affordances_include": ["interrupt_available"]},
    ),
    (
        "affordances_exclude",
        _FREE_TEXT_FRAME, {},
        {"affordances_exclude": ["interrupt_available"]},
        {"affordances_exclude": ["accepts_text_input"]},
    ),
    (
        "semantic_fp_nonempty",
        _FREE_TEXT_FRAME, {},
        {"semantic_fp_nonempty": True},
        None,  # fp is always a non-empty sha256 hex digest; bad direction not reachable
    ),
]


@pytest.mark.parametrize(
    "key,frame,ctx_dict,good_expect,bad_expect",
    _KEY_TABLE,
    ids=[row[0] for row in _KEY_TABLE],
)
def test_each_sidecar_key_pass_and_fail(key, frame, ctx_dict, good_expect, bad_expect):
    ctx = build_ctx(ctx_dict)
    obs = ClaudeDriver().observe(frame, ctx)

    # (a) correct expect value — must NOT raise
    assert_observation(obs, good_expect, fixture_id=f"{key}/pass")

    # (b) wrong expect value — MUST raise AssertionError
    if bad_expect is not None:
        with pytest.raises(AssertionError):
            assert_observation(obs, bad_expect, fixture_id=f"{key}/fail")


def test_unknown_expect_key_raises():
    """A typo in an expect key (e.g. heartbeat_presnt) must raise, not silently skip."""
    frame = _FREE_TEXT_FRAME
    obs = ClaudeDriver().observe(frame, build_ctx({}))
    with pytest.raises(AssertionError, match="heartbeat_presnt"):
        assert_observation(obs, {"heartbeat_presnt": True}, fixture_id="typo-test")


def test_load_expectation_round_trips(tmp_path):
    """load_expectation must parse the YAML sidecar and return the matching dict."""
    data = {
        "ctx": {"last_submitted_text": None, "child_alive": True},
        "expect": {"prompt_kind": "free_text"},
    }
    p = tmp_path / "fixture.yaml"
    p.write_text(yaml.safe_dump(data))
    loaded = load_expectation(p)
    assert loaded == data
