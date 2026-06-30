"""Tier-1 sidecar harness (nelix-5gc Task 3).

Provides:
  load_expectation(yaml_path) -> dict   — parse a <name>.yaml sidecar
  build_ctx(expect) -> ObservationCtx  — construct ctx from sidecar or plain dict
  assert_observation(obs, expect, *, fixture_id) -> None  — rich assertion
"""
import yaml
from daemon.observation import ObservationCtx

# Every key that assert_observation recognises.  A key absent from this set is
# almost certainly a typo that would otherwise silently skip the assertion.
_KNOWN_EXPECT_KEYS = frozenset({
    "prompt_kind",
    "submitted_echo_present",
    "ask_mode",
    "busy_reason",
    "heartbeat_present",
    "options_ids",
    "options",
    "affordances_include",
    "affordances_exclude",
    "semantic_fp_nonempty",
})


def load_expectation(yaml_path):
    """Parse a YAML sidecar file and return its raw dict."""
    return yaml.safe_load(yaml_path.read_text())


def build_ctx(expect):
    """Build an ObservationCtx from a sidecar dict or a plain ctx dict.

    Accepts two calling conventions:
      - full sidecar dict with a ``ctx`` key (from load_expectation)
      - bare ctx dict (from inline test code)
    """
    c = (expect or {}).get("ctx", {}) if "ctx" in (expect or {}) else (expect or {})
    return ObservationCtx(
        last_submitted_text=c.get("last_submitted_text"),
        child_alive=c.get("child_alive", True),
        exit_code=c.get("exit_code"),
    )


def assert_observation(obs, expect, *, fixture_id):
    """Assert that *obs* matches every key present in *expect*.

    Only keys that appear in *expect* are checked; omitted keys are not asserted.

    Recognised keys (all optional):
      prompt_kind            str — obs.prompt_kind must equal this
      submitted_echo_present bool
      ask_mode               bool
      busy_reason            str | null
      heartbeat_present      bool — obs.heartbeat.present must equal this
      options_ids            list[str] — [o.id for o in obs.options] must equal this
      options                list[{id,label}] — [(o.id,o.label) for o in obs.options] must equal
                             [(e["id"],e["label"]) for e in expect["options"]]
      affordances_include    list[str] — each must be in obs.affordances
      affordances_exclude    list[str] — each must NOT be in obs.affordances
      semantic_fp_nonempty   bool — if true, obs.semantic_fp must be non-empty
    """
    def fail(msg):
        raise AssertionError(f"[{fixture_id}] {msg}")

    for k in expect:
        if k not in _KNOWN_EXPECT_KEYS:
            raise AssertionError(f"[{fixture_id}] unknown expect-key {k!r}")

    if "prompt_kind" in expect and obs.prompt_kind != expect["prompt_kind"]:
        fail(f"prompt_kind {obs.prompt_kind!r} != {expect['prompt_kind']!r}")
    if "submitted_echo_present" in expect and obs.submitted_echo_present != expect["submitted_echo_present"]:
        fail(f"submitted_echo_present {obs.submitted_echo_present} != {expect['submitted_echo_present']}")
    if "ask_mode" in expect and obs.ask_mode != expect["ask_mode"]:
        fail(f"ask_mode {obs.ask_mode} != {expect['ask_mode']}")
    if "busy_reason" in expect and obs.busy_reason != expect["busy_reason"]:
        fail(f"busy_reason {obs.busy_reason!r} != {expect['busy_reason']!r}")
    if "heartbeat_present" in expect and obs.heartbeat.present != expect["heartbeat_present"]:
        fail(f"heartbeat.present {obs.heartbeat.present} != {expect['heartbeat_present']}")
    if "options_ids" in expect:
        got = [o.id for o in obs.options]
        if got != expect["options_ids"]:
            fail(f"options_ids {got} != {expect['options_ids']}")
    if "options" in expect:
        got_pairs = [(o.id, o.label) for o in obs.options]
        exp_pairs = [(e["id"], e["label"]) for e in expect["options"]]
        if got_pairs != exp_pairs:
            fail(f"options (id, label) pairs {got_pairs} != {exp_pairs}")
    for a in expect.get("affordances_include", []):
        if a not in obs.affordances:
            fail(f"affordance {a!r} missing from {set(obs.affordances)}")
    for a in expect.get("affordances_exclude", []):
        if a in obs.affordances:
            fail(f"affordance {a!r} should be absent")
    if expect.get("semantic_fp_nonempty") and not obs.semantic_fp:
        fail("semantic_fp is empty")
