import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import wake  # noqa: E402


class FakeCtx:
    def __init__(self):
        self.dispatched = []
    def dispatch_tool(self, name, args, **k):
        self.dispatched.append((name, args)); return "{}"


def test_arm_waiter_passes_token_file_not_env(tmp_path):
    ctx = FakeCtx()
    tf = tmp_path / ".active.json"
    wake.arm_waiter(ctx, "http://127.0.0.1:9000", after_seq=5, token_file=tf)
    assert len(ctx.dispatched) == 1
    name, args = ctx.dispatched[0]
    assert name == "terminal"
    assert args["background"] is True and args["notify_on_complete"] is True
    cmd = args["command"]
    assert str(Path(wake.__file__).parent / "bin" / "nelix-wait") in cmd
    assert "--base" in cmd and "http://127.0.0.1:9000" in cmd
    assert "--after 5" in cmd
    assert "--token-file" in cmd and str(tf) in cmd
    # The terminal tool ignores an `env` dict, so the token must NOT travel that way;
    # and it must NOT be inlined in the command (avoids ps / redact_secrets exposure).
    assert "env" not in args
