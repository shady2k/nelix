# tests/test_invariants_registry.py
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests.golden.invariants import INVARIANTS, Invariant

_EXPECTED_IDS = {
    "I1", "I2a", "I2b", "I3", "I4a", "I4b", "I5", "I6a", "I6b",
    "I7", "I8", "I9", "I-R1", "I-R2", "I-AM", "I-BC",
}

def test_every_spec_invariant_is_registered():
    ids = {i.id for i in INVARIANTS}
    assert ids == _EXPECTED_IDS, f"missing {_EXPECTED_IDS - ids}, extra {ids - _EXPECTED_IDS}"

def test_each_invariant_is_well_formed():
    for i in INVARIANTS:
        assert isinstance(i, Invariant)
        assert i.tier in (1, 2, 3)
        assert i.kind in ("frame", "sequence", "session", "synthetic")
        assert i.bug_commit and i.description
