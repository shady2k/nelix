from daemon.drivers import get_driver

def test_claude_declares_discovery_protocol_and_aliases():
    d = get_driver("claude")
    assert getattr(d, "models_protocol", None) == "anthropic"
    assert getattr(d, "model_aliases", frozenset()) == frozenset({"haiku", "sonnet", "opus"})
