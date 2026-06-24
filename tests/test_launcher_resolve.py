import pytest
from launcher_resolve import resolve_launcher


def test_auto_resolves_local(monkeypatch):
    monkeypatch.setenv("TERMINAL_ENV", "local")
    assert resolve_launcher("auto") == "local"


def test_auto_docker_is_post_mvp(monkeypatch):
    monkeypatch.setenv("TERMINAL_ENV", "docker")
    with pytest.raises(NotImplementedError):
        resolve_launcher("auto")


def test_explicit_local_under_docker_fails_closed(monkeypatch):
    monkeypatch.setenv("TERMINAL_ENV", "docker")
    with pytest.raises(PermissionError):
        resolve_launcher("local")
    assert resolve_launcher("local", allow_weaker=True) == "local"
