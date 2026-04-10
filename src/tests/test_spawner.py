# pyright: reportMissingParameterType=false

import asyncio

import pytest

from team_harness.agents.spawner import spawn
from team_harness.agents.template import AgentTemplate
from team_harness.config import Config
from tests.helpers import fake_agent_template


@pytest.mark.asyncio
async def test_spawn_creates_logs_and_uses_devnull(tmp_path):
    log_dir = tmp_path / "logs"
    config = Config(agent_templates={"codex": fake_agent_template()})

    proc = await spawn(
        agent_id="agent_test",
        agent_type="codex",
        prompt="hello",
        cwd=tmp_path,
        config=config,
        log_dir=log_dir,
    )
    await asyncio.wait_for(proc.proc.wait(), 2)

    assert proc.proc.stdin is None
    assert (log_dir / "agent_test_stdout.log").read_text().strip() == "hello"
    assert (log_dir / "agent_test_stderr.log").exists()


# ---------------------------------------------------------------------------
# default_model + model_env_vars injection
# ---------------------------------------------------------------------------


def _write_env_dump_script(path):
    """Write a small shell script that dumps its ANTHROPIC_*/CODEX_* env
    vars to the file named by $ENV_DUMP. Used to assert what env the
    spawner actually passes to the child process."""

    path.write_text(
        "#!/bin/sh\n"
        'printenv | grep -E "^(ANTHROPIC_|CODEX_)" > "$ENV_DUMP" || true\n'
        "exit 0\n"
    )
    path.chmod(0o755)


@pytest.mark.asyncio
async def test_spawn_default_model_injected_as_model_flag(tmp_path):
    """A template with `default_model` gets the flag appended automatically
    when the caller does not pass an explicit `model=`."""

    fake_codex = tmp_path / "fake-codex"
    fake_codex.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$ARGV_DUMP"\nexit 0\n')
    fake_codex.chmod(0o755)
    argv_dump = tmp_path / "argv.txt"

    config = Config(
        agent_templates={
            "codex": AgentTemplate(
                command=(str(fake_codex),),
                model_flag="--model",
                default_model="gpt-5.4",
            )
        }
    )
    result = await spawn(
        agent_id="agent_codex",
        agent_type="codex",
        prompt="hello",
        cwd=tmp_path,
        config=config,
        log_dir=tmp_path / "logs",
        extra_env={"ARGV_DUMP": str(argv_dump)},
    )
    await asyncio.wait_for(result.proc.wait(), 2)

    argv = argv_dump.read_text(encoding="utf-8").splitlines()
    assert "--model" in argv
    assert "gpt-5.4" in argv
    assert argv[argv.index("--model") + 1] == "gpt-5.4"


@pytest.mark.asyncio
async def test_spawn_explicit_model_overrides_default(tmp_path):
    fake_codex = tmp_path / "fake-codex"
    fake_codex.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$ARGV_DUMP"\nexit 0\n')
    fake_codex.chmod(0o755)
    argv_dump = tmp_path / "argv.txt"

    config = Config(
        agent_templates={
            "codex": AgentTemplate(
                command=(str(fake_codex),),
                model_flag="--model",
                default_model="gpt-5.4",
            )
        }
    )
    result = await spawn(
        agent_id="agent_codex",
        agent_type="codex",
        prompt="hello",
        cwd=tmp_path,
        config=config,
        log_dir=tmp_path / "logs",
        extra_env={"ARGV_DUMP": str(argv_dump)},
        model="gpt-5.4-mini",
    )
    await asyncio.wait_for(result.proc.wait(), 2)

    argv = argv_dump.read_text(encoding="utf-8").splitlines()
    assert "gpt-5.4-mini" in argv
    assert "gpt-5.4" not in argv


@pytest.mark.asyncio
async def test_spawn_claude_template_sets_three_env_vars(tmp_path, monkeypatch):
    """A claude template with default_model set must inject the three
    'main model' env vars into the child process. The two haiku-related
    vars must NOT be set by the template."""

    # Start with a clean slate so we can assert "not set".
    monkeypatch.delenv("ANTHROPIC_DEFAULT_HAIKU_MODEL", raising=False)
    monkeypatch.delenv("ANTHROPIC_SMALL_FAST_MODEL", raising=False)

    fake_claude = tmp_path / "fake-claude"
    _write_env_dump_script(fake_claude)
    env_dump = tmp_path / "env.txt"

    config = Config(
        agent_templates={
            "claude": AgentTemplate(
                command=(str(fake_claude),),
                model_flag="--model",
                default_model="claude-opus-4-6",
                model_env_vars=(
                    "ANTHROPIC_MODEL",
                    "ANTHROPIC_DEFAULT_SONNET_MODEL",
                    "ANTHROPIC_DEFAULT_OPUS_MODEL",
                ),
            )
        }
    )
    result = await spawn(
        agent_id="agent_claude",
        agent_type="claude",
        prompt="hello",
        cwd=tmp_path,
        config=config,
        log_dir=tmp_path / "logs",
        extra_env={"ENV_DUMP": str(env_dump)},
    )
    await asyncio.wait_for(result.proc.wait(), 2)

    dumped = {
        line.split("=", 1)[0]: line.split("=", 1)[1]
        for line in env_dump.read_text(encoding="utf-8").splitlines()
        if "=" in line
    }
    assert dumped.get("ANTHROPIC_MODEL") == "claude-opus-4-6"
    assert dumped.get("ANTHROPIC_DEFAULT_SONNET_MODEL") == "claude-opus-4-6"
    assert dumped.get("ANTHROPIC_DEFAULT_OPUS_MODEL") == "claude-opus-4-6"
    # The two haiku-related vars must NOT be set by the template.
    assert "ANTHROPIC_DEFAULT_HAIKU_MODEL" not in dumped
    assert "ANTHROPIC_SMALL_FAST_MODEL" not in dumped


@pytest.mark.asyncio
async def test_spawn_claude_inherits_parent_haiku_var_unchanged(tmp_path, monkeypatch):
    """If the parent environment already has a user-set haiku var, it
    must pass through to the child unchanged. We respect the user; we
    just don't actively override."""

    monkeypatch.setenv("ANTHROPIC_DEFAULT_HAIKU_MODEL", "user-haiku-model")

    fake_claude = tmp_path / "fake-claude"
    _write_env_dump_script(fake_claude)
    env_dump = tmp_path / "env.txt"

    config = Config(
        agent_templates={
            "claude": AgentTemplate(
                command=(str(fake_claude),),
                model_flag="--model",
                default_model="claude-sonnet-4-6",
                model_env_vars=("ANTHROPIC_MODEL",),
            )
        }
    )
    result = await spawn(
        agent_id="agent_claude",
        agent_type="claude",
        prompt="hi",
        cwd=tmp_path,
        config=config,
        log_dir=tmp_path / "logs",
        extra_env={"ENV_DUMP": str(env_dump)},
    )
    await asyncio.wait_for(result.proc.wait(), 2)

    dumped = {
        line.split("=", 1)[0]: line.split("=", 1)[1]
        for line in env_dump.read_text(encoding="utf-8").splitlines()
        if "=" in line
    }
    # Template sets this.
    assert dumped.get("ANTHROPIC_MODEL") == "claude-sonnet-4-6"
    # User's parent-env haiku var flows through untouched.
    assert dumped.get("ANTHROPIC_DEFAULT_HAIKU_MODEL") == "user-haiku-model"


@pytest.mark.asyncio
async def test_spawn_caller_extra_env_overrides_template_env(tmp_path, monkeypatch):
    """Merge order: os.environ < template_env < caller extra_env.
    A test/SDK user that passes `extra_env={...}` must win over the
    template's declared model env vars."""

    monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)

    fake_claude = tmp_path / "fake-claude"
    _write_env_dump_script(fake_claude)
    env_dump = tmp_path / "env.txt"

    config = Config(
        agent_templates={
            "claude": AgentTemplate(
                command=(str(fake_claude),),
                model_flag="--model",
                default_model="template-default-model",
                model_env_vars=("ANTHROPIC_MODEL",),
            )
        }
    )
    result = await spawn(
        agent_id="agent_claude",
        agent_type="claude",
        prompt="hi",
        cwd=tmp_path,
        config=config,
        log_dir=tmp_path / "logs",
        extra_env={"ENV_DUMP": str(env_dump), "ANTHROPIC_MODEL": "caller-wins-model"},
    )
    await asyncio.wait_for(result.proc.wait(), 2)

    dumped = {
        line.split("=", 1)[0]: line.split("=", 1)[1]
        for line in env_dump.read_text(encoding="utf-8").splitlines()
        if "=" in line
    }
    assert dumped.get("ANTHROPIC_MODEL") == "caller-wins-model"
