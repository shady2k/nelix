import daemon.app as app
import daemon.broker_client as bc


def test_concurrency_limit_default_flows_config_to_app_to_manager(monkeypatch, tmp_path):
    """Prove the default concurrency_limit=5 flows through config→app.main()→SessionManager
    via the REAL load_concurrency_limit call inside main()."""
    captured = {}

    class _FakeSessionManager:
        def __init__(self, *a, concurrency_limit, **kw):
            captured["limit"] = concurrency_limit
            raise SystemExit(0)   # stop main() immediately after SessionManager is constructed

    monkeypatch.setattr(app, "SessionManager", _FakeSessionManager)
    monkeypatch.setattr(app, "load_specs", lambda *a, **k: {})
    monkeypatch.setattr(app, "acquire_singleton", lambda *a, **k: 7)
    monkeypatch.setattr(app, "BrokerClient", lambda: object())
    monkeypatch.setattr(app, "set_broker", lambda *a, **k: None)
    monkeypatch.setattr(app.reaper, "reconcile_orphans", lambda *a, **k: [])

    # Non-existent config → load_concurrency_limit returns the default 5
    monkeypatch.setenv("NELIX_CONFIG", str(tmp_path / "absent.toml"))
    monkeypatch.setenv("NELIX_RPC_TOKEN", "t")

    try:
        app.main()
    except SystemExit:
        pass

    assert captured.get("limit") == 5


def test_broker_set_before_server_built(monkeypatch, tmp_path):
    order = []

    class _FakeBroker:
        def close(self): order.append("broker_close")

    # app.py did `from daemon.broker_client import BrokerClient, set_broker` -> patch app's own refs.
    monkeypatch.setattr(app, "BrokerClient", lambda: _FakeBroker())

    def _set(client):
        order.append("set_broker")
        bc._broker = client
    monkeypatch.setattr(app, "set_broker", _set)

    def _fake_make_server(*a, **k):
        order.append("make_server")
        raise SystemExit(0)                       # stop main() right after server build
    monkeypatch.setattr(app, "make_server", _fake_make_server)

    monkeypatch.setattr(app, "load_specs", lambda *a, **k: {})
    monkeypatch.setattr(app, "acquire_singleton", lambda *a, **k: 7)   # fake lock fd
    monkeypatch.setattr(app.reaper, "reconcile_orphans", lambda *a, **k: [])
    monkeypatch.setenv("NELIX_RPC_TOKEN", "t")

    try:
        app.main()
    except SystemExit:
        pass
    assert order.index("set_broker") < order.index("make_server")
