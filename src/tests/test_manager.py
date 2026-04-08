# pyright: reportMissingParameterType=false

import asyncio
from datetime import datetime
from datetime import timezone

import pytest

from team_harness.agents.manager import AgentManager
from team_harness.agents.manager import AgentState


@pytest.mark.asyncio
async def test_manager_wait_and_poll(tmp_path):
    proc = await asyncio.create_subprocess_exec("sh", "-lc", "echo hello")
    state = AgentState(
        id="agent_1",
        agent_type="codex",
        prompt="p",
        cwd=str(tmp_path),
        proc=proc,
        spawn_time=datetime.now(timezone.utc),
        stdout_log=tmp_path / "out.log",
        stderr_log=tmp_path / "err.log",
    )
    manager = AgentManager()
    manager.register(state)
    exit_code = await manager.wait_one("agent_1")
    assert exit_code == 0
    assert manager.get("agent_1").finished_at is not None


@pytest.mark.asyncio
async def test_manager_kill_already_exited(tmp_path):
    proc = await asyncio.create_subprocess_exec("sh", "-lc", "exit 0")
    await proc.wait()
    state = AgentState(
        id="agent_1",
        agent_type="codex",
        prompt="p",
        cwd=str(tmp_path),
        proc=proc,
        spawn_time=datetime.now(timezone.utc),
        stdout_log=tmp_path / "out.log",
        stderr_log=tmp_path / "err.log",
    )
    manager = AgentManager()
    manager.register(state)
    manager.kill("agent_1")
