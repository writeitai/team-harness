# pyright: reportMissingParameterType=false

import asyncio

import pytest

from team_harness.agents.spawner import spawn
from team_harness.agents.template import AgentTemplate
from team_harness.config import Config


def _write_capture_script(path):
    path.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$@\" > \"$CAPTURE_FILE\"\n",
        encoding="utf-8",
    )
    path.chmod(0o755)


@pytest.mark.asyncio
async def test_spawn_resume_passes_resume_session_id(tmp_path):
    capture_file = tmp_path / "args.txt"
    fake_codex = tmp_path / "fake-codex"
    _write_capture_script(fake_codex)
    config = Config(
        agent_templates={
            "codex": AgentTemplate(
                command=(str(fake_codex), "exec"),
                shared_flags=("--json",),
                resume_prefix=("resume",),
                resume_flags=("{session_id}",),
                model_flag=None,
            )
        }
    )

    result = await spawn(
        agent_id="agent_resume",
        agent_type="codex",
        prompt="continue here",
        cwd=tmp_path,
        config=config,
        log_dir=tmp_path / "logs",
        extra_env={"CAPTURE_FILE": str(capture_file)},
        mode="resume",
        resume_session_id="abc-123",
    )
    await asyncio.wait_for(result.proc.wait(), 2)

    assert capture_file.read_text(encoding="utf-8").splitlines() == [
        "exec",
        "resume",
        "--json",
        "abc-123",
        "continue here",
    ]


@pytest.mark.asyncio
async def test_spawn_fresh_passes_prompt_and_model_override(tmp_path):
    capture_file = tmp_path / "args.txt"
    fake_tool = tmp_path / "fake-tool"
    _write_capture_script(fake_tool)
    config = Config(
        agent_templates={
            "codex": AgentTemplate(
                command=(str(fake_tool),),
                shared_flags=("--json",),
            )
        }
    )

    result = await spawn(
        agent_id="agent_fresh",
        agent_type="codex",
        prompt="hello world",
        cwd=tmp_path,
        config=config,
        log_dir=tmp_path / "logs",
        extra_env={"CAPTURE_FILE": str(capture_file)},
        model="gpt-5.4",
    )
    await asyncio.wait_for(result.proc.wait(), 2)

    assert capture_file.read_text(encoding="utf-8").splitlines() == [
        "--json",
        "--model",
        "gpt-5.4",
        "hello world",
    ]


@pytest.mark.asyncio
async def test_spawn_replaces_generated_uuid_placeholder(tmp_path):
    capture_file = tmp_path / "args.txt"
    fake_tool = tmp_path / "fake-tool"
    _write_capture_script(fake_tool)
    config = Config(
        agent_templates={
            "claude": AgentTemplate(
                command=(str(fake_tool),),
                shared_flags=("--session-id", "{generated_uuid}"),
                model_flag=None,
            )
        }
    )

    result = await spawn(
        agent_id="agent_uuid",
        agent_type="claude",
        prompt="hello world",
        cwd=tmp_path,
        config=config,
        log_dir=tmp_path / "logs",
        extra_env={"CAPTURE_FILE": str(capture_file)},
    )
    await asyncio.wait_for(result.proc.wait(), 2)

    args = capture_file.read_text(encoding="utf-8").splitlines()
    assert args[0:2] == ["--session-id", result.generated_uuid]
    assert args[-1] == "hello world"
    assert result.generated_uuid is not None
