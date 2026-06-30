"""Tests for the Tier-1 sidecar harness (nelix-5gc Task 3).

Verifies that load_expectation / build_ctx / assert_observation behave correctly
before any real sidecar files exist on disk.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests.golden._harness import load_expectation, build_ctx, assert_observation
from daemon.drivers.claude import ClaudeDriver


def test_assert_observation_passes_on_matching_expect(tmp_path):
    frame = "Here is my answer.\n❯ \n⏵⏵ ask mode (shift+tab to cycle)"
    expect = {"prompt_kind": "free_text", "affordances_include": ["accepts_text_input"],
              "ask_mode": True, "semantic_fp_nonempty": True}
    obs = ClaudeDriver().observe(frame, build_ctx({}))
    assert_observation(obs, expect, fixture_id="smoke")  # must not raise


def test_assert_observation_fails_on_wrong_prompt_kind():
    frame = "working… esc to interrupt"
    expect = {"prompt_kind": "free_text"}
    obs = ClaudeDriver().observe(frame, build_ctx({}))
    import pytest
    with pytest.raises(AssertionError):
        assert_observation(obs, expect, fixture_id="smoke")
