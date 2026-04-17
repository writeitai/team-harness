# pyright: reportMissingParameterType=false

from datetime import datetime
from datetime import timezone
from unittest.mock import MagicMock

from team_harness.agents.manager import AgentManager
from team_harness.agents.manager import AgentState
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


def test_plain_console_end_compaction_suppresses_line_on_failure(tmp_path, capsys):
    console = PlainConsole(
        ContextTracker(model_id="m", model_limit=100), AgentManager(), tmp_path
    )

    console.begin_compaction()
    console.end_compaction(12_345, 12_345, success=False)

    output = capsys.readouterr().out
    assert "Compacting conversation..." in output
    assert "Context compacted:" not in output


def test_harness_console_end_compaction_clears_compacting_flag_on_failure(
    monkeypatch, tmp_path
):
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    console = HarnessConsole(
        ContextTracker(model_id="m", model_limit=100), AgentManager(), tmp_path
    )
    console._console = MagicMock()
    console._live = MagicMock()
    console._live_running = True

    console.begin_compaction()
    console.end_compaction(12_345, 12_345, success=False)

    assert console._compacting is False
    console._live.update.assert_called()
    printed = " ".join(
        str(call.args[0]) for call in console._console.print.call_args_list
    )
    assert "Context compacted: " not in printed


def test_harness_console_phase_transitions(monkeypatch, tmp_path):
    """Spinner phase transitions through the turn lifecycle."""
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    console = HarnessConsole(
        ContextTracker(model_id="m", model_limit=100), AgentManager(), tmp_path
    )
    console._live = MagicMock()
    assert console._phase == "idle"

    console.begin_turn(1)
    assert console._phase == "thinking"

    console.begin_streaming()
    assert console._phase == "streaming"

    console.end_streaming()
    assert console._phase == "tools"

    console.end_turn()
    assert console._phase == "idle"


def test_harness_console_status_bar_shows_thinking(monkeypatch, tmp_path):
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    console = HarnessConsole(
        ContextTracker(model_id="m", model_limit=100), AgentManager(), tmp_path
    )
    console._phase = "thinking"
    text = console._render_status_bar().plain
    assert "Thinking" in text


def test_harness_console_status_bar_no_thinking_when_idle(monkeypatch, tmp_path):
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    console = HarnessConsole(
        ContextTracker(model_id="m", model_limit=100), AgentManager(), tmp_path
    )
    console._phase = "idle"
    text = console._render_status_bar().plain
    assert "Thinking" not in text


def test_harness_console_print_user_prompt(monkeypatch, tmp_path):
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    from io import StringIO

    from rich.console import Console as RichConsole

    buf = StringIO()
    console = HarnessConsole(
        ContextTracker(model_id="m", model_limit=100), AgentManager(), tmp_path
    )
    console._console = RichConsole(file=buf, force_terminal=True, width=80)
    console.print_user_prompt("Hello world")
    output = buf.getvalue()
    assert "Hello world" in output


def test_plain_console_print_user_prompt(tmp_path, capsys):
    console = PlainConsole(
        ContextTracker(model_id="m", model_limit=100), AgentManager(), tmp_path
    )
    console.print_user_prompt("Hello world")
    output = capsys.readouterr().out
    assert "> Hello world" in output


def test_agent_emojis_in_panel(monkeypatch, tmp_path):
    from io import StringIO

    from rich.console import Console as RichConsole

    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    manager = AgentManager()
    console = HarnessConsole(
        ContextTracker(model_id="m", model_limit=100), manager, tmp_path
    )
    buf = StringIO()
    console._console = RichConsole(file=buf, force_terminal=True, width=120)
    # Create a mock agent state
    proc = MagicMock()
    proc.returncode = 0
    state = AgentState(
        id="test123456",
        agent_type="codex",
        prompt="test",
        cwd="/tmp",
        proc=proc,
        spawn_time=datetime.now(timezone.utc),
        stdout_log=tmp_path / "stdout.log",
        stderr_log=tmp_path / "stderr.log",
        exit_code=0,
        status="done",
    )
    manager.register(state)
    console.print_agent_panel_inline()
    output = buf.getvalue()
    assert "\U0001f537" in output


def test_style_paths_absolute():
    from team_harness.ui.console import _style_paths

    result = _style_paths("/usr/local/bin/python")
    assert result.plain == "/usr/local/bin/python"
    # Check that some styling was applied (spans should be non-empty)
    assert len(result._spans) > 0


def test_style_paths_relative():
    from team_harness.ui.console import _style_paths

    result = _style_paths("See ./src/main.py for details")
    assert "src/main.py" in result.plain
    assert len(result._spans) > 0


def test_style_paths_repo_relative():
    from team_harness.ui.console import _style_paths

    result = _style_paths("Changed src/team_harness/ui/console.py")
    assert len(result._spans) > 0


def test_style_paths_no_false_positive_url():
    from team_harness.ui.console import _style_paths

    result = _style_paths("Visit https://example.com/foo/bar")
    # No part of the URL should be highlighted
    assert len(result._spans) == 0


def test_style_paths_no_match_plain_text():
    from team_harness.ui.console import _style_paths

    result = _style_paths("Hello world, no paths here")
    assert len(result._spans) == 0


def test_style_paths_trailing_period_excluded():
    from team_harness.ui.console import _style_paths

    result = _style_paths("Check /usr/bin/python.")
    spans = result._spans
    for span in spans:
        matched = result.plain[span.start : span.end]
        assert not matched.endswith(".")


def test_style_paths_hyphenated_repo_path():
    from team_harness.ui.console import _style_paths

    result = _style_paths("team-harness/src/main.py")
    assert len(result._spans) > 0
    matched = result.plain[result._spans[0].start : result._spans[0].end]
    assert "team-harness" in matched


def test_agent_emoji_unknown_type_falls_back():
    from team_harness.ui.console import AGENT_EMOJIS

    assert AGENT_EMOJIS.get("unknown_type", "") == ""
