# tests/test_invariants_registry.py
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests.golden.invariants import INVARIANTS, Invariant

_EXPECTED_IDS = {
    "I1", "I2a", "I2b", "I3", "I4a", "I4b", "I5", "I6a", "I6b",
    "I7", "I8", "I9", "I-R1", "I-R2", "I-AM", "I-BC",
}

_EXPECTED = {
    # id: (tier, kind, bug_commit)
    "I1": (1, "frame", "3f7f6d7"),
    "I2a": (1, "frame", "cd3352d"),
    "I3": (1, "frame", "e80fbcc"),
    "I4a": (1, "frame", "68d6c7c"),
    "I5": (1, "frame", "f3d89dc"),
    "I6a": (1, "frame", "8ecb2f5"),
    "I-R1": (1, "frame", "9164bf6"),
    "I-R2": (1, "frame", "f24dd9e"),
    "I-AM": (1, "frame", "d874852"),
    "I-BC": (1, "frame", "c52c5cc"),
    "I2b": (2, "sequence", "cd3352d"),
    "I4b": (2, "sequence", "68d6c7c"),
    "I7": (2, "sequence", "6e0b8c6"),
    "I8": (2, "sequence", "6de482c"),
    "I6b": (3, "session", "8ecb2f5"),
    "I9": (3, "synthetic", "2f7fac4"),
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

def test_tier_kind_commit_mapping_is_frozen():
    got = {i.id: (i.tier, i.kind, i.bug_commit) for i in INVARIANTS}
    assert got == _EXPECTED
