# pyright: reportMissingParameterType=false
from io import StringIO
from unittest.mock import patch

from team_harness.ui.terminal import clear_terminal_progress
from team_harness.ui.terminal import is_iterm2
from team_harness.ui.terminal import register_progress_cleanup
from team_harness.ui.terminal import set_terminal_progress


def test_is_iterm2_with_term_program(monkeypatch):
    monkeypatch.setenv("TERM_PROGRAM", "iTerm.app")
    monkeypatch.delenv("ITERM_SESSION_ID", raising=False)
    assert is_iterm2() is True


def test_is_iterm2_with_session_id(monkeypatch):
    monkeypatch.delenv("TERM_PROGRAM", raising=False)
    monkeypatch.setenv("ITERM_SESSION_ID", "w0t0p0:12345")
    assert is_iterm2() is True


def test_is_iterm2_false(monkeypatch):
    monkeypatch.delenv("TERM_PROGRAM", raising=False)
    monkeypatch.delenv("ITERM_SESSION_ID", raising=False)
    assert is_iterm2() is False


def test_set_terminal_progress_writes_osc(monkeypatch):
    monkeypatch.setenv("TERM_PROGRAM", "iTerm.app")
    monkeypatch.delenv("TMUX", raising=False)
    buf = StringIO()
    with patch("team_harness.ui.terminal.sys.stdout", buf):
        buf.isatty = lambda: True
        set_terminal_progress()
    assert "\033]9;4;3;" in buf.getvalue()


def test_set_terminal_progress_noop_without_iterm2(monkeypatch):
    monkeypatch.delenv("TERM_PROGRAM", raising=False)
    monkeypatch.delenv("ITERM_SESSION_ID", raising=False)
    monkeypatch.delenv("TMUX", raising=False)
    buf = StringIO()
    with patch("team_harness.ui.terminal.sys.stdout", buf):
        buf.isatty = lambda: True
        set_terminal_progress()
    assert buf.getvalue() == ""


def test_set_terminal_progress_noop_in_tmux(monkeypatch):
    monkeypatch.setenv("TERM_PROGRAM", "iTerm.app")
    monkeypatch.setenv("TMUX", "/tmp/tmux-501/default,12345,0")
    buf = StringIO()
    with patch("team_harness.ui.terminal.sys.stdout", buf):
        buf.isatty = lambda: True
        set_terminal_progress()
    assert buf.getvalue() == ""


def test_clear_terminal_progress_writes_osc(monkeypatch):
    monkeypatch.setenv("TERM_PROGRAM", "iTerm.app")
    monkeypatch.delenv("TMUX", raising=False)
    buf = StringIO()
    with patch("team_harness.ui.terminal.sys.stdout", buf):
        buf.isatty = lambda: True
        clear_terminal_progress()
    assert "\033]9;4;0;" in buf.getvalue()


def test_set_terminal_progress_noop_not_tty(monkeypatch):
    monkeypatch.setenv("TERM_PROGRAM", "iTerm.app")
    monkeypatch.delenv("TMUX", raising=False)
    buf = StringIO()
    with patch("team_harness.ui.terminal.sys.stdout", buf):
        # StringIO.isatty() returns False by default
        set_terminal_progress()
    assert buf.getvalue() == ""


def test_clear_terminal_progress_noop_without_iterm2(monkeypatch):
    monkeypatch.delenv("TERM_PROGRAM", raising=False)
    monkeypatch.delenv("ITERM_SESSION_ID", raising=False)
    monkeypatch.delenv("TMUX", raising=False)
    buf = StringIO()
    with patch("team_harness.ui.terminal.sys.stdout", buf):
        buf.isatty = lambda: True
        clear_terminal_progress()
    assert buf.getvalue() == ""


def test_clear_terminal_progress_noop_in_tmux(monkeypatch):
    monkeypatch.setenv("TERM_PROGRAM", "iTerm.app")
    monkeypatch.setenv("TMUX", "/tmp/tmux-501/default,12345,0")
    buf = StringIO()
    with patch("team_harness.ui.terminal.sys.stdout", buf):
        buf.isatty = lambda: True
        clear_terminal_progress()
    assert buf.getvalue() == ""


def test_register_progress_cleanup_idempotent(monkeypatch):
    import team_harness.ui.terminal as mod

    monkeypatch.setattr(mod, "_atexit_registered", False)
    with patch("team_harness.ui.terminal.atexit.register") as mock_register:
        register_progress_cleanup()
        register_progress_cleanup()
        mock_register.assert_called_once()


def test_set_terminal_progress_emits_exact_sequence(monkeypatch):
    monkeypatch.setenv("TERM_PROGRAM", "iTerm.app")
    monkeypatch.delenv("TMUX", raising=False)
    buf = StringIO()
    with patch("team_harness.ui.terminal.sys.stdout", buf):
        buf.isatty = lambda: True
        set_terminal_progress()
    assert buf.getvalue() == "\033]9;4;3;\007"


def test_clear_terminal_progress_emits_exact_sequence(monkeypatch):
    monkeypatch.setenv("TERM_PROGRAM", "iTerm.app")
    monkeypatch.delenv("TMUX", raising=False)
    buf = StringIO()
    with patch("team_harness.ui.terminal.sys.stdout", buf):
        buf.isatty = lambda: True
        clear_terminal_progress()
    assert buf.getvalue() == "\033]9;4;0;\007"
