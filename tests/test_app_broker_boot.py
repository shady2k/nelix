import daemon.app as app
import daemon.broker_client as bc


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
