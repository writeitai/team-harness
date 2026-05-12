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
    """Write a small shell script that dumps its ANTHROPIC_*/CODEX_*/
    LLM_*/OPENROUTER_*/PROVIDER_* env vars to the file named by $ENV_DUMP.
    Used to assert what env the spawner actually passes to the child
    process."""

    path.write_text(
        "#!/bin/sh\n"
        'printenv | grep -E "^(ANTHROPIC_|CODEX_|LLM_|OPENROUTER_|PROVIDER_)" '
        '> "$ENV_DUMP" || true\n'
        "exit 0\n"
    )
    path.chmod(0o755)


def _parse_env_dump(env_dump_path) -> dict[str, str]:
    return {
        line.split("=", 1)[0]: line.split("=", 1)[1]
        for line in env_dump_path.read_text(encoding="utf-8").splitlines()
        if "=" in line
    }


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
                default_model="gpt-5.5",
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
    assert "gpt-5.5" in argv
    assert argv[argv.index("--model") + 1] == "gpt-5.5"


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
                default_model="gpt-5.5",
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
        model="gpt-5.5-mini",
    )
    await asyncio.wait_for(result.proc.wait(), 2)

    argv = argv_dump.read_text(encoding="utf-8").splitlines()
    assert "gpt-5.5-mini" in argv
    assert "gpt-5.5" not in argv


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

    dumped = _parse_env_dump(env_dump)
    assert dumped.get("ANTHROPIC_MODEL") == "caller-wins-model"


# ---------------------------------------------------------------------------
# provider_env — OpenRouter-style env var presets with {env:VAR} expansion
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_spawn_provider_env_expands_env_ref_when_set(tmp_path, monkeypatch):
    from team_harness.agents.template import _clear_provider_env_warnings

    _clear_provider_env_warnings()
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-123")

    fake_claude = tmp_path / "fake-claude"
    _write_env_dump_script(fake_claude)
    env_dump = tmp_path / "env.txt"

    config = Config(
        agent_templates={
            "claude": AgentTemplate(
                command=(str(fake_claude),),
                model_flag=None,
                provider_env=(
                    ("ANTHROPIC_BASE_URL", "https://openrouter.ai/api"),
                    ("ANTHROPIC_AUTH_TOKEN", "{env:OPENROUTER_API_KEY}"),
                    ("ANTHROPIC_API_KEY", ""),
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

    dumped = _parse_env_dump(env_dump)
    assert dumped.get("ANTHROPIC_BASE_URL") == "https://openrouter.ai/api"
    assert dumped.get("ANTHROPIC_AUTH_TOKEN") == "sk-or-test-123"
    assert dumped.get("ANTHROPIC_API_KEY") == ""


@pytest.mark.asyncio
async def test_spawn_provider_env_expands_env_ref_when_missing(tmp_path, monkeypatch):
    from team_harness.agents.template import _clear_provider_env_warnings

    _clear_provider_env_warnings()
    monkeypatch.delenv("MY_MISSING_SECRET", raising=False)

    fake_claude = tmp_path / "fake-claude"
    _write_env_dump_script(fake_claude)
    env_dump = tmp_path / "env.txt"

    config = Config(
        agent_templates={
            "claude": AgentTemplate(
                command=(str(fake_claude),),
                model_flag=None,
                provider_env=(("PROVIDER_SECRET", "{env:MY_MISSING_SECRET}"),),
            )
        }
    )
    with pytest.warns(UserWarning, match="MY_MISSING_SECRET"):
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

    dumped = _parse_env_dump(env_dump)
    assert dumped.get("PROVIDER_SECRET") == ""


@pytest.mark.asyncio
async def test_spawn_provider_env_literal_passthrough(tmp_path):
    """Values without any `{env:…}` placeholder are passed through
    verbatim, including the empty-string blanking pattern used for
    `ANTHROPIC_API_KEY`."""

    fake_claude = tmp_path / "fake-claude"
    _write_env_dump_script(fake_claude)
    env_dump = tmp_path / "env.txt"

    config = Config(
        agent_templates={
            "claude": AgentTemplate(
                command=(str(fake_claude),),
                model_flag=None,
                provider_env=(
                    ("ANTHROPIC_BASE_URL", "https://openrouter.ai/api"),
                    ("ANTHROPIC_API_KEY", ""),
                ),
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

    dumped = _parse_env_dump(env_dump)
    assert dumped["ANTHROPIC_BASE_URL"] == "https://openrouter.ai/api"
    assert dumped["ANTHROPIC_API_KEY"] == ""


@pytest.mark.asyncio
async def test_spawn_provider_env_merge_order_model_env_wins(tmp_path, monkeypatch):
    """When provider_env and model_env_vars both declare the same name,
    model_env_vars (later in merge order) wins."""

    from team_harness.agents.template import _clear_provider_env_warnings

    _clear_provider_env_warnings()
    monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)

    fake_claude = tmp_path / "fake-claude"
    _write_env_dump_script(fake_claude)
    env_dump = tmp_path / "env.txt"

    config = Config(
        agent_templates={
            "claude": AgentTemplate(
                command=(str(fake_claude),),
                model_flag=None,
                default_model="dynamic-model",
                model_env_vars=("ANTHROPIC_MODEL",),
                provider_env=(("ANTHROPIC_MODEL", "literal-provider-value"),),
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

    dumped = _parse_env_dump(env_dump)
    # model_env_vars layer (later in merge order) wins.
    assert dumped["ANTHROPIC_MODEL"] == "dynamic-model"


@pytest.mark.asyncio
async def test_spawn_provider_env_caller_extra_env_wins(tmp_path, monkeypatch):
    from team_harness.agents.template import _clear_provider_env_warnings

    _clear_provider_env_warnings()
    monkeypatch.setenv("OPENROUTER_API_KEY", "from-shell")

    fake_claude = tmp_path / "fake-claude"
    _write_env_dump_script(fake_claude)
    env_dump = tmp_path / "env.txt"

    config = Config(
        agent_templates={
            "claude": AgentTemplate(
                command=(str(fake_claude),),
                model_flag=None,
                provider_env=(("ANTHROPIC_AUTH_TOKEN", "{env:OPENROUTER_API_KEY}"),),
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
        extra_env={"ENV_DUMP": str(env_dump), "ANTHROPIC_AUTH_TOKEN": "caller-wins"},
    )
    await asyncio.wait_for(result.proc.wait(), 2)

    dumped = _parse_env_dump(env_dump)
    assert dumped["ANTHROPIC_AUTH_TOKEN"] == "caller-wins"


# ---------------------------------------------------------------------------
# OpenHands — env-only model injection (no --model flag)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_spawn_openhands_sets_llm_model_env_without_model_flag(
    tmp_path, monkeypatch
):
    """OpenHands has no --model flag; model override must land in
    LLM_MODEL env, and the argv must be exactly the template shape."""

    monkeypatch.delenv("LLM_MODEL", raising=False)

    fake_openhands = tmp_path / "fake-openhands"
    fake_openhands.write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "$@" > "$ARGV_DUMP"\n'
        'printenv | grep -E "^(ANTHROPIC_|CODEX_|LLM_)" > "$ENV_DUMP" || true\n'
        "exit 0\n"
    )
    fake_openhands.chmod(0o755)
    argv_dump = tmp_path / "argv.txt"
    env_dump = tmp_path / "env.txt"

    config = Config(
        agent_templates={
            "openhands": AgentTemplate(
                command=(str(fake_openhands),),
                shared_flags=("--headless", "--json", "--override-with-envs"),
                prompt_flag="-t",
                prompt_position="tail",
                model_flag=None,
                model_env_vars=("LLM_MODEL",),
            )
        }
    )
    result = await spawn(
        agent_id="agent_openhands",
        agent_type="openhands",
        prompt="hello",
        cwd=tmp_path,
        config=config,
        log_dir=tmp_path / "logs",
        extra_env={"ARGV_DUMP": str(argv_dump), "ENV_DUMP": str(env_dump)},
        model="anthropic/claude-sonnet-4-6",
    )
    await asyncio.wait_for(result.proc.wait(), 2)

    argv = argv_dump.read_text(encoding="utf-8").splitlines()
    assert argv == ["--headless", "--json", "--override-with-envs", "-t", "hello"]
    assert "--model" not in argv

    dumped = _parse_env_dump(env_dump)
    assert dumped.get("LLM_MODEL") == "anthropic/claude-sonnet-4-6"
