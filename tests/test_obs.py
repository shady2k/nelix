import io, json
from conftest import EXECUTOR
from daemon.obs import redact, Logger


def test_redact_masks_secrets():
    assert "sk-secret123456789" not in redact("key=sk-secret123456789")
    assert redact("ZAI_API_KEY=abcd1234efgh5678").endswith("***")
    assert redact("hello world") == "hello world"


def test_logger_writes_json_line_with_correlation():
    buf = io.StringIO()
    log = Logger(stream=buf)
    log.event("session", "info", session_id="s1", executor=EXECUTOR, msg="started")
    rec = json.loads(buf.getvalue().strip())
    assert rec["session_id"] == "s1" and rec["component"] == "session" and rec["msg"] == "started"


def test_audit_decision_redacts_grid():
    buf = io.StringIO()
    log = Logger(audit_stream=buf)
    log.audit_decision("s1", EXECUTOR, "waiting_for_user", "evt-1",
                       "Run: curl -H 'token=abcd1234efgh5678' ...")
    rec = json.loads(buf.getvalue().strip())
    assert rec["event_id"] == "evt-1" and "abcd1234efgh5678" not in rec["grid"]


def test_redact_masks_bearer_tokens():
    """Mask Bearer <token> regardless of token length."""
    result = redact("Bearer sk-abc123")
    assert "sk-abc123" not in result
    assert "***" in result


def test_redact_masks_short_prefixed_credentials():
    """Mask tokens by known secret prefixes (sk-, ghp_, gho_, ghs_, xox, AKIA, eyJ)."""
    assert "sk-abc123" not in redact("token is sk-abc123")
    assert "ghp_abcdEFGH1234" not in redact("token is ghp_abcdEFGH1234")
    assert "gho_abc123" not in redact("gho_abc123 leaked")
    assert "ghs_xyz789" not in redact("secret: ghs_xyz789")
    assert "xoxb-abc123def456" not in redact("slack xoxb-abc123def456")
    assert "AKIA1234567890AB" not in redact("AWS AKIA1234567890AB")
    assert "eyJhbGc" not in redact("jwt: eyJhbGc")


def test_redact_no_over_redaction():
    """Ensure normal words are not over-redacted."""
    assert redact("hello world") == "hello world"
    assert redact("this is a test") == "this is a test"
