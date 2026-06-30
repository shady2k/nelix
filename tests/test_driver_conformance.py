"""Driver-conformance harness (spec §5.6): assert the claude driver's observe() against golden
frames captured from real CLI sessions. When Claude Code drifts (e.g. it drops a marker the driver
keys on), this goes RED in dev — instead of the daemon misclassifying a live agent (nelix-48o).

Golden frames live in tests/golden/claude/<expected>/*.txt — the directory name is the OLD six-state
label; it is remapped to the new prompt_kind vocabulary (working->none, idle_prompt->free_text,
permission_prompt->{permission_choice|modal_choice}). Refresh with bin/nelix-capture; see README.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from daemon.drivers.claude import ClaudeDriver   # noqa: E402
from daemon.drivers.base import Driver           # noqa: E402


# ---- protocol-shape conformance (spec §5.6) --------------------------------------------
# The driver contract is REPLACED outright: observe() is the sole classification contract;
# classify() and the folded predicates are gone.
_REMOVED = ("classify", "is_accepting_input", "is_modal_choice", "is_ask_mode",
            "input_submission_present")
_REQUIRED = ("observe", "normalize_frame", "is_transcript_volatile",
             "format_submission", "submit_text", "select_option", "interrupt")


def test_driver_protocol_has_observe_not_classify():
    for name in _REQUIRED:
        assert hasattr(Driver, name), f"Driver protocol must declare {name}()"
    for name in _REMOVED:
        assert not hasattr(Driver, name), f"Driver protocol must NOT declare {name}()"


def test_registry_fails_closed_for_unmigrated_driver():
    # A driver that does not implement observe() must be REJECTED at instantiation — the registry
    # fails closed rather than letting the core call a missing classification contract (NIT-17).
    from daemon.drivers import register, get_driver, DRIVERS

    @register("_stub_no_observe")
    class _Stub:
        ask_mode_toggle = ""
        command_prefixes = ()
        submit_key = "\r"
        # no observe()

    try:
        with pytest.raises(TypeError):
            get_driver("_stub_no_observe")
    finally:
        DRIVERS.pop("_stub_no_observe", None)


def test_every_registered_driver_observes():
    # Every registered driver must return an Observation from observe() for any frame.
    from daemon.drivers import DRIVERS, get_driver
    from daemon.observation import Observation
    for name in DRIVERS:
        drv = get_driver(name)
        o = drv.observe("hello\n❯ \n⏵⏵ ask mode (shift+tab to cycle)", _CTX)
        assert isinstance(o, Observation), f"{name}.observe() must return an Observation"

GOLDEN = Path(__file__).resolve().parent / "golden" / "claude"

from daemon.observation import ObservationCtx        # noqa: E402
from tests.golden._harness import load_expectation, build_ctx, assert_observation  # noqa: E402

_CTX = ObservationCtx(last_submitted_text=None, child_alive=True, exit_code=None)

# The golden directory name (old six-state) maps to the allowed new prompt_kind(s).
# Used only for the legacy (no-sidecar) path.
_REMAP = {
    "working": {"none"},
    "idle_prompt": {"free_text"},
    "permission_prompt": {"permission_choice", "modal_choice"},
}


def _cases():
    """Discover all golden .txt frames under tests/golden/claude/<category>/.

    For each frame:
      - if a sibling <name>.yaml sidecar exists  → sidecar-driven case
      - otherwise                                → legacy prompt-kind-only case

    New category dirs are discovered automatically (no hardcoded CATEGORIES list).
    """
    cases = []
    for cat_dir in sorted(GOLDEN.iterdir()):
        if not cat_dir.is_dir() or cat_dir.name.startswith("_"):
            continue  # skip _regression and other non-category dirs
        txt_files = sorted(cat_dir.glob("*.txt"))
        assert txt_files, (
            f"no golden frames in {cat_dir} — a category must not silently pass empty"
        )
        for f in txt_files:
            sidecar = f.with_suffix(".yaml")
            if sidecar.exists():
                data = load_expectation(sidecar)
                cases.append(("sidecar", cat_dir.name, f, data))
            else:
                cases.append(("legacy", cat_dir.name, f, None))
    return cases


_CASES = _cases()


@pytest.mark.parametrize(
    "mode,cat,path,sidecar",
    _CASES,
    ids=[str(p.relative_to(GOLDEN)) for _, _, p, _ in _CASES],
)
def test_claude_observe_matches_golden(mode, cat, path, sidecar):
    frame = path.read_text()
    fixture_id = str(path.relative_to(GOLDEN))

    if mode == "sidecar":
        ctx = build_ctx(sidecar)
        o = ClaudeDriver().observe(frame, ctx)
        assert_observation(o, sidecar["expect"], fixture_id=fixture_id)
    else:
        # Legacy path: directory name maps to allowed prompt_kind set.
        # Kept for the 9 existing .txt frames that have no sidecar yet (back-compat).
        o = ClaudeDriver().observe(frame, _CTX)
        allowed = _REMAP.get(cat, set())
        if not allowed:
            pytest.skip(f"no _REMAP entry for category {cat!r} — add a sidecar to enable assertions")
        if o.prompt_kind not in allowed:
            head = "\n".join(ln for ln in frame.splitlines() if ln.strip())[:400]
            pytest.fail(
                f"{fixture_id}: expected prompt_kind in {allowed}, "
                f"got {o.prompt_kind!r}\n--- first non-blank lines ---\n{head}"
            )
