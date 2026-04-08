# pyright: reportMissingParameterType=false

import asyncio

import pytest

from team_harness.agents.spawner import spawn
from team_harness.config import Config


@pytest.mark.asyncio
async def test_spawn_creates_logs_and_uses_devnull(tmp_path):
    log_dir = tmp_path / "logs"
    config = Config(agent_templates={"codex": "echo {prompt}"})

    proc = await spawn(
        agent_id="agent_test",
        agent_type="codex",
        prompt="hello",
        cwd=tmp_path,
        config=config,
        log_dir=log_dir,
    )
    await asyncio.wait_for(proc.wait(), 2)

    assert proc.stdin is None
    assert (log_dir / "agent_test_stdout.log").read_text().strip() == "hello"
    assert (log_dir / "agent_test_stderr.log").exists()
