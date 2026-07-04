import io, json
from daemon.obs import redact, Logger


def _lines(buf):
    return [json.loads(l) for l in buf.getvalue().splitlines() if l.strip()]


def test_redact_masks_secrets_but_not_plain_text():
    assert "sk-secret123456789" not in redact("key=sk-secret123456789")
    assert redact("hello world") == "hello world"


def test_level_gating_suppresses_below_threshold():
    buf = io.StringIO()
    log = Logger(level="info", stream=buf)
    log.debug("session", "noisy", session_id="s1")
    log.info("session", "kept", session_id="s1")
    log.warning("session", "kept2")
    recs = _lines(buf)
    assert [r["event"] for r in recs] == ["kept", "kept2"]
    assert recs[0]["level"] == "info" and recs[0]["component"] == "session"


def test_debug_threshold_lets_debug_through():
    buf = io.StringIO()
    Logger(level="debug", stream=buf).debug("session", "shown")
    assert _lines(buf)[0]["event"] == "shown"


def test_audit_always_written_even_when_threshold_is_error():
    buf = io.StringIO()
    log = Logger(level="error", stream=buf, audit_stream=buf)
    log.info("session", "dropped")                       # below ERROR -> gone
    log.audit_task("s1", "demo", "do the thing")         # audit -> kept
    recs = _lines(buf)
    assert len(recs) == 1 and recs[0]["category"] == "audit"
    assert recs[0]["level"] == "info" and recs[0]["component"] == "task_delivered"


def test_audit_decision_shape_and_redaction():
    buf = io.StringIO()
    Logger(audit_stream=buf).audit_decision(
        "s1", "demo", "waiting_for_user", "evt-1",
        "Run: curl -H 'token=abcd1234efgh5678' ...")
    rec = _lines(buf)[0]
    assert rec["category"] == "audit" and rec["component"] == "decision"
    assert rec["event"] == "waiting_for_user" and rec["event_id"] == "evt-1"
    assert "abcd1234efgh5678" not in rec["grid"]


def test_field_aware_redaction():
    buf = io.StringIO()
    log = Logger(stream=buf)
    log.info("session", "spawn", session_id="s1",
             token="abcd1234efgh5678", api_key="zzz", leader_pid=4321,
             event_id="evt-9", reason="user_stop", msg="hi token=abcd1234efgh5678")
    rec = _lines(buf)[0]
    assert rec["token"] == "***" and rec["api_key"] == "***"      # secret field names masked
    assert rec["leader_pid"] == 4321                              # numbers untouched
    assert rec["event_id"] == "evt-9" and rec["reason"] == "user_stop"   # ids/reasons kept
    assert "abcd1234efgh5678" not in rec["msg"]                   # free-text redacted


def test_redact_keeps_benign_kebab_snake_identifiers():
    """nelix-4ei: a 16+ char kebab/snake identifier is normal text, not a secret.
    In s-9610d25c these were masked to ***, mangling the user's OWN delivered task
    ('правит ***.service'). A 16-char compound identifier must survive redact()."""
    for ident in ("acmetool-redirector", "ansible-playbook",
                  "acme_reconcile_log", "already-reloaded"):
        assert redact(ident) == ident, f"{ident!r} should survive redact() unredacted"


def test_redact_keeps_the_s9610d25c_delivered_task_text():
    """The live readability regression: the delivered task text round-trips with
    every kebab/snake identifier intact and no spurious *** masking."""
    task = ("правит acmetool-redirector.service через ansible-playbook, "
            "пишет в acme_reconcile_log; статус уже already-reloaded")
    out = redact(task)
    assert "acmetool-redirector" in out
    assert "ansible-playbook" in out
    assert "acme_reconcile_log" in out
    assert "already-reloaded" in out
    assert "***" not in out


def test_redact_still_masks_real_secrets_after_tightening():
    """Tightening _LONGTOK must not weaken the real secret masks: prefix, KV,
    Bearer, and the bare opaque-token catch-all."""
    assert redact("sk-" + "a" * 20) == "***"                          # sk- prefix
    assert redact("ghp_" + "A" * 16) == "***"                         # ghp_ prefix
    assert "eyJ" + "A" * 30 not in redact("Bearer eyJ" + "A" * 30)    # Bearer + JWT
    assert "ABCdef1234567890" not in redact("api_key=ABCdef1234567890")  # key=value
    # bare 40+ char opaque token — no prefix, no key= — the _LONGTOK catch-all
    opaque = "da39a3ee5e6b4b0d3255bfef95601890afd80709"  # 40 hex chars (sha1 of "")
    assert len(opaque) >= 40
    assert redact(opaque) == "***"
    assert opaque not in redact("session " + opaque + " started")


def test_error_exc_info_captures_redacted_traceback():
    buf = io.StringIO()
    log = Logger(stream=buf)
    try:
        raise RuntimeError("boom token=abcd1234efgh5678")
    except RuntimeError:
        log.error("session", "monitor_exception", session_id="s1", exc_info=True)
    rec = _lines(buf)[0]
    assert rec["event"] == "monitor_exception" and "RuntimeError" in rec["traceback"]
    assert "abcd1234efgh5678" not in rec["traceback"]
