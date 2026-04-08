# pyright: reportMissingParameterType=false

import json

import pytest

from team_harness.agents.manager import AgentManager
from team_harness.tools import todo_tools
from team_harness.tracking.context import ContextTracker
from team_harness.ui.console import HarnessConsole


@pytest.mark.asyncio
async def test_todo_round_trip_and_console_panel(tmp_path):
    todo_tools.setup(run_dir=tmp_path)
    tasks = [{"id": "1", "description": "x", "status": "pending"}]
    assert await todo_tools.todo_write(tasks) == "Todo list updated (1 tasks)."
    assert json.loads(await todo_tools.todo_read()) == tasks

    console = HarnessConsole(
        ctx=ContextTracker(model_id="m", model_limit=100),
        manager=AgentManager(),
        run_dir=tmp_path,
    )
    layout = console._render_live()
    assert "todos" in {child.name for child in layout.children}


@pytest.mark.asyncio
async def test_todo_invalid_status(tmp_path):
    todo_tools.setup(run_dir=tmp_path)
    assert "ERROR:" in await todo_tools.todo_write(
        [{"id": "1", "description": "x", "status": "bad"}]
    )
