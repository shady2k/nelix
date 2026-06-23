from daemon.events import EventQueue


def test_publish_seq_and_after():
    q = EventQueue()
    e1 = q.publish("waiting_for_user", "a", "waiting_for_user")
    e2 = q.publish("done", "b", "done_candidate")
    assert (e1.seq, e2.seq) == (1, 2)
    assert q.latest_after(0).seq == 1
    assert q.latest_after(1).seq == 2
    assert q.latest_after(2) is None


def test_pending_and_answer():
    q = EventQueue()
    e = q.publish("waiting_for_user", "y/n?", "waiting_for_user")
    assert q.pending().event_id == e.event_id
    assert q.mark_answered(e.event_id) is True
    assert q.pending() is None
