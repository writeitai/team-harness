# pyright: reportMissingParameterType=false, reportArgumentType=false

import asyncio
from datetime import datetime
from datetime import timezone
import json
from types import SimpleNamespace

import pytest

from team_harness.agents.manager import AgentManager
from team_harness.agents.manager import AgentState
from team_harness.harness import _extract_final_text
from team_harness.harness import AgentSummary
from team_harness.harness import Harness
from team_harness.harness import HarnessError
from team_harness.harness import HarnessResult
from team_harness.tools.agent_tools import build_agent_tool_bindings
from team_harness.tools.fs_tools import build_fs_tool_bindings
from team_harness.tools.todo_tools import build_todo_tool_bindings
from team_harness.tracking.context import ContextTracker
from team_harness.tracking.run_log import RunLogWriter
from team_harness.ui.console import HarnessConsole
from team_harness.ui.console import make_console
from team_harness.ui.console import PlainConsole
from team_harness.ui.console import SilentConsole


def _tool_map(bindings):
    return {schema["function"]["name"]: fn for schema, fn in bindings}


def test_extract_final_text_edge_cases():
    assert _extract_final_text([]) == ""
    assert (
        _extract_final_text(
            [
                {"role": "assistant", "content": "tool prelude", "tool_calls": []},
                {"role": "assistant", "content": ""},
                {"role": "assistant", "content": "final answer"},
            ]
        )
        == "final answer"
    )
    assert (
        _extract_final_text(
            [
                {"role": "assistant", "content": "tool only", "tool_calls": [{}]},
                {"role": "tool", "content": "ok"},
            ]
        )
        == ""
    )


def test_console_mode_selection(monkeypatch, tmp_path):
    ctx = ContextTracker(model_id="m", model_limit=100)
    manager = AgentManager()

    monkeypatch.setattr("sys.stdout.isatty", lambda: False)
    assert isinstance(make_console(ctx, manager, tmp_path), PlainConsole)
    assert isinstance(make_console(ctx, manager, tmp_path, mode="plain"), PlainConsole)
    assert isinstance(make_console(ctx, manager, tmp_path, mode="rich"), HarnessConsole)
    assert isinstance(make_console(mode="silent"), SilentConsole)

    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    assert isinstance(make_console(ctx, manager, tmp_path, mode="auto"), HarnessConsole)


@pytest.mark.asyncio
async def test_harness_run_returns_result_with_agents(monkeypatch, tmp_path):
    runs_dir = tmp_path / "runs"
    state = {}

    async def fake_resolve_model_limit(model, client, config):
        return 100

    async def fake_loop(messages, config, run_log, ui, registry, client, ctx):
        state["manager"].register(
            AgentState(
                id="agent_1",
                agent_type="codex",
                prompt="worker prompt",
                cwd=config.cwd,
                proc=SimpleNamespace(returncode=0),
                spawn_time=datetime.now(timezone.utc),
                stdout_log=config.run_dir / "agent_1_stdout.log",
                stderr_log=config.run_dir / "agent_1_stderr.log",
            )
        )
        messages.append({"role": "assistant", "content": "final text"})

    def fake_make_console(*, ctx, manager, run_dir, mode="auto"):
        state["manager"] = manager
        state["mode"] = mode
        return SilentConsole()

    class FakeClient:
        def __init__(self, api_base, api_key, model):
            self.api_base = api_base
            self.api_key = api_key
            self.model = model

    monkeypatch.setattr("team_harness.harness.RUNS_DIR", runs_dir)
    monkeypatch.setattr(
        "team_harness.harness.resolve_model_limit", fake_resolve_model_limit
    )
    monkeypatch.setattr("team_harness.harness.run", fake_loop)
    monkeypatch.setattr("team_harness.harness.make_console", fake_make_console)
    monkeypatch.setattr("team_harness.harness.load_skills", lambda cwd=None: [])
    monkeypatch.setattr("team_harness.harness.validate_templates", lambda *args: None)
    monkeypatch.setattr("team_harness.harness.CoordinatorClient", FakeClient)

    result = await Harness(
        api_base="http://localhost:11434/v1", api_key="test-key", cwd=str(tmp_path)
    ).run("do the task")

    assert isinstance(result, HarnessResult)
    assert result.text == "final text"
    assert result.run_id
    assert len(result.agents) == 1
    assert result.agents[0].id == "agent_1"
    assert result.agents[0].agent_type == "codex"
    assert state["mode"] == "silent"
    assert (runs_dir / result.run_id / "run.json").exists()


def test_agent_summary_fields():
    spawned_at = datetime.now(timezone.utc)
    finished_at = datetime.now(timezone.utc)
    summary = AgentSummary(
        id="agent_1",
        agent_type="codex",
        cwd="/tmp/work",
        status="done",
        exit_code=0,
        prompt="test prompt",
        spawned_at=spawned_at,
        finished_at=finished_at,
    )

    assert summary.id == "agent_1"
    assert summary.agent_type == "codex"
    assert summary.cwd == "/tmp/work"
    assert summary.status == "done"
    assert summary.exit_code == 0
    assert summary.prompt == "test prompt"
    assert summary.spawned_at == spawned_at
    assert summary.finished_at == finished_at


@pytest.mark.asyncio
async def test_harness_run_raises_harness_error_on_loop_failure(monkeypatch, tmp_path):
    runs_dir = tmp_path / "runs"

    async def fake_resolve_model_limit(model, client, config):
        return 100

    async def failing_loop(messages, config, run_log, ui, registry, client, ctx):
        raise RuntimeError("coordinator exploded")

    class FakeClient:
        def __init__(self, api_base, api_key, model):
            pass

    monkeypatch.setattr("team_harness.harness.RUNS_DIR", runs_dir)
    monkeypatch.setattr(
        "team_harness.harness.resolve_model_limit", fake_resolve_model_limit
    )
    monkeypatch.setattr("team_harness.harness.run", failing_loop)
    monkeypatch.setattr(
        "team_harness.harness.make_console", lambda **_: SilentConsole()
    )
    monkeypatch.setattr("team_harness.harness.load_skills", lambda cwd=None: [])
    monkeypatch.setattr("team_harness.harness.validate_templates", lambda *args: None)
    monkeypatch.setattr("team_harness.harness.CoordinatorClient", FakeClient)

    with pytest.raises(HarnessError, match="coordinator exploded"):
        await Harness(
            api_base="http://localhost:11434/v1", api_key="test-key", cwd=str(tmp_path)
        ).run("do the task")

    # Verify run log was still finalized
    run_dirs = list(runs_dir.iterdir())
    assert len(run_dirs) == 1
    run_json = run_dirs[0] / "run.json"
    assert run_json.exists()
    data = json.loads(run_json.read_text())
    assert "error" in data


@pytest.mark.asyncio
async def test_harness_error_importable_from_package():
    from team_harness import HarnessError as HE

    assert HE is HarnessError


@pytest.mark.asyncio
async def test_build_agent_tool_bindings_produce_working_closures(
    monkeypatch, tmp_path, config, manager, ui
):
    config.run_dir = tmp_path
    config.agent_templates = {"codex": "echo {prompt}"}
    run_log = RunLogWriter("run_1", tmp_path, config.model, config.api_base)

    async def fake_spawn(*args, **kwargs):
        return await asyncio.create_subprocess_exec("sh", "-lc", "exit 0")

    monkeypatch.setattr("team_harness.tools.agent_tools.spawner.spawn", fake_spawn)
    monkeypatch.setattr(
        "team_harness.tools.agent_tools.spawner.build_command",
        lambda *args, **kwargs: ["fake-agent"],
    )

    tools = _tool_map(
        build_agent_tool_bindings(manager, run_log, config, ui, ["codex", "harness"])
    )
    agent_id = await tools["spawn_agent"](
        type="codex", prompt="hello", cwd=str(tmp_path)
    )
    await asyncio.sleep(0.05)

    payload = json.loads(await tools["list_agents"]())
    assert payload[0]["id"] == agent_id
    assert payload[0]["type"] == "codex"
    assert ("spawned", agent_id) in ui.agent_events


@pytest.mark.asyncio
async def test_build_fs_tool_bindings_isolate_cursor_state(tmp_path):
    path = tmp_path / "progress.log"
    path.write_text("one\n")

    tools_a = _tool_map(build_fs_tool_bindings())
    tools_b = _tool_map(build_fs_tool_bindings())

    assert await tools_a["read_new_file_content"](str(path)) == "one\n"
    assert await tools_b["read_new_file_content"](str(path)) == "one\n"

    path.write_text("one\ntwo\n")
    assert await tools_a["read_new_file_content"](str(path)) == "two\n"
    assert await tools_b["read_new_file_content"](str(path)) == "two\n"


@pytest.mark.asyncio
async def test_build_todo_tool_bindings_isolate_todo_path(tmp_path):
    run_a = tmp_path / "run_a"
    run_b = tmp_path / "run_b"
    tools_a = _tool_map(build_todo_tool_bindings(run_a))
    tools_b = _tool_map(build_todo_tool_bindings(run_b))

    tasks_a = [{"id": "1", "description": "a", "status": "pending"}]
    tasks_b = [{"id": "2", "description": "b", "status": "done"}]

    assert await tools_a["todo_write"](tasks_a) == "Todo list updated (1 tasks)."
    assert await tools_b["todo_write"](tasks_b) == "Todo list updated (1 tasks)."
    assert json.loads(await tools_a["todo_read"]()) == tasks_a
    assert json.loads(await tools_b["todo_read"]()) == tasks_b
