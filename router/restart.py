"""The RESTART path — mirrors router/start.py with start-row allocation (nelix-9a4.4).

A restart is not a simple passthrough of the old session_id — the daemon now REQUIRES
a router-assigned new_session_id (spec §3), and the router MUST reserve a start row
and assign a generation BEFORE forwarding, exactly as /start does.

Flow for POST /restart:
  1. Validate owner_id, session_id (old), force.
  2. Reserve a NEW start row: idempotency_key = "restart:<old_session_id>" so the same
     old session can only be restarted once (retries replay). The fingerprint is derived
     from (owner_id, old_session_id) — restart doesn't carry executor/task/cwd because
     the daemon inherits those from the old session.
  3. REPLAY: started -> return original outcome; starting -> return in-progress;
     failed -> replay recorded failure.
  4. FRESH: pick active generation, assign_generation BEFORE forwarding, forward /restart
     to the daemon with new_session_id, then commit on success or fail on error.
"""
import hashlib

from nelix_contracts.errors import (
    GENERATION_UNAVAILABLE, IDEMPOTENCY_CONFLICT, INTERNAL_ERROR, INVALID_REQUEST,
    NelixError,
)
from nelix_contracts.ids import (
    InvalidId, validate_owner_id, validate_session_id,
)


_HTTP_STATUS = {
    INVALID_REQUEST: 400,
    IDEMPOTENCY_CONFLICT: 409,
    GENERATION_UNAVAILABLE: 503,
    INTERNAL_ERROR: 500,
}

_MAX_REASON = 500


def http_status(code: str) -> int:
    return _HTTP_STATUS.get(code, 500)


def _require_str(value, name):
    if not isinstance(value, str) or not value:
        raise NelixError(INVALID_REQUEST, f"{name} is required and must be a non-empty string")
    return value


def _request_fingerprint(owner_id, old_session_id) -> str:
    """A stable digest over the restart's semantic identity."""
    canonical = f"{owner_id}\x00{old_session_id}\x00restart"
    return hashlib.sha256(canonical.encode()).hexdigest()[:64]


def _derive_orchestration_id(owner_id, old_session_id) -> str:
    """Deterministic orchestration id from (owner, old_session)."""
    digest = hashlib.sha256(f"{owner_id}\x00{old_session_id}".encode()).hexdigest()[:32]
    return "o-" + digest


class RestartPath:
    def __init__(self, ledger, registry):
        self._ledger = ledger
        self._registry = registry

    def handle(self, body) -> "tuple[int, dict]":
        try:
            return self._handle(body)
        except NelixError as e:
            return http_status(e.code), e.to_envelope()

    def _handle(self, body):
        if not isinstance(body, dict):
            raise NelixError(INVALID_REQUEST, "restart body must be a JSON object")
        owner_id = body.get("owner_id")
        try:
            validate_owner_id(owner_id)
        except InvalidId as e:
            raise NelixError(INVALID_REQUEST, str(e)) from None
        old_sid = body.get("session_id")
        try:
            validate_session_id(old_sid)
        except InvalidId as e:
            raise NelixError(INVALID_REQUEST, str(e)) from None
        force = bool(body.get("force", False))

        idem_key = f"restart:{old_sid}"
        orch_id = _derive_orchestration_id(owner_id, old_sid)
        fingerprint = _request_fingerprint(owner_id, old_sid)

        res = self._ledger.reserve(idempotency_key=idem_key, owner_id=owner_id,
                                   orchestration_id=orch_id,
                                   request_fingerprint=fingerprint)
        if res.replay:
            return self._replay(res)
        return self._drive_fresh(res.session_id, owner_id, old_sid, force)

    def _replay(self, res):
        if res.state == "started":
            return 200, {"operation": "restart", "status": "restarted",
                         "session_id": res.session_id,
                         "generation_id": res.generation_id, "replay": True}
        if res.state == "starting":
            return 200, {"operation": "restart", "status": "starting",
                         "session_id": res.session_id,
                         "generation_id": res.generation_id, "replay": True}
        raise NelixError(GENERATION_UNAVAILABLE, res.reason or "restart previously failed")

    def _drive_fresh(self, new_sid, owner_id, old_sid, force):
        try:
            gen = self._registry.active()
        except NelixError as e:
            self._fail(new_sid, f"{e.code}: {e.message}")
            raise
        try:
            self._ledger.assign_generation(new_sid, gen.epoch)
        except NelixError as e:
            self._fail(new_sid, f"could not bind generation: {e.message}")
            raise
        return self._forward(new_sid, gen, owner_id, old_sid, force)

    def _forward(self, new_sid, gen, owner_id, old_sid, force):
        try:
            from rpc_client import RpcClient, ForwardConnectError, ForwardResponseError
        except ImportError:
            from .rpc_client import RpcClient, ForwardConnectError, ForwardResponseError
        try:
            reply = RpcClient(gen.transport, owner_id).restart(
                old_sid, new_session_id=new_sid, owner_id=owner_id, force=force)
        except ForwardConnectError as e:
            reason = f"forward to generation failed before delivery: {e}"
            self._fail(new_sid, reason)
            raise NelixError(GENERATION_UNAVAILABLE, reason) from None
        except ForwardResponseError as e:
            raise NelixError(
                GENERATION_UNAVAILABLE,
                f"forward to generation was ambiguous (reservation left starting): {e}") from None

        if isinstance(reply, dict) and reply.get("status") == "restarted" \
                and reply.get("session_id") == new_sid:
            self._ledger.commit(new_sid, gen.epoch)
            out = {"operation": "restart", "status": "restarted",
                   "session_id": new_sid, "generation_id": gen.epoch}
            for k in ("snapshot", "next_after_seq", "next_action", "lineage_id",
                      "restart_count", "restarted_from"):
                if k in reply:
                    out[k] = reply[k]
            return 200, out

        reason = reply.get("error", "generation rejected the restart") if isinstance(reply, dict) else f"unexpected restart reply: {reply!r}"
        self._fail(new_sid, str(reason)[:_MAX_REASON] or "restart failed")
        raise NelixError(GENERATION_UNAVAILABLE, f"generation rejected the restart: {reason}")

    def _fail(self, sid, reason):
        try:
            self._ledger.fail(sid, reason[:_MAX_REASON] or "restart failed")
        except NelixError:
            pass
