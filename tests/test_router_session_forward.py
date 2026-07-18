"""nelix-3rm slice 3c.2: SessionForward — the router's session-keyed OWNER-route forwarding
(status/dialog/screen/respond/stop/restart) plus the owner-EXEMPT executor plane (/hook, /message).

AUTH PASSTHROUGH is the whole point (spec §7): SessionForward validates owner_id's SHAPE (same as
/start) then forwards it UNCHANGED to the generation, which alone decides ownership
(`owner.owns_session`, daemon-side). These tests prove: (1) the forward reaches the generation with
the caller's OWN owner_id, never a router-substituted one; (2) a wrong-owner request's rejection is
RELAYED FAITHFULLY — the exact status+body the generation answered with, unexamined, whatever shape
that rejection takes (a 200 with an error body for status/screen, a 404 for dialog/respond/stop/
restart — mirroring daemon/rpc_server.py); (3) hook/message pass the secret header + raw body
through untouched, never touching owner_id at all; (4) a transport failure of EITHER phase collapses
to one retryable GENERATION_UNAVAILABLE, never a bare 500 (these routes carry no ledger reservation
to protect, unlike /start)."""
import pytest

from conftest import OWNER
from nelix_contracts.errors import NelixError
from router.registry import GenerationRegistry
from router.session_forward import SessionForward

from _router_fakes import Backend, Supervisor

OTHER_OWNER = "harness-y"
SID = "s-" + "a" * 32


@pytest.fixture
def wired():
    backend = Backend()
    registry = GenerationRegistry(supervisor=Supervisor(backend.transport),
                                  health_probe=lambda t: backend.build_id)
    backend.owns[SID] = OWNER                     # simulate: OWNER started this session
    forward = SessionForward(registry)
    yield forward, backend
    backend.close()


# ============================================================ owner passthrough, happy path

def test_status_forwards_session_id_and_owner_id(wired):
    forward, backend = wired
    status, body = forward.status(OWNER, SID)
    assert status == 200
    assert body["session_id"] == SID
    call = backend.calls[-1]
    assert call["query"]["owner_id"] == [OWNER]
    assert call["query"]["session_id"] == [SID]


def test_status_include_progress_is_passed_through_when_given(wired):
    forward, backend = wired
    forward.status(OWNER, SID, include_progress="1")
    assert backend.calls[-1]["query"]["include_progress"] == ["1"]


def test_status_omits_include_progress_when_not_given(wired):
    forward, backend = wired
    forward.status(OWNER, SID)
    assert "include_progress" not in backend.calls[-1]["query"]


def test_dialog_forwards_and_relays_body(wired):
    forward, backend = wired
    status, body = forward.dialog(OWNER, SID)
    assert status == 200
    assert body["chunk"] == "DIALOG " + SID


def test_screen_forwards_and_relays_body(wired):
    forward, backend = wired
    status, body = forward.screen(OWNER, SID)
    assert status == 200
    assert body["screen"] == "SCREEN " + SID


def test_respond_forwards_answer_and_decision_id(wired):
    forward, backend = wired
    status, body = forward.respond(OWNER, SID, "yes", decision_id="dec-1")
    assert status == 200
    assert body["answer"] == "yes"
    assert backend.calls[-1]["body"]["decision_id"] == "dec-1"


def test_stop_forwards(wired):
    forward, backend = wired
    status, body = forward.stop(OWNER, SID)
    assert status == 200
    assert body["status"] == "stopped"


def test_restart_forwards_force_flag(wired):
    forward, backend = wired
    status, body = forward.restart(OWNER, SID, force=True)
    assert status == 200
    assert body["force"] is True


# ============================================================ auth passthrough — the ownership test

@pytest.mark.parametrize("op,expected_status", [
    ("status", 200), ("screen", 200), ("dialog", 404),
])
def test_wrong_owner_get_rejection_is_relayed_faithfully(wired, op, expected_status):
    forward, backend = wired
    status, body = getattr(forward, op)(OTHER_OWNER, SID)
    assert status == expected_status
    assert "error" in body                     # the GENERATION's rejection, relayed unexamined


def test_wrong_owner_respond_rejection_is_relayed_faithfully(wired):
    forward, backend = wired
    status, body = forward.respond(OTHER_OWNER, SID, "yes")
    assert status == 404
    assert body["status"] == "unknown_session"


def test_wrong_owner_stop_rejection_is_relayed_faithfully(wired):
    forward, backend = wired
    status, body = forward.stop(OTHER_OWNER, SID)
    assert status == 404
    assert body["status"] == "unknown_session"


def test_wrong_owner_restart_rejection_is_relayed_faithfully(wired):
    forward, backend = wired
    status, body = forward.restart(OTHER_OWNER, SID)
    assert status == 404
    assert body["status"] == "unknown_session"


# ============================================================ shape validation (router-level, cheap)

def test_bad_owner_id_shape_is_invalid_request(wired):
    forward, backend = wired
    with pytest.raises(NelixError) as exc:
        forward.status("has space", SID)
    assert exc.value.code == "invalid_request"


def test_missing_session_id_is_invalid_request(wired):
    forward, backend = wired
    with pytest.raises(NelixError) as exc:
        forward.respond(OWNER, None, "yes")
    assert exc.value.code == "invalid_request"


def test_bad_session_id_shape_is_invalid_request(wired):
    forward, backend = wired
    with pytest.raises(NelixError) as exc:
        forward.screen(OWNER, "not-a-real-session-id")
    assert exc.value.code == "invalid_request"


# ============================================================ hook/message — owner-EXEMPT

def test_forward_secret_passes_header_and_raw_body_through_unchanged(wired):
    forward, backend = wired
    status, body = forward.forward_secret(
        "POST", "/hook/" + SID, {"X-Nelix-Hook-Secret": backend.hook_secret},
        b'{"hook_event_name": "tool_call"}')
    assert status == 204
    call = backend.calls[-1]
    assert call["headers"]["X-Nelix-Hook-Secret"] == backend.hook_secret
    assert call["raw_body"] == b'{"hook_event_name": "tool_call"}'


def test_forward_secret_wrong_secret_is_relayed_as_401(wired):
    forward, backend = wired
    status, body = forward.forward_secret(
        "POST", "/message/" + SID, {"X-Nelix-Hook-Secret": "wrong"}, b'{"kind": "note"}')
    assert status == 401
    assert body == {"error": "unauthorized"}


def test_forward_secret_never_carries_an_owner_id(wired):
    # The whole point of the exemption: no owner_id anywhere on this path (not in headers/body).
    forward, backend = wired
    forward.forward_secret("POST", "/message/" + SID, {"X-Nelix-Hook-Secret": backend.hook_secret},
                           b'{"kind": "note", "text": "hi"}')
    call = backend.calls[-1]
    assert b"owner_id" not in call["raw_body"]
    assert "owner_id" not in call["headers"]


# ---------------------------------------------- router-side sid shape validation (finding)

@pytest.mark.parametrize("path", ["/hook/not-a-real-session-id", "/message/not-a-real-session-id"])
def test_forward_secret_bad_shape_sid_is_invalid_request_before_any_forward(wired, path):
    # Unlike every owner-scoped route above (_session() validates before forwarding), /hook and
    # /message used to forward a caller-supplied sid to the wire unchecked. Not exploitable (the
    # daemon validates independently), but inconsistent with the router's fail-fast pattern -- a
    # bad-shape sid must now be rejected router-side, before ever reaching the backend.
    forward, backend = wired
    before = len(backend.calls)
    with pytest.raises(NelixError) as exc:
        forward.forward_secret("POST", path, {"X-Nelix-Hook-Secret": backend.hook_secret}, b"{}")
    assert exc.value.code == "invalid_request"
    assert len(backend.calls) == before          # never reached the wire


# ============================================================ transport failure -> retryable

def test_transport_failure_is_retryable_generation_unavailable():
    from daemon.transport import Transport

    class _DeadSupervisor:
        _t = Transport.tcp("127.0.0.1", 9, "t")            # discard port: connection refused

        def active_generation(self):
            return (self._t, {"pid": 1, "start_fingerprint": "fp"})

        def held_generation(self):
            return (self._t, {"pid": 1, "start_fingerprint": "fp"})

        def ensure_running(self):
            return self._t

    registry = GenerationRegistry(supervisor=_DeadSupervisor(), health_probe=lambda t: None)
    forward = SessionForward(registry)
    with pytest.raises(NelixError) as exc:
        forward.status(OWNER, SID)
    assert exc.value.code == "generation_unavailable"
    assert exc.value.retryable is True


def test_transport_failure_on_hook_forward_is_retryable_generation_unavailable():
    from daemon.transport import Transport

    class _DeadSupervisor:
        _t = Transport.tcp("127.0.0.1", 9, "t")

        def active_generation(self):
            return (self._t, {"pid": 1, "start_fingerprint": "fp"})

        def held_generation(self):
            return (self._t, {"pid": 1, "start_fingerprint": "fp"})

        def ensure_running(self):
            return self._t

    registry = GenerationRegistry(supervisor=_DeadSupervisor(), health_probe=lambda t: None)
    forward = SessionForward(registry)
    with pytest.raises(NelixError) as exc:
        forward.forward_secret("POST", "/hook/" + SID, {"X-Nelix-Hook-Secret": "x"}, b"{}")
    assert exc.value.code == "generation_unavailable"
    assert exc.value.retryable is True
