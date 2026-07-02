"""Tier-3 session-loop replay harness (nelix-5gc Task 6).

Tiers 1-2 (frame conformance + raw-replay sequence oracles) drive the renderer + ClaudeDriver +
BeliefEngine directly; they BYPASS the real ``daemon.session.Session`` — so the GLUE that only the
Session owns (event publication, ``respond`` routing to ``driver.select_option``, delivery
confirmation, terminal publication) is untested by them.

``tests/test_session.py`` already drives the REAL Session by feeding its ``FakeHandle`` a list of
FABRICATED frame strings. ``RawReplayHandle`` is the SAME pattern with the SAME method surface, but
its frames come from a REAL capture (``tests/_replay.replay_frames`` for a ``.raw`` / the timestamped
``.capture`` stream) instead of hand-authored strings. The two builders below mirror the
``_session()`` (post-delivery ``_loop``) and ``start()``/``_run`` (delivery) wiring from
``tests/test_session.py`` so the real Session is driven over recorded bytes with a ``FakeClock`` —
deterministically, no real PTY/threads.
"""
import json
import threading
import time
from pathlib import Path

from daemon.session import Session
from daemon.hooks import HookEvent
from daemon.dialog import Dialog
from daemon.drivers.claude import ClaudeDriver
from daemon.transcript_builder import TranscriptBuilder
from daemon.events import EventQueue
from daemon.clock import FakeClock
from tests._replay import replay_frames

_GOLDEN = Path(__file__).resolve().parent / "golden" / "claude" / "_regression"


class Spec:
    """Executor spec mirroring tests/test_session.py::Spec (the fields Session/BeliefEngine read)."""
    driver = "claude"
    settle_seconds = 1.5
    respond_write_seconds = 5.0
    respond_confirm_seconds = 0.3
    delivery_confirm_seconds = 2.0
    max_idle_seconds = 600.0
    startup_timeout_seconds = 60.0
    tail_lines = 100
    status_tail_chars = 4000
    dialog_page_chars = 8000
    spool_max_bytes = 1_000_000

    def argv(self):
        return ["runner", "--interactive"]


def raw_frames(name):
    """Distinct rendered frames (list[str]) from a committed ``.raw`` capture."""
    return [frame for _, frame in replay_frames((_GOLDEN / name).read_bytes())]


def capture_frames(name, *, cols=120, rows=40):
    """Distinct rendered frames (list[str]) from a committed timestamped ``.capture`` stream.

    Same distinct-frame contract as tests/_replay.replay_frames, but sourced from a synthesized
    capture (read via daemon.capture.read_capture) rather than a raw byte blob.
    """
    from daemon.capture import read_capture
    from daemon.renderer.ghostty import GhosttyRenderer
    r = GhosttyRenderer(cols, rows)
    seen, out = None, []
    try:
        for _, chunk in read_capture(_GOLDEN / name):
            r.feed(chunk)
            frame = r.render()
            if frame == seen:
                continue
            seen = frame
            out.append(frame)
    finally:
        r.close()
    return out


class RawReplayHandle:
    """Scripted PTY handle with the SAME method surface as tests/test_session.py::FakeHandle, but
    its frames come from a REAL capture instead of fabricated strings.

    render() walks ``frames``; the process stays alive and the loop is terminated by setting ``stop``
    once the last frame is reached (so observe never sees a false exit). Each pump() advances the
    injected FakeClock by ``step`` so the engine's settle/grace windows elapse deterministically (no
    real sleeps, no time.* in the belief path). write() is recorded so a test can spy the PTY bytes
    Session actuates (e.g. driver.select_option's digit+CR)."""

    def __init__(self, frames, stop=None, clock=None, step=1.0):
        self.frames = list(frames)
        self.i = -1
        self.writes = []
        self._stop = stop
        self._clock = clock
        self._step = step

    def pump(self, timeout=0.1):
        self.i += 1
        if self._clock is not None:
            self._clock.advance(self._step)
        if self._stop is not None and self.i >= len(self.frames) - 1:
            self._stop.set()
        return True

    def render(self):
        return self.frames[min(self.i, len(self.frames) - 1)]

    def is_alive(self):
        return True

    def exit_code(self):
        return None

    def write(self, data, timeout=None, drain_output=False):
        self.writes.append(data)

    def finalize(self):
        dialog = getattr(self, '_dialog', None)
        if dialog is not None:
            for ln in self.render().splitlines():
                t = ln.rstrip()
                if t:
                    dialog.add_agent_line(t)

    def leader_pid(self):
        return 4242

    def leader_pgid(self):
        return 4242

    def assert_leader_is_group_leader(self):
        pid, pgid = self.leader_pid(), self.leader_pgid()
        if pid is None or pid != pgid:
            raise RuntimeError(f"pty leader {pid} is not its own group leader (pgid={pgid})")

    def leader_status(self):
        from daemon.launchers.base import LeaderStatus
        return LeaderStatus(alive=True, exit_code=None, signal=None, status_available=False)

    def close(self):
        pass


def _wire(tmp_path, spec, logger=None):
    """Build a Session wired exactly like tests/test_session.py: real ClaudeDriver, FakeClock,
    EventQueue, private Dialog. Returns (sess, ev, clock)."""
    ev = EventQueue()
    clock = FakeClock(0.0)
    sess = Session("s1", "demo", ClaudeDriver(), None, spec, ev, clock=clock, logger=logger)
    sess._dialog = Dialog(tmp_path / "s1", tail_lines=spec.tail_lines,
                          spool_max_bytes=spec.spool_max_bytes)
    sess._clock = clock
    return sess, ev, clock


def replay_session(tmp_path, frames, *, spec=None, step=1.0, pad_last=0):
    """Mirror tests/test_session.py::_session(): a Session whose RawReplayHandle replays ``frames``,
    primed at task_delivery='delivered' so a test can drive the POST-delivery run loop (sess._loop())
    over a real capture. ``pad_last`` repeats the final frame so a settle-on-stable decision
    (idle_confirm_window) actually publishes before the handle sets _stop. Returns (sess, ev)."""
    spec = spec or Spec()
    sess, ev, clock = _wire(tmp_path, spec)
    seq = list(frames) + [frames[-1]] * pad_last if (frames and pad_last) else list(frames)
    sess._handle = RawReplayHandle(seq, stop=sess._stop, clock=clock, step=step)
    sess._handle._dialog = sess._dialog
    sess._task_delivery = "delivered"
    return sess, ev


def delivery_run(tmp_path, frames, *, task, spec=None, step=1.0, pad_last=0,
                 logger=None, on_terminal=None):
    """Drive the REAL Session DELIVERY path (Session._run's pre-delivery loop + _deliver_task) over a
    real capture, synchronously and deterministically.

    Mirrors Session.start()'s wiring (held task, dialog, transcript, handle, spawn_ts) but runs
    _run() inline on the calling thread. _wait_until_ready (a real-wall-clock settle loop) is
    neutralized so the in-process FakeClock handle drives every frame; callers must also neutralize
    daemon.session.time.sleep (used by _ensure_ask_mode). ``pad_last`` repeats the final frame so a
    silent/held screen persists across enough pumps for the injected clock to cross a startup deadline
    before the handle sets _stop. ``logger`` captures the lifecycle/forensic trail; ``on_terminal``
    spies the slot-free callback the manager wires. Returns (sess, ev, handle)."""
    spec = spec or Spec()
    sess, ev, clock = _wire(tmp_path, spec, logger=logger)
    sess._task_raw = task
    sess._task = task
    sess._transcript = TranscriptBuilder(sess._dialog, sess._driver, sess._rows)
    sess._spawn_ts = 0.0
    if on_terminal is not None:
        sess.on_terminal = on_terminal
    seq = list(frames) + [frames[-1]] * pad_last if (frames and pad_last) else list(frames)
    handle = RawReplayHandle(seq, stop=sess._stop, clock=clock, step=step)
    handle._dialog = sess._dialog
    sess._handle = handle
    sess._wait_until_ready = lambda *a, **k: None   # neutralize the real-wall-clock settle wait
    sess._run()
    return sess, ev, handle


def respond_via_monitor(sess, answer, decision_id, frame, *, timeout=10.0, before_drain=None):
    """Deterministic single-process model of the REAL two-thread pre-delivery blocked answer
    (C1: monitor is the sole writer). ``respond()`` runs on a worker (RPC) thread: it ENQUEUES the
    answer and blocks on the monitor. THIS thread IS the monitor: once the enqueue is visible it
    drains the answer against ``frame`` — SUBMIT (write the keystrokes) when the modal is still on
    screen + delivery pending, ABORT (nothing typed) otherwise — then joins the worker and returns
    its RespondOutcome. The spin on ``_pending_answer`` models the monitor noticing the answer on its
    next tick (deterministic: the enqueue is immediate).

    ``before_drain`` (optional) runs on THIS (monitor) thread in the enqueue→drain window — after
    respond() has committed the answer but before the monitor re-observes the screen to drain it.
    It models a monitor-side transition that lands in that exact window (e.g. delivery confirming
    pending->delivered, the TOCTOU the single-writer drain's _task_delivery re-check closes)."""
    box = {}

    def rpc():
        try:
            box["out"] = sess.respond(answer, decision_id=decision_id)
        except BaseException as exc:        # pragma: no cover - surfaced via the assert below
            box["err"] = exc

    t = threading.Thread(target=rpc, daemon=True)
    t.start()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with sess._lock:
            enqueued = sess._pending_answer is not None
        if enqueued:
            break
        time.sleep(0)
    else:
        t.join(timeout=1.0)
        raise AssertionError(box.get("err") or "respond() never enqueued a pending answer")
    if before_drain is not None:
        before_drain(sess)            # monitor-side transition in the enqueue→drain window
    sess._drain_pending_answer(frame)
    t.join(timeout=timeout)
    if "err" in box:
        raise box["err"]
    return box["out"]


def delivery_drive(tmp_path, frames, *, task, spec=None, step=1.0, respond=None,
                   max_iters=None, logger=None):
    """Drive Session._run's PRE-DELIVERY loop body frame-by-frame over a REAL capture — the
    deterministic, single-threaded equivalent of the monitor's delivery phase, with one injection
    point ``respond`` a test uses to answer a modal at exactly the moment the orchestrator would
    (right after the blocked decision is published), reproducing the pre-delivery phantom-blocked
    race.

    Mirrors Session._run's loop body (pump -> render -> normalized-frame tracking -> _delivery_tick)
    and stops once ``task_delivery`` leaves 'pending'. ``respond``, if given, is called AFTER each
    tick with the current pending decision dict (or None); when it returns a non-None answer string
    the helper routes it through ``respond_via_monitor`` — the C1 monitor-is-sole-writer path — so a
    pre-delivery blocked answer is written by THIS (monitor) thread against the current frame, not by
    respond() on the RPC thread. As in delivery_run, _wait_until_ready (a real-wall-clock settle) is
    neutralized and callers must neutralize daemon.session.time.sleep (used by _ensure_ask_mode).
    Returns (sess, ev, handle)."""
    spec = spec or Spec()
    sess, ev, clock = _wire(tmp_path, spec, logger=logger)
    sess._task_raw = task
    sess._task = task
    sess._transcript = TranscriptBuilder(sess._dialog, sess._driver, sess._rows)
    sess._spawn_ts = 0.0
    handle = RawReplayHandle(list(frames), stop=sess._stop, clock=clock, step=step)
    handle._dialog = sess._dialog
    sess._handle = handle
    sess._wait_until_ready = lambda *a, **k: None   # neutralize the real-wall-clock settle wait
    drv = sess._driver
    it = 0
    while not sess._stop.is_set() and sess._task_delivery == "pending":
        handle.pump(0.1)
        sess._last_byte = 0.0
        with sess._lock:
            frame = handle.render()
        norm = drv.normalize_frame(frame)
        if norm != sess._norm:
            sess._norm = norm
            sess._norm_since = 0.0
            sess._last_progress = 0.0
        # C1: drain a previously-enqueued blocked answer on the current frame BEFORE the tick (the
        # monitor is the sole writer; this mirrors _run calling _drain_pending_answer each tick).
        sess._drain_pending_answer(frame)
        sess._delivery_tick(frame)
        if respond is not None:
            dec = sess._decision
            if dec is not None and "event_id" in dec:
                ans = respond(dec, sess)
                if ans is not None:
                    respond_via_monitor(sess, ans, dec["decision_id"], frame)
        if not handle.is_alive():
            break
        it += 1
        if max_iters is not None and it >= max_iters:
            break
    return sess, ev, handle


def replay_hooks(sess, path):
    """Replay a `.jsonl` of raw Claude hook payloads through the REAL Session (Task 12): enqueue each
    event via ``sess.on_hook`` then drain it with ``sess._loop_once()`` — the exact ``on_hook`` queue +
    ``_loop`` drain path a live ``curl`` from a hook drives, in recorded order.

    JSONL has no comment syntax, so lines that begin with ``#`` (each fixture's synthesis-provenance
    header) and blank lines are skipped. Each payload's keys mirror the daemon ``/hook`` route
    (``rpc_server`` builds a ``HookEvent`` the same way): ``hook_event_name`` + optional ``tool_name`` /
    ``tool_input`` / ``is_interrupt`` / (``message``|``matcher``). The session id is the live ``sess``'s
    (a fixture's ``session_id`` field is documentation only — the daemon takes it from the URL, not the
    body). Returns a trail of ``(raw_event, control_state)`` after each drained event so a caller can
    assert the whole state trajectory (e.g. "busy for every mid-flight event, idle only on the Stop")."""
    trail = []
    for line in Path(path).read_text().splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        body = json.loads(s)
        ev = HookEvent(session_id=sess._id, event=body["hook_event_name"],
                       tool_name=body.get("tool_name"), tool_input=body.get("tool_input") or {},
                       is_interrupt=bool(body.get("is_interrupt")),
                       notification=body.get("message") or body.get("matcher"))
        sess.on_hook(ev)
        sess._loop_once()
        trail.append((body["hook_event_name"], sess.snapshot()["control_state"]))
    return trail
