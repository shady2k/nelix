import io, json
from daemon.obs import Logger
from daemon.app import warn_invalid_log_level
from daemon.config import LogLevelConfig


def test_invalid_log_level_warns_once():
    buf = io.StringIO()
    warn_invalid_log_level(Logger(level="info", stream=buf),
                           LogLevelConfig(level="info", invalid_value="loud", invalid_source="env"))
    recs = [json.loads(l) for l in buf.getvalue().splitlines() if l.strip()]
    assert len(recs) == 1 and recs[0]["event"] == "invalid_log_level"
    assert recs[0]["value"] == "loud" and recs[0]["source"] == "env" and recs[0]["using"] == "info"


def test_valid_log_level_no_warning():
    buf = io.StringIO()
    warn_invalid_log_level(Logger(stream=buf), LogLevelConfig(level="info"))
    assert buf.getvalue() == ""


def test_install_stack_dump_handler_enables_faulthandler():
    import faulthandler
    from daemon.app import install_stack_dump_handler
    install_stack_dump_handler()
    assert faulthandler.is_enabled()
