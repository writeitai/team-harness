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


def test_harness_console_resume_is_noop(monkeypatch, tmp_path):
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    console = HarnessConsole(
        ContextTracker(model_id="m", model_limit=100), AgentManager(), tmp_path
    )
    console._live = MagicMock()
    console._live_enabled = True
    console._live_running = False

    console.resume_after_input()

    console._live.start.assert_not_called()
    console._live.update.assert_not_called()


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


def test_harness_console_status_bar_prefixes_estimated_total(monkeypatch, tmp_path):
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    ctx = ContextTracker(model_id="m", model_limit=100)
    ctx.update({"prompt_tokens": 20, "completion_tokens": 10})
    ctx.set_estimated_total(
        [{"role": "system", "content": "sys"}, {"role": "user", "content": "a" * 400}]
    )
    console = HarnessConsole(ctx, AgentManager(), tmp_path)

    text = console._render_status_bar().plain

    assert "ctx: ~" in text


def test_ui_context_warning_mentions_clear(monkeypatch, tmp_path, capsys):
    plain = PlainConsole(
        ContextTracker(model_id="m", model_limit=100), AgentManager(), tmp_path
    )

    plain.context_warning()

    output = capsys.readouterr().out
    assert "/clear" in output
    assert "/reset" not in output

    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    rich = HarnessConsole(
        ContextTracker(model_id="m", model_limit=100), AgentManager(), tmp_path
    )
    rich._console = MagicMock()

    rich.context_warning()

    printed = "".join(call.args[0] for call in rich._console.print.call_args_list)
    assert "/clear" in printed
    assert "/reset" not in printed


def test_harness_console_shows_compacting_indicator(monkeypatch, tmp_path):
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    console = HarnessConsole(
        ContextTracker(model_id="m", model_limit=100), AgentManager(), tmp_path
    )

    console.begin_compaction()
    text = console._render_status_bar().plain

    assert "compacting..." in text
    console.end_compaction(100, 50)
    assert console._compacting is False


def test_harness_console_prints_post_compaction_notice(monkeypatch, tmp_path):
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    console = HarnessConsole(
        ContextTracker(model_id="m", model_limit=100), AgentManager(), tmp_path
    )
    console._console = MagicMock()
    console._live = MagicMock()
    console._live_running = True

    console.begin_compaction()
    console.end_compaction(12_345, 6_789)

    printed = " ".join(
        str(call.args[0]) for call in console._console.print.call_args_list
    )
    assert "Context compacted: ~12,345 -> ~6,789 tokens" in printed


def test_plain_console_prints_post_compaction_notice(tmp_path, capsys):
    console = PlainConsole(
        ContextTracker(model_id="m", model_limit=100), AgentManager(), tmp_path
    )

    console.begin_compaction()
    console.end_compaction(12_345, 6_789)

    output = capsys.readouterr().out
    assert "Compacting conversation..." in output
    assert "Context compacted: ~12,345 -> ~6,789 tokens" in output
