import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import wake  # noqa: E402


class FakeCtx:
    def __init__(self):
        self.dispatched = []
    def dispatch_tool(self, name, args, **k):
        self.dispatched.append((name, args)); return "{}"


def test_arm_waiter_passes_state_file_not_base_token(tmp_path):
    """arm_waiter builds a --state-file command; no --base or --token-file."""
    ctx = FakeCtx()
    sf = tmp_path / ".active.json"
    wake.arm_waiter(ctx, after_seq=5, state_file=sf)
    assert len(ctx.dispatched) == 1
    name, args = ctx.dispatched[0]
    assert name == "terminal"
    assert args["background"] is True and args["notify_on_complete"] is True
    cmd = args["command"]
    assert str(Path(wake.__file__).parent / "bin" / "nelix-wait") in cmd
    assert "--state-file" in cmd and str(sf) in cmd
    assert "--after 5" in cmd
    # --base and --token-file must be gone
    assert "--base" not in cmd and "--token-file" not in cmd
    # The terminal tool ignores an `env` dict, so the token must NOT travel that way.
    assert "env" not in args
    assert "--session-id" not in cmd               # omitted when no session_id given


def test_arm_waiter_scopes_to_session_when_given(tmp_path):
    ctx = FakeCtx()
    sf = tmp_path / ".active.json"
    wake.arm_waiter(ctx, after_seq=5, state_file=sf, session_id="s-abc")
    cmd = ctx.dispatched[0][1]["command"]
    assert "--session-id s-abc" in cmd             # scoped so cross-session events don't wake
