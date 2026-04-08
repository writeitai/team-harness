# pyright: reportMissingParameterType=false

from team_harness.agents.manager import AgentManager
from team_harness.tracking.context import ContextTracker
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
