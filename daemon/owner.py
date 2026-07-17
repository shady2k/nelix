"""`owner_id` — the correctness namespace that keeps two harnesses on one daemon apart.

Two harnesses on ONE daemon is the point of the product: Hermes drives it from a phone while
Claude Code drives it locally, against the same sessions. Without an owner, a board read returns
EVERY session, so the reading harness adopts every session it sees, arms a waiter for each, and
can answer another harness's decisions. `owner_id` partitions that namespace.

IT IS NOT AUTHENTICATION, AND THE NAME MUST NOT OVERPROMISE. Every caller reaches this daemon
over a 0700/0600 unix socket (or a shared token) as ONE uid, so any local caller can assert any
owner id it likes and get that owner's sessions. This buys CORRECTNESS — two cooperating
harnesses cannot trip over each other by accident — not confinement. A caller that lies is out
of scope, exactly as `daemon/rpc_server.py`'s transport auth intends: the uid IS the boundary.
The executor-facing `/hook` and `/message` routes are deliberately NOT owner-gated; they carry a
per-session secret, which is a STRONGER check than an owner id, not a weaker one.

The rules, in one place:
  * The DURABLE RECORD ON DISK IS THE ONLY ORACLE. Every ownership decision — live session,
    terminal relay, transcript on disk — reads `sessions/<sid>/owner.json`. One authority
    cannot disagree with itself, and it survives a daemon restart (durable, not a lease).
  * FAIL CLOSED. Missing, unreadable or malformed record => `owner_of` is None => nobody owns
    the session and every caller-facing route treats it as UNKNOWN. A session whose owner we
    cannot establish is not a session anyone may drive.
  * A NON-OWNER SEES "unknown session", NOT "forbidden". The owner is a namespace, so Y's
    session does not EXIST for X — that is the honest answer, and it is also why no route needs
    a new error shape. (This hides existence as a side effect; that is not the point and must
    not be sold as one. One uid — X can simply assert Y's owner id and look.)
"""
import json
import os
import re

import paths

# MUST stay identical to nelix_contracts.ids._OWNER_RE. The core deliberately does NOT import
# nelix_contracts: that package is test-only (pyproject.toml keeps the core's runtime closure at
# wasmtime alone), and its validate_session_id would reject this daemon's own `s-<8hex>` ids
# outright. So the rule is restated here and PINNED BY TEST — tests/test_owner_contract_drift.py
# fails the moment the two charsets disagree, which is the drift a copied regex would otherwise
# hide. An owner the core accepts but the store would later reject is a bug deferred, not avoided.
_OWNER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class OwnerRejected(ValueError):
    """A caller-supplied owner_id of bad shape. A ValueError subclass so the /start route maps it
    to 400 (client input error) ahead of the generic ValueError->409 'daemon full' branch."""


class OwnerWriteFailed(Exception):
    """The owner record could not be durably persisted, so the start MUST fail. Not a ValueError:
    this is not the caller's mistake and must not be reported as one (the route maps it to 500).
    Deliberately NOT swallowed the way `Session._write_meta` swallows its OSError — that sidecar
    is capture metadata and a lost field costs a replay; THIS is an access invariant, and a
    session running without a durable owner is one every caller may drive."""


def validate(owner_id) -> str:
    """Shape-check a CALLER-supplied owner id. Raises OwnerRejected. Never coerces, never
    defaults: a defaulted owner is a shared owner, which is precisely the bug."""
    if not isinstance(owner_id, str) or _OWNER_RE.match(owner_id) is None:
        raise OwnerRejected(f"invalid owner_id: {owner_id!r}")
    return owner_id


def write(session_dir, owner_id) -> None:
    """Durably bind `session_dir` to `owner_id`, atomically. Raises OwnerRejected (bad shape) or
    OwnerWriteFailed (could not persist).

    Atomic because a torn record reads back malformed, and malformed fails closed — a start that
    returned 200 would hand back a session id its own owner could never use again. Written and
    fsynced BEFORE the caller ever learns the session id, so there is no window in which a
    reachable session has no owner.
    """
    validate(owner_id)
    path = paths.session_owner(session_dir)
    tmp = path.with_name(path.name + ".tmp")
    try:
        paths.ensure_private_dir(path.parent)
        with open(tmp, "w", opener=paths.private_opener) as f:
            json.dump({"owner_id": owner_id}, f)
            f.flush()
            os.fsync(f.fileno())          # the bytes, not just the page cache
        os.replace(tmp, path)             # atomic: readers see the old record or the new one
        _fsync_dir(path.parent)           # and the RENAME itself survives a crash
    except OSError as e:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise OwnerWriteFailed(f"could not persist owner record for {session_dir}: {e}") from e


def _fsync_dir(d):
    # Best-effort: the rename is already atomic, this only pins its DURABILITY, and some
    # filesystems refuse an O_RDONLY fsync on a directory. A failure here must not fail a start
    # whose record is already in place and readable.
    try:
        fd = os.open(d, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def owner_of(session_dir):
    """The durable owner of `session_dir`, or None if it cannot be established.

    None is the FAIL-CLOSED answer and it covers every way the record can fail us: no session
    dir, no record, unreadable, truncated, not JSON, not an object, no owner_id key, or an
    owner_id of bad shape. Callers must treat None as "nobody owns this" — never as a wildcard.
    """
    try:
        with open(paths.session_owner(session_dir)) as f:
            rec = json.load(f)
    except (OSError, ValueError):         # missing / unreadable / not JSON (JSONDecodeError < ValueError)
        return None
    if not isinstance(rec, dict):
        return None
    try:
        return validate(rec.get("owner_id"))
    except OwnerRejected:                 # a record we cannot trust is a record we do not honour
        return None


def owned_by(session_dir, owner_id):
    """The STORED owner of `session_dir` if `owner_id` owns it, else None. One read.

    THE single ownership decision — `owns` is this narrowed to a bool. Callers that need the value
    they authorised against (restart, which must give the new session the old one's owner) take it
    from here rather than pairing `owner_of` with a hand-rolled `==`, because that comparison has a
    trap, and this is the one place it is written down.

    The trap: `owner_of(d) == owner_id` returns TRUE when both sides are None — an ownerless
    session matching a caller that simply OMITTED the field. Spelled that way, the fail-closed
    record grants precisely what it exists to deny, and it is a plain `==` that reads correct.
    (Measured: a hand-rolled comparison in _restart_source reintroduced exactly this and the whole
    suite stayed green, because the RPC route happens to shape-check first. The manager is the
    API; the route is only one caller of it.)

    What actually closes it is the pair of invariants below, NOT a validate() on `owner_id`:
      * `owner_of` returns a VALIDATED owner id or None — never a value it does not trust. So a
        malformed or absent caller id cannot equal a stored one; there is nothing to compare it to.
      * "no owner" and "refused" are the SAME value, None. There is no spelling of "nobody owns
        this" that is also a grant, so the trap is unreachable rather than merely guarded.
    A validate() here would be a third check that can never change an outcome — not a guard, just
    another place for the rule to rot.
    """
    stored = owner_of(session_dir)     # a VALIDATED owner id, or None if none can be established
    return stored if (stored is not None and stored == owner_id) else None


def owns(session_dir, owner_id) -> bool:
    """Does `owner_id` own the session at `session_dir`? The ownership predicate."""
    return owned_by(session_dir, owner_id) is not None


def owns_session(session_id, owner_id) -> bool:
    """`owns`, keyed by session id against the daemon's own sessions root."""
    return owns(paths.sessions_root() / session_id, owner_id)


def session_owned_by(session_id, owner_id):
    """`owned_by`, keyed by session id against the daemon's own sessions root."""
    return owned_by(paths.sessions_root() / session_id, owner_id)
