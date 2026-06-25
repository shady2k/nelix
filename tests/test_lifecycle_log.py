import io, json
from daemon.obs import Logger
from daemon import lifecycle_log as ll


def _rec(buf):
    return json.loads(buf.getvalue().splitlines()[-1])


def test_redact_argv_keeps_structure_masks_secrets_and_paths():
    # fictional argv ONLY — never put real infra (commands, vault paths, $HOME) in tests.
    argv = ["runner", "-secret=kv/app", "-no-prefix", "--", "/opt/app/launch.sh"]
    red = ll.redact_argv(argv)
    assert red[0] == "runner" and red[2] == "-no-prefix" and red[3] == "--"
    assert "kv/app" not in red[1]
    assert red[-1] == "launch.sh"                   # absolute path collapsed to basename
    assert "/opt/app" not in " ".join(red)          # no fs structure leaked


def test_command_fingerprint_is_stable_and_short():
    fp1 = ll.command_fingerprint(["runner", "-no-prefix"])
    fp2 = ll.command_fingerprint(["runner", "-no-prefix"])
    assert fp1 == fp2 and len(fp1) == 16


def test_log_executor_spawned_fields():
    buf = io.StringIO()
    ll.log_executor_spawned(Logger(stream=buf), session_id="s1", executor="agent",
                            leader_pid=42, leader_pgid=42,
                            argv=["runner", "-secret=kv/app"], launcher="LocalLauncher")
    rec = _rec(buf)
    assert rec["event"] == "executor_spawned" and rec["leader_pid"] == 42
    assert rec["process_role"] == "pty_leader" and rec["launcher"] == "LocalLauncher"
    assert "kv/app" not in json.dumps(rec["argv_redacted"])
    assert "argv" not in rec and len(rec["command_fingerprint"]) == 16


def test_log_executor_exited_warns_on_nonzero():
    buf = io.StringIO()
    ll.log_executor_exited(Logger(stream=buf), session_id="s1", reason="crashed",
                           leader_exit_code=1, leader_signal=None, status_available=True,
                           alive_for=0.5, task_delivery="pending", screen_fingerprint="abc")
    assert _rec(buf)["level"] == "warning"


def test_log_executor_exited_info_on_clean_delivered_exit():
    buf = io.StringIO()
    ll.log_executor_exited(Logger(stream=buf), session_id="s1", reason="exited",
                           leader_exit_code=0, leader_signal=None, status_available=True,
                           alive_for=12.0, task_delivery="delivered", screen_fingerprint="abc")
    assert _rec(buf)["level"] == "info"
