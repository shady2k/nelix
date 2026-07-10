from daemon.events import EventQueue, RESPONDABLE_KINDS


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
    assert q.mark_answered(e1.event_id) == e1.seq        # returns the answered seq, not bool
    assert q.pending("s1") is None
    assert q.pending("s2") is not None


def test_mark_answered_returns_seq():
    q = EventQueue()
    e = q.publish("s-1", "a", "waiting_for_user", "?", "idle_prompt")
    assert q.mark_answered(e.event_id) == e.seq
    assert q.mark_answered("evt-nope") is None


def test_latest_seq_and_session_scoped_wait():
    q = EventQueue()
    assert q.latest_seq() == 0
    a = q.publish("s-A", "x", "blocked", "", "startup_interstitial")
    q.publish("s-B", "x", "blocked", "", "startup_interstitial")
    assert q.latest_seq() == 2
    # session-scoped: from before A, only s-A's event is returned for session s-A
    assert q.latest_after(0, session_id="s-A") is a
    assert q.latest_after(a.seq, session_id="s-A") is None


def test_no_stale_wake_after_respond():
    # The cursor model: after answering A, arming past A.seq must NOT re-return A — only the
    # NEXT event (B) wakes the orchestrator. This is what kills the stale-wake loop.
    q = EventQueue()
    a = q.publish("s1", "claude", "waiting_for_user", "?", "idle_prompt")
    assert q.mark_answered(a.event_id) == a.seq
    assert q.wait_event(after_seq=a.seq, timeout=0.1, session_id="s1") is None
    b = q.publish("s1", "claude", "waiting_for_user", "?2", "idle_prompt")
    assert q.wait_event(after_seq=a.seq, timeout=0.1, session_id="s1") is b


def test_wait_event_blocks_then_returns():
    import threading, time
    q = EventQueue()
    def producer():
        time.sleep(0.05); q.publish("s1", "claude", "done", "x", "done_candidate")
    threading.Thread(target=producer, daemon=True).start()
    assert q.wait_event(after_seq=0, timeout=2, session_id="s1").seq == 1
    assert q.wait_event(after_seq=1, timeout=0.1, session_id="s1") is None


def test_wait_event_requires_session_id():
    # session_id is a REQUIRED parameter: the wait primitive must be STRUCTURALLY incapable of a
    # global (cross-session) wait, which would deliver one session's event to another's orchestrator.
    # Omitting it is a programming error (TypeError), never a silent global wait.
    import pytest
    q = EventQueue()
    with pytest.raises(TypeError):
        q.wait_event(after_seq=0, timeout=0.1)


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


def test_resolve_decision_clears_all_reemits_of_that_decision():
    # A pause can span several events (a re-emit / nag) sharing one decision_id. Resolving the
    # decision (by id) must clear ALL of them — targeted, not a blanket session-answer (IMPORTANT-7).
    q = EventQueue()
    q.publish("s1", "x", "blocked", "trust?", "startup_interstitial",
              requires_response=True, decision_id="dec-A")
    b = q.publish("s1", "x", "blocked", "trust?", "startup_interstitial",
                  requires_response=True, hung=True, decision_id="dec-A")
    # a DIFFERENT decision for the same session must NOT be resolved
    other = q.publish("s1", "x", "waiting_for_user", "?", "idle_prompt",
                      requires_response=True, decision_id="dec-B")
    q.publish("s2", "x", "waiting_for_user", "?", "idle_prompt",
              requires_response=True, decision_id="dec-C")
    assert q.resolve_decision("dec-A", "answered") == b.seq    # highest seq of that decision
    assert q.pending("s1") is other                            # dec-B still pending (not resolved)
    assert q.pending("s2") is not None                         # other session untouched
    assert q.resolve_decision("dec-A", "answered") is None     # nothing left to resolve for dec-A


def test_resolve_decision_records_the_reason():
    q = EventQueue()
    e = q.publish("s1", "x", "waiting_for_user", "?", "idle_prompt",
                  requires_response=True, decision_id="dec-Z")
    assert e.resolved_reason is None
    q.resolve_decision("dec-Z", "withdrawn")
    assert e.resolved_reason == "withdrawn"
    assert q.pending("s1") is None                             # withdrawn -> not pending


def test_publish_on_publish_runs_before_waiters_are_notified():
    # Install-before-notify: the on_publish hook (where the session installs its decision) runs
    # while the event is reserved but BEFORE waiters wake — so a woken status pull never observes
    # an event without its decision.
    import threading, time as _t
    q = EventQueue()
    order = []
    started = threading.Event()

    def waiter():
        started.set()
        evt = q.wait_event(after_seq=0, timeout=2, session_id="s1")
        order.append(("woke", evt.event_id if evt else None))

    t = threading.Thread(target=waiter, daemon=True); t.start()
    assert started.wait(1)
    _t.sleep(0.05)                                      # let the waiter block on the condition
    e = q.publish("s1", "x", "waiting_for_user", "?", "idle_prompt",
                  on_publish=lambda ev: order.append(("installed", ev.event_id)))
    t.join(2)
    assert order[0] == ("installed", e.event_id)        # install BEFORE the waiter wakes
    assert order[1] == ("woke", e.event_id)


def test_latest_seq_global_and_per_session():
    q = EventQueue()
    a = q.publish("s-a", "ex", "working", "", "working")
    b = q.publish("s-b", "ex", "working", "", "working")
    c = q.publish("s-a", "ex", "waiting_for_user", "", "working")
    assert q.latest_seq() == c.seq          # global unchanged
    assert q.latest_seq("s-a") == c.seq
    assert q.latest_seq("s-b") == b.seq
    assert q.latest_seq("s-missing") == 0


def test_latest_seqs_single_pass():
    q = EventQueue()
    q.publish("s-a", "ex", "working", "", "working")
    b = q.publish("s-b", "ex", "working", "", "working")
    c = q.publish("s-a", "ex", "blocked", "", "working")
    assert q.latest_seqs(["s-a", "s-b", "s-x"]) == {"s-a": c.seq, "s-b": b.seq, "s-x": 0}


def test_blocked_event_is_pending_and_carries_fields():
    q = EventQueue()
    e = q.publish("s-1", "agent", "blocked", "trust?", "startup_interstitial",
                  hint="task_not_delivered", task_delivery="pending",
                  requires_response=True, screen_excerpt="❯ 1. Yes")
    assert e.task_delivery == "pending" and e.requires_response is True
    assert e.screen_excerpt == "❯ 1. Yes"
    assert q.pending("s-1") is e                 # blocked is respondable
    q.mark_answered(e.event_id)
    assert q.pending("s-1") is None
    assert "blocked" in RESPONDABLE_KINDS and "waiting_for_user" in RESPONDABLE_KINDS
