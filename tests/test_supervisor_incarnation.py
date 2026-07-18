"""nelix-3rm (router slice 3c.1): the router keys a generation EPOCH on the daemon's process
incarnation so that a daemon RESTART (new pid, or a reused pid with a new start time) yields a NEW
epoch. supervisor.incarnation() surfaces that identity — (pid, start_fingerprint) — from the same
.active.json state endpoint() already trusts, with the same liveness + fingerprint gate."""
import importlib
import json
import os

import paths
import supervisor
from daemon import reaper, singleton
from daemon.transport import Transport


def test_incarnation_is_none_when_no_daemon(monkeypatch, tmp_path):
    monkeypatch.setenv("NELIX_HOME", str(tmp_path))
    importlib.reload(paths); importlib.reload(supervisor)
    assert supervisor.incarnation() is None


def test_incarnation_reports_pid_and_fingerprint_of_live_daemon(monkeypatch, tmp_path):
    monkeypatch.setenv("NELIX_HOME", str(tmp_path))
    importlib.reload(paths); importlib.reload(supervisor)
    paths.ensure_private_dir(paths.nelix_root())
    supervisor._write_state(os.getpid(), Transport.unix(str(paths.rpc_sock())))
    inc = supervisor.incarnation()
    assert inc is not None
    assert inc["pid"] == os.getpid()
    assert inc["start_fingerprint"] == reaper.ProcessInspector().start_fingerprint(os.getpid())


def test_incarnation_rejects_a_reused_pid_whose_fingerprint_differs(monkeypatch, tmp_path):
    monkeypatch.setenv("NELIX_HOME", str(tmp_path))
    importlib.reload(paths); importlib.reload(supervisor)
    paths.ensure_private_dir(paths.nelix_root())
    supervisor._write_state(os.getpid(), Transport.unix(str(paths.rpc_sock())))
    # The pid is alive (our own) but reports a DIFFERENT start fingerprint than the one recorded:
    # a recycled pid must not masquerade as the same daemon incarnation.
    monkeypatch.setattr(reaper.ProcessInspector, "start_fingerprint",
                        lambda self, pid: "different-fingerprint")
    assert supervisor.incarnation() is None


def test_active_generation_reads_transport_and_incarnation_from_one_snapshot(monkeypatch, tmp_path):
    """Finding #3: the router needs the transport and the incarnation from the SAME .active.json
    read, so a restart cannot bind a new incarnation's epoch to a dead incarnation's transport.
    active_generation() returns both, gated by the same liveness + fingerprint + health checks
    endpoint()/incarnation() apply."""
    monkeypatch.setenv("NELIX_HOME", str(tmp_path))
    importlib.reload(paths); importlib.reload(supervisor)
    paths.ensure_private_dir(paths.nelix_root())
    t = Transport.unix(str(paths.rpc_sock()))
    supervisor._write_state(os.getpid(), t)
    monkeypatch.setattr(supervisor, "_healthy", lambda tr: True)   # a daemon answering our protocol
    snap = supervisor.active_generation()
    assert snap is not None
    transport, inc = snap
    assert transport.kind == t.kind and transport.path == t.path
    assert inc["pid"] == os.getpid()
    assert inc["start_fingerprint"] == reaper.ProcessInspector().start_fingerprint(os.getpid())


def test_active_generation_is_none_when_unhealthy(monkeypatch, tmp_path):
    """An unhealthy (or unreachable) recorded daemon yields no snapshot at all — never a transport
    without its incarnation, or vice versa. Mirrors endpoint()'s health gate."""
    monkeypatch.setenv("NELIX_HOME", str(tmp_path))
    importlib.reload(paths); importlib.reload(supervisor)
    paths.ensure_private_dir(paths.nelix_root())
    supervisor._write_state(os.getpid(), Transport.unix(str(paths.rpc_sock())))
    monkeypatch.setattr(supervisor, "_healthy", lambda tr: False)
    assert supervisor.active_generation() is None


def test_active_generation_is_none_when_no_daemon(monkeypatch, tmp_path):
    monkeypatch.setenv("NELIX_HOME", str(tmp_path))
    importlib.reload(paths); importlib.reload(supervisor)
    assert supervisor.active_generation() is None


def test_current_generation_reads_the_pair_without_a_health_rpc(monkeypatch, tmp_path):
    """Finding #1: current_generation() is the CHEAP recorded read (held_generation()'s building
    block for the TCP-holder reconcile) — it returns the recorded (transport, incarnation) straight
    from .active.json and MUST NOT do a /health RPC. We make any health probe blow up to prove it is
    never called."""
    monkeypatch.setenv("NELIX_HOME", str(tmp_path))
    importlib.reload(paths); importlib.reload(supervisor)
    paths.ensure_private_dir(paths.nelix_root())
    t = Transport.unix(str(paths.rpc_sock()))
    supervisor._write_state(os.getpid(), t)

    def _explode(_tr):
        raise AssertionError("current_generation() must not call the health probe")
    monkeypatch.setattr(supervisor, "_healthy", _explode)

    snap = supervisor.current_generation()
    assert snap is not None
    transport, inc = snap
    assert transport.kind == t.kind and transport.path == t.path
    assert inc["pid"] == os.getpid()
    assert inc["start_fingerprint"] == reaper.ProcessInspector().start_fingerprint(os.getpid())


def test_current_generation_is_none_when_no_daemon(monkeypatch, tmp_path):
    monkeypatch.setenv("NELIX_HOME", str(tmp_path))
    importlib.reload(paths); importlib.reload(supervisor)
    assert supervisor.current_generation() is None


def test_held_generation_is_the_live_lock_holder_authoritative_over_active_json(monkeypatch, tmp_path):
    """Finding #1 (rev 3): the router's UNDER-LOCK identity read is the VALIDATED LIVE LOCK HOLDER,
    not .active.json. .active.json is not monotonic — a paused spawner can rewrite it back to a
    superseded incarnation A after a newer B took the released singleton lock. held_generation()
    derives the incarnation from whoever HOLDS THE LOCK (kernel: exactly one live holder), so a stale
    .active.json (even one naming the same pid with a superseded fingerprint) can never make the
    caller route to A when B holds the lock. For a unix holder the transport is re-derived from the
    holder's own lock meta, never taken from the possibly-stale .active.json."""
    monkeypatch.setenv("NELIX_HOME", str(tmp_path))
    importlib.reload(paths); importlib.reload(supervisor)
    paths.ensure_private_dir(paths.nelix_root())
    insp = reaper.ProcessInspector()
    real_fp = insp.start_fingerprint(os.getpid())
    b_sock = str(paths.rpc_sock())
    # B (this live process) holds the singleton lock, serving a unix socket.
    fd = singleton.acquire(paths.daemon_lock(),
                           {"pid": os.getpid(), "start_fingerprint": real_fp,
                            "transport": "unix", "path": b_sock})
    assert fd is not None
    try:
        # .active.json is STALE: a superseded incarnation A pointing at a DIFFERENT socket.
        paths.state_file().write_text(json.dumps(
            {"pid": os.getpid(), "start_fingerprint": "stale-A-fingerprint",
             "transport": "unix", "path": "/tmp/stale-a.sock"}))
        snap = supervisor.held_generation()
        assert snap is not None
        transport, inc = snap
        # incarnation is the LIVE LOCK HOLDER (B), never the stale .active.json (A).
        assert inc == {"pid": os.getpid(), "start_fingerprint": real_fp}
        assert inc["start_fingerprint"] != "stale-A-fingerprint"
        # transport re-derived from the holder — never the stale A socket.
        assert transport == Transport.unix(b_sock)
    finally:
        os.close(fd)


def test_held_generation_is_none_when_no_live_lock_holder(monkeypatch, tmp_path):
    """The SINGLETON LOCK is authoritative: with a perfectly valid .active.json but NO live lock
    holder, there is no authoritative incarnation -> None (the caller spawns/retries)."""
    monkeypatch.setenv("NELIX_HOME", str(tmp_path))
    importlib.reload(paths); importlib.reload(supervisor)
    paths.ensure_private_dir(paths.nelix_root())
    supervisor._write_state(os.getpid(), Transport.unix(str(paths.rpc_sock())))
    assert supervisor.held_generation() is None


def test_held_generation_tcp_pairs_active_json_only_on_matching_incarnation(monkeypatch, tmp_path):
    """A tcp lock holder carries NO token in the lock meta (unreachable from the lock alone), so
    held_generation() re-derives its transport from .active.json — but ONLY when .active.json names
    the SAME incarnation as the live lock holder. A stale/mismatched .active.json yields None
    (GENERATION_UNAVAILABLE/retryable), never a start routed to a superseded incarnation's port."""
    monkeypatch.setenv("NELIX_HOME", str(tmp_path))
    importlib.reload(paths); importlib.reload(supervisor)
    paths.ensure_private_dir(paths.nelix_root())
    insp = reaper.ProcessInspector()
    real_fp = insp.start_fingerprint(os.getpid())
    fd = singleton.acquire(paths.daemon_lock(),
                           {"pid": os.getpid(), "start_fingerprint": real_fp,
                            "transport": "tcp", "port": 4321})
    assert fd is not None
    try:
        # Matching incarnation: .active.json is the SAME (pid, fingerprint) as the lock holder ->
        # pair its (tokened) transport with the holder's identity.
        paths.state_file().write_text(json.dumps(
            {"pid": os.getpid(), "start_fingerprint": real_fp,
             "transport": "tcp", "host": "127.0.0.1", "port": 4321, "token": "tok-B"}))
        snap = supervisor.held_generation()
        assert snap is not None
        transport, inc = snap
        assert transport == Transport.tcp("127.0.0.1", 4321, "tok-B")
        assert inc == {"pid": os.getpid(), "start_fingerprint": real_fp}

        # Stale incarnation: .active.json's fingerprint no longer matches the live lock holder ->
        # cannot pair B's identity with A's transport -> None (never route to the stale port).
        paths.state_file().write_text(json.dumps(
            {"pid": os.getpid(), "start_fingerprint": "stale-A-fingerprint",
             "transport": "tcp", "host": "127.0.0.1", "port": 9, "token": "tok-A"}))
        assert supervisor.held_generation() is None
    finally:
        os.close(fd)
