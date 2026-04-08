# pyright: reportMissingParameterType=false

from unittest.mock import MagicMock

from team_harness.agents.manager import AgentManager
from team_harness.tracking.context import ContextTracker
from team_harness.ui.console import HarnessConsole
from team_harness.ui.console import make_console
from team_harness.ui.console import PlainConsole


def test_make_console_plain(monkeypatch, tmp_path):
    monkeypatch.setattr("sys.stdout.isatty", lambda: False)
    console = make_console(
        ctx=ContextTracker(model_id="m", model_limit=100),
        manager=AgentManager(),
        run_dir=tmp_path,
    )
    assert isinstance(console, PlainConsole)


def test_make_console_harness_when_stdout_is_tty(monkeypatch, tmp_path):
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    console = make_console(
        ContextTracker(model_id="m", model_limit=100), AgentManager(), tmp_path
    )
    assert isinstance(console, HarnessConsole)


def test_plain_console_pause_resume_noop(tmp_path):
    console = PlainConsole(
        ContextTracker(model_id="m", model_limit=100), AgentManager(), tmp_path
    )

    console.pause_for_input()
    console.resume_after_input()


def test_harness_console_pause_stops_live_when_started(monkeypatch, tmp_path):
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    console = HarnessConsole(
        ContextTracker(model_id="m", model_limit=100), AgentManager(), tmp_path
    )
    console._live = MagicMock()
    console._live_enabled = True
    console._live_running = True

    console.pause_for_input()

    console._live.stop.assert_called_once()


def test_harness_console_resume_restarts_and_updates_live(monkeypatch, tmp_path):
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    console = HarnessConsole(
        ContextTracker(model_id="m", model_limit=100), AgentManager(), tmp_path
    )
    console._live = MagicMock()
    console._live_enabled = True
    console._live_running = False  # paused — resume should start it

    console.resume_after_input()

    console._live.start.assert_called_once()
    console._live.update.assert_called_once()


def test_harness_console_pause_is_noop_when_not_started(monkeypatch, tmp_path):
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    console = HarnessConsole(
        ContextTracker(model_id="m", model_limit=100), AgentManager(), tmp_path
    )
    console._live = MagicMock()
    console._live_enabled = False
    console._live_running = False

    console.pause_for_input()

    console._live.stop.assert_not_called()


def test_harness_console_resume_is_noop_when_not_started(monkeypatch, tmp_path):
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    console = HarnessConsole(
        ContextTracker(model_id="m", model_limit=100), AgentManager(), tmp_path
    )
    console._live = MagicMock()
    console._live_enabled = False
    console._live_running = False

    console.resume_after_input()

    console._live.start.assert_not_called()
    console._live.update.assert_not_called()
