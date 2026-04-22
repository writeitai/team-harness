# pyright: reportMissingParameterType=false

import asyncio
from datetime import datetime
from datetime import timezone
import json

import pytest

from team_harness.agents.manager import AgentState
from team_harness.agents.template import AgentTemplate
from team_harness.cli import _graceful_shutdown
from team_harness.tools import agent_tools
from team_harness.tracking.models import AgentRecord
from team_harness.tracking.run_log import RunLogWriter
from tests.helpers import fake_agent_template


@pytest.mark.asyncio
async def test_spawn_agent_appends_suffix_before_output_instruction(
    tmp_path, config, manager, ui
):
    config.run_dir = tmp_path
    config.agent_templates = {"codex": fake_agent_template()}
    config.worker_suffix = "Always include a brief verification note."
    run_log = RunLogWriter(
        run_id="run_1",
        run_dir=tmp_path,
        provider=config.provider,
        model=config.model,
        api_base=config.api_base,
    )
    agent_tools.setup(manager=manager, run_log=run_log, config=config, ui=ui)
    agent_id = await agent_tools.spawn_agent(
        type="codex", prompt="hello", cwd=str(tmp_path)
    )
    await asyncio.sleep(0.1)
    state = manager.get(agent_id)
    data = json.loads((tmp_path / "run.json").read_text())
    full_prompt = data["agents"][0]["full_prompt"]
    assert "hello" in full_prompt
    prompt_index = full_prompt.index("hello")
    suffix_index = full_prompt.index(config.worker_suffix)
    footer = agent_tools._build_worker_output_footer("", config)
    output_index = full_prompt.index(footer)
    assert prompt_index < suffix_index < output_index
    assert full_prompt.endswith(footer)
    assert state.agent_type == "codex"


@pytest.mark.asyncio
async def test_read_new_output_waits_and_kills(tmp_path, config, manager, ui):
    stdout = tmp_path / "agent_stdout.log"
    stderr = tmp_path / "agent_stderr.log"
    stdout.write_text("one\n")
    stderr.write_text("")
    proc = await asyncio.create_subprocess_exec("sleep", "5")
    state = AgentState(
        id="agent_1",
        agent_type="codex",
        prompt="p",
        cwd=str(tmp_path),
        proc=proc,
        spawn_time=datetime.now(timezone.utc),
        stdout_log=stdout,
        stderr_log=stderr,
    )
    manager.register(state)
    run_log = RunLogWriter(
        run_id="run_1",
        run_dir=tmp_path,
        provider=config.provider,
        model=config.model,
        api_base=config.api_base,
    )
    agent_tools.setup(manager=manager, run_log=run_log, config=config, ui=ui)
    assert await agent_tools.read_new_agent_output("agent_1") == "one\n"
    stdout.write_text("one\ntwo\n")
    pieces = await asyncio.gather(
        agent_tools.read_new_agent_output("agent_1"),
        agent_tools.read_new_agent_output("agent_1"),
    )
    assert "".join(pieces) == "two\n"
    timeout_result = json.loads(
        await agent_tools.wait_for_any(["agent_1"], timeout=0.1)
    )
    assert timeout_result["timed_out"] is True
    assert json.loads(await agent_tools.wait_for_agents([], timeout=0.1)) == {
        "agents": {},
        "timed_out": False,
    }
    assert json.loads(await agent_tools.wait_for_any([], timeout=0.1)) == {
        "agent_id": None,
        "timed_out": False,
        "running": [],
    }
    result = json.loads(await agent_tools.kill_agent("agent_1"))
    assert result["killed"] is True
    assert result["agent_id"] == "agent_1"
    await asyncio.sleep(0.1)
    assert not any(
        event == "done" and agent_id == "agent_1" for event, agent_id in ui.agent_events
    )


@pytest.mark.asyncio
async def test_list_agents_and_graceful_shutdown(tmp_path, config, manager, ui):
    stdout = tmp_path / "agent_stdout.log"
    stderr = tmp_path / "agent_stderr.log"
    proc = await asyncio.create_subprocess_exec("sh", "-lc", "echo hello")
    state = AgentState(
        id="agent_1",
        agent_type="codex",
        prompt="p",
        cwd=str(tmp_path),
        proc=proc,
        spawn_time=datetime.now(timezone.utc),
        stdout_log=stdout,
        stderr_log=stderr,
    )
    manager.register(state)
    run_log = RunLogWriter(
        run_id="run_1",
        run_dir=tmp_path,
        provider=config.provider,
        model=config.model,
        api_base=config.api_base,
    )
    agent_tools.setup(manager=manager, run_log=run_log, config=config, ui=ui)
    await asyncio.sleep(0.1)
    payload = json.loads(await agent_tools.list_agents())
    assert payload[0]["status"] == "done (exit 0)"

    proc2 = await asyncio.create_subprocess_exec("sleep", "5")
    state2 = AgentState(
        id="agent_2",
        agent_type="codex",
        prompt="p",
        cwd=str(tmp_path),
        proc=proc2,
        spawn_time=datetime.now(timezone.utc),
        stdout_log=tmp_path / "agent2_stdout.log",
        stderr_log=tmp_path / "agent2_stderr.log",
    )
    manager.register(state2)
    run_log.record_agent_spawn(
        AgentRecord(
            id="agent_2",
            agent_type="codex",
            cwd=str(tmp_path),
            prompt="p",
            full_prompt="p",
            command=["sleep", "5"],
            spawned_at=state2.spawn_time,
            stdout_log=str(state2.stdout_log),
            stderr_log=str(state2.stderr_log),
        )
    )
    await _graceful_shutdown(manager=manager, run_log=run_log, ui=ui, timeout=0.01)
    data = json.loads((tmp_path / "run.json").read_text())
    statuses = {agent["id"]: agent["status"] for agent in data["agents"]}
    assert statuses["agent_2"] == "killed"


@pytest.mark.asyncio
async def test_list_agents_shows_killed_status(tmp_path, config, manager, ui):
    config.run_dir = tmp_path
    config.agent_templates = {
        "codex": AgentTemplate(command=("sh", "-lc", "sleep 5"), model_flag=None)
    }
    run_log = RunLogWriter(
        run_id="run_1",
        run_dir=tmp_path,
        provider=config.provider,
        model=config.model,
        api_base=config.api_base,
    )
    agent_tools.setup(manager=manager, run_log=run_log, config=config, ui=ui)

    agent_id = await agent_tools.spawn_agent(
        type="codex", prompt="hello", cwd=str(tmp_path)
    )
    result = json.loads(await agent_tools.kill_agent(agent_id))
    payload = json.loads(await agent_tools.list_agents())

    assert result["killed"] is True
    assert result["agent_id"] == agent_id
    assert len(payload) == 1
    assert payload[0]["id"] == agent_id
    assert payload[0]["type"] == "codex"
    assert payload[0]["status"] == "killed"
    assert payload[0]["cwd"] == str(tmp_path)


@pytest.mark.asyncio
async def test_spawn_agent_harness_depth_guard_blocks_spawn(
    tmp_path, config, manager, ui, monkeypatch
):
    config.run_dir = tmp_path
    config.max_depth = 2
    run_log = RunLogWriter(
        run_id="run_1",
        run_dir=tmp_path,
        provider=config.provider,
        model=config.model,
        api_base=config.api_base,
    )
    agent_tools.setup(manager=manager, run_log=run_log, config=config, ui=ui)
    monkeypatch.setenv("TEAM_HARNESS_DEPTH", "2")

    result = await agent_tools.spawn_agent(
        type="harness", prompt="hello", cwd=str(tmp_path)
    )

    assert result == "ERROR: max harness depth (2) reached"
    assert manager.list_all() == []
    data = json.loads((tmp_path / "run.json").read_text())
    assert data["agents"] == []


@pytest.mark.asyncio
async def test_kill_agent_updates_manager_and_run_log(tmp_path, config, manager, ui):
    stdout = tmp_path / "agent_stdout.log"
    stderr = tmp_path / "agent_stderr.log"
    proc = await asyncio.create_subprocess_exec("sleep", "5")
    state = AgentState(
        id="agent_1",
        agent_type="codex",
        prompt="p",
        cwd=str(tmp_path),
        proc=proc,
        spawn_time=datetime.now(timezone.utc),
        stdout_log=stdout,
        stderr_log=stderr,
    )
    manager.register(state)
    run_log = RunLogWriter(
        "run_1", tmp_path, config.provider, config.model, config.api_base
    )
    run_log.record_agent_spawn(
        AgentRecord(
            id="agent_1",
            agent_type="codex",
            cwd=str(tmp_path),
            prompt="p",
            full_prompt="p",
            command=["sleep", "5"],
            spawned_at=state.spawn_time,
            stdout_log=str(stdout),
            stderr_log=str(stderr),
        )
    )
    agent_tools.setup(manager, run_log, config, ui)

    result = json.loads(await agent_tools.kill_agent("agent_1"))

    assert result["killed"] is True
    assert result["agent_id"] == "agent_1"
    assert state.status == "killed"
    assert state.finished_at is not None
    assert state.exit_code is not None

    data = json.loads((tmp_path / "run.json").read_text())
    assert data["agents"][0]["status"] == "killed"
    assert data["agents"][0]["exit_code"] == state.exit_code
    assert ("killed", "agent_1") in ui.agent_events


@pytest.mark.asyncio
async def test_wait_for_any_includes_failure_classification_on_api_error(
    tmp_path, config, manager, ui
):
    """When an agent fails with API error patterns in stderr, wait_for_any
    should include a failure_classification in the response."""
    stdout = tmp_path / "agent_stdout.log"
    stderr = tmp_path / "agent_stderr.log"
    stdout.write_text("")
    stderr.write_text("Error: API request failed with status: 429 rate limit exceeded")
    proc = await asyncio.create_subprocess_exec("sh", "-lc", "exit 1")
    state = AgentState(
        id="agent_1",
        agent_type="codex",
        prompt="do the thing",
        cwd=str(tmp_path),
        proc=proc,
        spawn_time=datetime.now(timezone.utc),
        stdout_log=stdout,
        stderr_log=stderr,
    )
    manager.register(state)
    run_log = RunLogWriter(
        "run_1", tmp_path, config.provider, config.model, config.api_base
    )
    agent_tools.setup(manager, run_log, config, ui)

    result = json.loads(await agent_tools.wait_for_any(["agent_1"], timeout=5))

    assert result["timed_out"] is False
    assert result["finished_agent_id"] == "agent_1"
    assert "failure_classification" in result
    fc = result["failure_classification"]
    assert fc["is_api_error"] is True
    assert fc["category"] == "rate_limit"
    assert "suggested_action" in fc


@pytest.mark.asyncio
async def test_wait_for_any_no_classification_on_normal_failure(
    tmp_path, config, manager, ui
):
    """When an agent fails without API error patterns, no failure_classification
    should be present."""
    stdout = tmp_path / "agent_stdout.log"
    stderr = tmp_path / "agent_stderr.log"
    stdout.write_text("")
    stderr.write_text("Traceback: IndexError: list index out of range")
    proc = await asyncio.create_subprocess_exec("sh", "-lc", "exit 1")
    state = AgentState(
        id="agent_1",
        agent_type="codex",
        prompt="do the thing",
        cwd=str(tmp_path),
        proc=proc,
        spawn_time=datetime.now(timezone.utc),
        stdout_log=stdout,
        stderr_log=stderr,
    )
    manager.register(state)
    run_log = RunLogWriter(
        "run_1", tmp_path, config.provider, config.model, config.api_base
    )
    agent_tools.setup(manager, run_log, config, ui)

    result = json.loads(await agent_tools.wait_for_any(["agent_1"], timeout=5))

    assert result["timed_out"] is False
    assert "failure_classification" not in result


@pytest.mark.asyncio
async def test_wait_for_any_no_classification_on_success(tmp_path, config, manager, ui):
    """Successful agents should never have a failure_classification."""
    stdout = tmp_path / "agent_stdout.log"
    stderr = tmp_path / "agent_stderr.log"
    stdout.write_text("done")
    stderr.write_text("")
    proc = await asyncio.create_subprocess_exec("sh", "-lc", "exit 0")
    state = AgentState(
        id="agent_1",
        agent_type="codex",
        prompt="do the thing",
        cwd=str(tmp_path),
        proc=proc,
        spawn_time=datetime.now(timezone.utc),
        stdout_log=stdout,
        stderr_log=stderr,
    )
    manager.register(state)
    run_log = RunLogWriter(
        "run_1", tmp_path, config.provider, config.model, config.api_base
    )
    agent_tools.setup(manager, run_log, config, ui)

    result = json.loads(await agent_tools.wait_for_any(["agent_1"], timeout=5))

    assert result["timed_out"] is False
    assert "failure_classification" not in result
