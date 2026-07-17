"""The START path (spec §3) — the router's whole reason to allocate identity before forwarding.

Flow for `POST /start`:
  1. Validate owner_id; REQUIRE an idempotency_key (a lost-reply-safe start needs the CALLER to
     supply it — reject if absent). Validate executor/task/cwd/model.
  2. Derive a STABLE request_fingerprint over the semantic request, and a STABLE orchestration_id
     when the caller omitted one (see _derive_orchestration_id — a random mint would break idempotency
     for callers who supply only a key).
  3. reserve(): the DB's UNIQUE(owner,key) is the atomic arbiter. A fresh reservation mints the
     session id; a same-key-same-request retry returns the original row (replay=True); a
     same-key-DIFFERENT-request raises IDEMPOTENCY_CONFLICT.
  4. REPLAY: started -> return the original outcome (do NOT forward again — no second worker);
     starting -> return in-progress (a concurrent duplicate is mid-forward); failed -> replay the
     recorded failure.
  5. FRESH: pick the active generation, assign_generation() BEFORE forwarding (so a lost response can
     recover the original operation), forward with the assigned session_id, then commit() on success
     or fail() on failure/timeout. NEVER re-pick the active generation on a retry — the ledger's
     committed generation_id binds it.

The router shares ONE StartLedger and ONE GenerationRegistry across request threads (both are
thread-safe); this class holds no per-request state and is safe to share too."""
import hashlib
import json

from nelix_contracts.errors import (
    GENERATION_UNAVAILABLE, IDEMPOTENCY_CONFLICT, INTERNAL_ERROR, INVALID_REQUEST,
    STORE_CORRUPT, STORE_UNAVAILABLE, STORE_UNSUPPORTED, NelixError,
)
from nelix_contracts.ids import (
    InvalidId, new_orchestration_id, validate_orchestration_id, validate_owner_id,
)

_HTTP_STATUS = {
    INVALID_REQUEST: 400,
    IDEMPOTENCY_CONFLICT: 409,
    GENERATION_UNAVAILABLE: 503,
    STORE_UNAVAILABLE: 503,
    STORE_UNSUPPORTED: 500,
    STORE_CORRUPT: 500,
    INTERNAL_ERROR: 500,
}

# A recorded failure reason is stored durably (the ledger's reason column has no length bound of its
# own); cap it so a runaway upstream message cannot grow the database unboundedly.
_MAX_REASON = 500


def http_status(code: str) -> int:
    """HTTP status for a contract error code. Unknown -> 500 (our bug, not the caller's)."""
    return _HTTP_STATUS.get(code, 500)


def _require_str(value, name):
    if not isinstance(value, str) or not value:
        raise NelixError(INVALID_REQUEST, f"{name} is required and must be a non-empty string")
    return value


def _request_fingerprint(executor, task, cwd, model, orchestration_id) -> str:
    """A stable digest of the SEMANTIC request. Same request -> same fingerprint (a retry replays);
    different request under the same key -> different fingerprint (the ledger flags a conflict)."""
    canonical = json.dumps(
        {"executor": executor, "task": task, "cwd": cwd, "model": model,
         "orchestration_id": orchestration_id},
        sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _derive_orchestration_id(owner_id, idempotency_key) -> str:
    """A DETERMINISTIC orchestration id for a caller that omitted one, derived from (owner, key).

    A random new_orchestration_id() would break idempotency: the ledger compares orchestration_id
    on replay, so a retry that minted a fresh random id would be flagged as a same-key-different-
    request CONFLICT instead of replaying — defeating the lost-reply guarantee for any caller that
    supplies only an idempotency_key. Deriving it from (owner, key) makes the retry reproduce the
    SAME id (and is race-free for concurrent duplicates, unlike a lookup-then-mint). Distinct
    starts (distinct keys) still get distinct orchestrations. Shape: o-<32hex> (validates)."""
    digest = hashlib.sha256(f"{owner_id}\x00{idempotency_key}".encode()).hexdigest()[:32]
    return "o-" + digest


def _reason_from_reply(reply) -> str:
    """A human reason extracted from a generation's failure reply (its /start error body), for the
    ledger's durable failure record."""
    if isinstance(reply, dict):
        err = reply.get("error")
        if isinstance(err, dict):
            return str(err.get("message") or err.get("code") or "generation error")
        if err:
            return str(err)
    return f"unexpected start reply: {reply!r}"


class StartPath:
    def __init__(self, ledger, registry):
        self._ledger = ledger
        self._registry = registry

    def handle(self, body) -> "tuple[int, dict]":
        """Handle one POST /start. Returns (http_status, response_dict). Every NelixError becomes a
        stable envelope — never a bare 500/stacktrace to the caller."""
        try:
            return self._handle(body)
        except NelixError as e:
            return http_status(e.code), e.to_envelope()

    def _handle(self, body):
        if not isinstance(body, dict):
            raise NelixError(INVALID_REQUEST, "start body must be a JSON object")
        owner_id = body.get("owner_id")
        try:
            validate_owner_id(owner_id)
        except InvalidId as e:
            raise NelixError(INVALID_REQUEST, str(e)) from None
        idem = _require_str(body.get("idempotency_key"), "idempotency_key")
        executor = _require_str(body.get("executor"), "executor")
        task = _require_str(body.get("task"), "task")
        cwd = _require_str(body.get("cwd"), "cwd")
        model = body.get("model")
        if model is not None and not isinstance(model, str):
            raise NelixError(INVALID_REQUEST, "model must be a string when provided")

        orchestration_id = body.get("orchestration_id")
        if orchestration_id is None:
            orchestration_id = _derive_orchestration_id(owner_id, idem)
        else:
            try:
                validate_orchestration_id(orchestration_id)
            except InvalidId as e:
                raise NelixError(INVALID_REQUEST, str(e)) from None

        fingerprint = _request_fingerprint(executor, task, cwd, model, orchestration_id)
        res = self._ledger.reserve(idempotency_key=idem, owner_id=owner_id,
                                   orchestration_id=orchestration_id,
                                   request_fingerprint=fingerprint)
        if res.replay:
            return self._replay(res)
        return self._drive_fresh(res.session_id, owner_id, executor, task, cwd, model)

    def _replay(self, res):
        """A same-key-same-request retry: return the ORIGINAL outcome, never a second forward."""
        if res.state == "started":
            return 200, {"operation": "start", "status": "started", "session_id": res.session_id,
                         "generation_id": res.generation_id, "replay": True}
        if res.state == "starting":
            # A concurrent duplicate is mid-forward (or a prior attempt is unresolved). Return the
            # in-progress reservation idempotently — do NOT forward (that is the second-worker risk).
            # Driving recovery of a stuck 'starting' is 3c.3 (restart/reconcile).
            return 200, {"operation": "start", "status": "starting", "session_id": res.session_id,
                         "generation_id": res.generation_id, "replay": True}
        # failed: replay the recorded failure (retryable — a fresh key may succeed later).
        raise NelixError(GENERATION_UNAVAILABLE, res.reason or "start previously failed")

    def _drive_fresh(self, sid, owner_id, executor, task, cwd, model):
        try:
            gen = self._registry.active()
        except NelixError as e:
            # No generation could be picked: fail the reservation so a same-key retry replays the
            # failure rather than minting a fresh worker, and surface the code.
            self._fail(sid, f"{e.code}: {e.message}")
            raise
        try:
            self._ledger.assign_generation(sid, gen.epoch)          # BEFORE forwarding (spec §3)
        except NelixError as e:
            self._fail(sid, f"could not bind generation: {e.message}")
            raise
        return self._forward(sid, gen, owner_id, executor, task, cwd, model)

    def _forward(self, sid, gen, owner_id, executor, task, cwd, model):
        try:
            from rpc_client import RpcClient
        except ImportError:                                          # package mode
            from .rpc_client import RpcClient
        try:
            reply = RpcClient(gen.transport, owner_id).start(
                executor, task, cwd, model=model, session_id=sid)
        except Exception as e:
            reason = f"forward to generation failed: {e}"
            self._fail(sid, reason)
            raise NelixError(GENERATION_UNAVAILABLE, reason) from None

        # Success is decided by the generation's own /start contract: a 200 body reports
        # status "started" and echoes the ASSIGNED session id. Anything else is a failed start.
        if isinstance(reply, dict) and reply.get("status") == "started" \
                and reply.get("session_id") == sid:
            self._ledger.commit(sid, gen.epoch)
            out = {"operation": "start", "status": "started", "session_id": sid,
                   "generation_id": gen.epoch}
            for k in ("snapshot", "next_after_seq", "next_action"):
                if k in reply:
                    out[k] = reply[k]
            return 200, out

        reason = _reason_from_reply(reply)
        self._fail(sid, reason)
        raise NelixError(GENERATION_UNAVAILABLE, f"generation rejected the start: {reason}")

    def _fail(self, sid, reason):
        """Record a failed start, best-effort. A fail() that itself conflicts (e.g. a concurrent
        commit already won) must not mask the original error the caller is being told about."""
        try:
            self._ledger.fail(sid, reason[:_MAX_REASON] or "start failed")
        except NelixError:
            pass
