import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import wake  # noqa: E402


class FakeCtx:
    def __init__(self):
        self.dispatched = []
    def dispatch_tool(self, name, args, **k):
        self.dispatched.append((name, args)); return "{}"


def test_arm_waiter_uses_bundled_path_and_token():
    ctx = FakeCtx()
    wake.arm_waiter(ctx, "http://127.0.0.1:9000", "secrettok", after_seq=5)
    assert len(ctx.dispatched) == 1
    name, args = ctx.dispatched[0]
    assert name == "terminal"
    assert args["background"] is True and args["notify_on_complete"] is True
    assert str(Path(wake.__file__).parent / "bin" / "nelix-wait") in args["command"]
    assert "--after 5" in args["command"]
    assert args["env"]["NELIX_RPC_TOKEN"] == "secrettok"
    assert args["env"]["NELIX_RPC"] == "http://127.0.0.1:9000"
