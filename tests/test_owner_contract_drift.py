"""The core's owner charset MUST equal nelix_contracts'.

daemon/owner.py restates `_OWNER_RE` instead of importing nelix_contracts, deliberately:
nelix_contracts is a TEST-ONLY package (pyproject.toml keeps the core's runtime closure at
wasmtime alone, so an installed wheel has no nelix_contracts to import), and its
validate_session_id would reject this daemon's own `s-<8hex>` session ids outright. The two id
worlds are genuinely separate today.

The owner CHARSET, though, is one rule in two places, and that is a drift risk with teeth: the
store derives a session's identity from its assigned start row (nelix-555), so once nelix-9a4.4
wires the store in, an owner the CORE accepted but the STORE rejects is a session that starts and
then cannot be recorded. A copied regex hides that until then. This test refuses to let it.

It compares BEHAVIOUR over a corpus, not the pattern strings: two regexes can be spelled
differently and mean the same thing, and it is the meaning that has to match.
"""
import pytest

from nelix_contracts.ids import InvalidId, validate_owner_id
from daemon import owner

# Values chosen to sit on every boundary the two rules could drift across.
CORPUS = [
    "o", "hermes", "claude-code", "Team.A", "a:b", "a_b", "a.b", "a-b",
    "o-0123456789abcdef0123456789abcdef",
    "x" * 127, "x" * 128, "x" * 129,          # the length cap, either side
    "", " ", "  hermes  ", "has space",
    "-lead", ".lead", ":lead", "_lead",       # leading char must be alnum
    "0lead", "Zlead",
    "tab\there", "nl\nhere", "nul\0here",
    "slash/here", "back\\here", "quo'te", 'dq"ote', "semi;colon", "amp&and",
    "plus+here", "at@here", "hash#here", "pct%here", "star*here",
    "héllo", "emoji😀", "ideo漢",
]


def _core_accepts(v):
    try:
        owner.validate(v)
        return True
    except owner.OwnerRejected:
        return False


def _store_accepts(v):
    try:
        validate_owner_id(v)
        return True
    except InvalidId:
        return False


@pytest.mark.parametrize("value", CORPUS)
def test_core_and_store_agree_on_owner_shape(value):
    assert _core_accepts(value) == _store_accepts(value), (
        f"owner_id {value!r}: core accepts={_core_accepts(value)} but "
        f"store accepts={_store_accepts(value)} — daemon/owner.py._OWNER_RE has drifted from "
        f"nelix_contracts.ids._OWNER_RE. An owner the core starts a session for but the store "
        f"cannot record is a session that dies on the way to disk (nelix-9a4.4)."
    )


@pytest.mark.parametrize("value", [None, 7, True, b"hermes", ["x"], {}])
def test_core_and_store_agree_on_non_strings(value):
    assert _core_accepts(value) is False and _store_accepts(value) is False


def test_the_corpus_actually_exercises_both_verdicts():
    # A corpus that is all-accept or all-reject would make the agreement test vacuous.
    verdicts = {_core_accepts(v) for v in CORPUS}
    assert verdicts == {True, False}
