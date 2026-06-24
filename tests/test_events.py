from daemon.events import EventQueue


def test_global_seq_across_sessions():
    q = EventQueue()
    a = q.publish("s1", "claude", "waiting_for_user", "a", "waiting_for_user")
    b = q.publish("s2", "codex", "done", "b", "done_candidate")
    assert (a.seq, b.seq) == (1, 2)
    assert a.session_id == "s1" and b.executor == "codex"
    assert q.latest_after(1).seq == 2 and q.latest_after(2) is None


def test_pending_filtered_by_session():
    q = EventQueue()
    e1 = q.publish("s1", "claude", "waiting_for_user", "y/n?", "waiting_for_user")
    q.publish("s2", "codex", "waiting_for_user", "y/n?", "waiting_for_user")
    assert q.pending("s1").event_id == e1.event_id
    assert q.mark_answered(e1.event_id) is True
    assert q.pending("s1") is None
    assert q.pending("s2") is not None


def test_wait_event_blocks_then_returns():
    import threading, time
    q = EventQueue()
    def producer():
        time.sleep(0.05); q.publish("s1", "claude", "done", "x", "done_candidate")
    threading.Thread(target=producer, daemon=True).start()
    assert q.wait_event(after_seq=0, timeout=2).seq == 1
    assert q.wait_event(after_seq=1, timeout=0.1) is None


def test_publish_carries_range_and_hint_under_lock():
    from daemon.events import EventQueue
    q = EventQueue()
    e = q.publish("s1", "demo", "waiting_for_user", "sum", "idle_prompt",
                  turn_index=2, range=(5, 9), hint="needs_permission", hung=False)
    assert e.turn_index == 2 and e.range == (5, 9) and e.hint == "needs_permission"
    pend = q.pending()
    assert pend is not None and pend.event_id == e.event_id
    q.mark_answered(e.event_id)
    assert q.pending() is None
