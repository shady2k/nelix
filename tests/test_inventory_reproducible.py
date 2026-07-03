"""bin/nelix-inventory must regenerate tests/golden/INVENTORY.md deterministically from TRACKED
inputs only (nelix-puf). Before this fix the generator also scanned a live, per-machine sessions
dir (~/.hermes/.../sessions), so its output varied by machine and the committed manifest could not
be reproduced. These tests pin the two properties that make it reproducible:

  1. the generator reads NO live/per-machine source — only the committed captures under
     tests/golden/claude/_regression/ (both `*.raw` and timed `*.capture`), and
  2. the invariants that used to resolve only from a live session now resolve from a COMMITTED
     capture — I4a (bare ❯) and I5 (ctrl+b panel) from the committed `s-039a61b4.raw` prefix, and
     I6a (numbered modal) from the committed `s-beb967e9.capture`.

The generator has no `.py` extension, so it is loaded via SourceFileLoader (same reason
tests/test_nelix_wrappers.py runs the bin scripts as subprocesses).
"""
import importlib.util
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GENERATOR = ROOT / "bin" / "nelix-inventory"
REGRESSION_DIR = ROOT / "tests" / "golden" / "claude" / "_regression"


@pytest.fixture(scope="module")
def gen():
    loader = SourceFileLoader("nelix_inventory", str(GENERATOR))
    spec = importlib.util.spec_from_loader("nelix_inventory", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_generator_source_has_no_live_sessions_path():
    """The reproducibility guarantee: the active code references neither the live sessions dir nor a
    SESSIONS_DIR constant — so the manifest cannot depend on per-machine state."""
    src = GENERATOR.read_text()
    assert ".hermes" not in src
    assert "SESSIONS_DIR" not in src


def test_committed_sources_are_all_tracked(gen):
    """Every scanned source lives under the committed _regression/ dir — never under $HOME/.hermes —
    and both raw dumps and timed captures are ingested."""
    sources = gen._committed_sources()
    names = {sid for sid, _p, _c, _r in sources}
    # the prefix (I4a/I5) and the timed modal capture (I6a) are both present and tracked
    assert "s-039a61b4" in names
    assert "s-beb967e9" in names
    for _sid, path, _c, _r in sources:
        assert path.parent == REGRESSION_DIR, path
        assert ".hermes" not in str(path)
        assert path.suffix in (".raw", ".capture")


def test_committed_sources_order_is_deterministic(gen):
    """Sorted by filename, so the scan order — and therefore the manifest — is stable across runs
    and machines."""
    src1 = gen._committed_sources()
    src2 = gen._committed_sources()
    assert src1 == src2
    paths = [path for _sid, path, _c, _r in src1]
    assert paths == sorted(paths)


def test_i4a_and_i5_resolve_from_committed_prefix(gen):
    """I4a (bare ❯, offset 896) and I5 (ctrl+b panel, offset 123904) resolve from the committed
    s-039a61b4.raw prefix — NOT from a live session."""
    sources = gen._committed_sources()

    sid, path, offset, _ = gen.scan_specific(
        ["s-039a61b4"], gen.pred_bare_prompt_no_footer, sources)
    assert sid == "s-039a61b4"
    assert path == REGRESSION_DIR / "s-039a61b4.raw"
    assert offset == 896

    sid, path, offset, _ = gen.scan_specific(
        ["s-039a61b4"], gen.pred_ctrl_b_panel, sources)
    assert sid == "s-039a61b4"
    assert path == REGRESSION_DIR / "s-039a61b4.raw"
    assert offset == 123904


def test_i6a_resolves_from_committed_capture(gen):
    """I6a (numbered modal) resolves from the committed timed capture s-beb967e9.capture, proving
    the generator ingests `*.capture` (read_capture) — not a live session."""
    sources = gen._committed_sources()
    sid, path, offset, frame = gen.scan_specific(
        ["s-6e9d8956", "s-7dbd7358", "s-beb967e9"], gen.pred_modal_menu, sources)
    assert sid == "s-beb967e9"
    assert path == REGRESSION_DIR / "s-beb967e9.capture"
    assert offset == 1456896
    assert gen.pred_modal_menu(frame)


def test_capture_ingest_reproduces_raw_bytes(gen):
    """`*.capture` ingest is lossless: concatenating the timed records reproduces the raw stream,
    so replaying the capture yields the same frames a raw dump would."""
    from daemon.capture import read_capture
    capture = REGRESSION_DIR / "s-beb967e9.capture"
    data = gen._source_bytes(capture)
    assert data == b"".join(chunk for _off, chunk in read_capture(capture))
    assert len(data) > 0
