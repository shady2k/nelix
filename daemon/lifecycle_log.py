"""Daemon lifecycle event vocabulary. Pure helpers over a generic Logger so the
event schema (required fields) lives in one place and Session/Manager call sites
stay one-liners. argv redaction + fingerprinting live HERE, not in Logger."""
import hashlib
import os

from daemon.obs import redact


def redact_argv(argv):
    out = []
    for tok in argv:
        tok = str(tok)
        # collapse absolute paths to their basename (keep the wrapper command name,
        # drop local fs structure), then run secret-pattern redaction on the token.
        if tok.startswith("/") and len(tok) > 1:
            tok = os.path.basename(tok.rstrip("/")) or tok
        out.append(redact(tok))
    return out


def command_fingerprint(argv_redacted):
    joined = "\x00".join(argv_redacted).encode()
    return hashlib.sha256(joined).hexdigest()[:16]


def log_executor_spawned(log, *, session_id, executor, leader_pid, leader_pgid, argv, launcher):
    red = redact_argv(argv)
    log.info("session", "executor_spawned", session_id=session_id, executor=executor,
             leader_pid=leader_pid, leader_pgid=leader_pgid, argv_redacted=red,
             command_fingerprint=command_fingerprint(red), launcher=launcher,
             process_role="pty_leader")


def log_belief_transition(log, *, session_id, prompt_kind, affordances, busy_reason, liveness,
                          semantic_fp, content_fp, prompt_fp, heartbeat_fp, quiet_elapsed, rule):
    """The decision/transition trail (spec §8): one line per engine action carrying the observation
    that drove it (prompt_kind + affordances + busy_reason + liveness), the fingerprints, and which
    rule fired. Observability and the replay test oracle are the same artifact — this is that line."""
    log.info("belief", "belief_transition", session_id=session_id, rule=rule,
             prompt_kind=prompt_kind, affordances=affordances, busy_reason=busy_reason,
             liveness=liveness, quiet_elapsed=round(quiet_elapsed, 3),
             semantic_fp=semantic_fp, content_fp=content_fp, prompt_fp=prompt_fp,
             heartbeat_fp=heartbeat_fp)


def log_executor_exited(log, *, session_id, reason, leader_exit_code, leader_signal,
                        status_available, alive_for, task_delivery, screen_fingerprint):
    clean = (reason in ("exited", "done") and leader_signal is None
             and leader_exit_code == 0 and task_delivery == "delivered")
    level = "info" if clean else "warning"
    log.emit(level, "session", "executor_exited", session_id, reason=reason,
             leader_exit_code=leader_exit_code, leader_signal=leader_signal,
             status_available=status_available, alive_for=alive_for,
             task_delivery=task_delivery, screen_fingerprint=screen_fingerprint)
