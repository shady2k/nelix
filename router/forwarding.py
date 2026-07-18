"""The forward-failure mapping shared by every 3c.2 forward (session-keyed, hook/message, and
operator): none of these routes are reserve-tracked like /start (no ledger row to protect), so a
transport failure of EITHER phase — connect (ForwardConnectError, definite) or response
(ForwardResponseError, ambiguous) — collapses to ONE retryable GENERATION_UNAVAILABLE envelope,
never a bare 500. Centralized here so router/session_forward.py and router/operator.py reuse the
SAME mapping rather than each re-deriving it."""
from nelix_contracts.errors import GENERATION_UNAVAILABLE, NelixError

try:
    from rpc_client import ForwardConnectError, ForwardResponseError
except ImportError:                                          # package mode
    from .rpc_client import ForwardConnectError, ForwardResponseError


def relay(fn):
    """Run one forward `fn` (a zero-arg call to a phase-split forward), mapping a transport-phase
    failure to one retryable GENERATION_UNAVAILABLE. `fn`'s own successful (status, body) return is
    passed through UNCHANGED — that response IS the generation's answer and must never be
    reinterpreted (including its own ownership verdict on the session-keyed owner routes)."""
    try:
        return fn()
    except ForwardConnectError as e:
        raise NelixError(GENERATION_UNAVAILABLE,
                         f"forward to generation failed before delivery: {e}") from None
    except ForwardResponseError as e:
        raise NelixError(GENERATION_UNAVAILABLE,
                         f"forward to generation was ambiguous: {e}") from None
