# pyright: reportMissingParameterType=false

import pytest

from team_harness.agents.registry import all_agent_types
from team_harness.agents.registry import build_command
from team_harness.agents.registry import check_harness_depth
from team_harness.agents.registry import get_allowed_types
from team_harness.agents.registry import validate_templates
from team_harness.config import Config


def test_build_command_codex_prompt_assignment_template():
    config = Config()

    command = build_command(
        agent_type="codex", prompt='Say "hello" && echo $HOME', config=config
    )

    assert command == [
        "codex",
        "exec",
        "--yolo",
        "--model",
        "gpt-5.4",
        'PROMPT=Say "hello" && echo $HOME',
    ]


def test_build_command_gemini_quoted_template():
    config = Config()

    command = build_command(
        agent_type="gemini", prompt='quoted "prompt"', config=config
    )

    assert command == ["gemini", "--approval-mode=yolo", "-p", 'quoted "prompt"']


def test_build_command_prompt_with_quotes_and_shell_metacharacters():
    config = Config()

    command = build_command(
        agent_type="codex",
        prompt='a "quote" && echo $HOME ; rm -rf * \\ still literal',
        config=config,
    )

    assert command[-1] == 'PROMPT=a "quote" && echo $HOME ; rm -rf * \\ still literal'
    assert len(command) == 6


def test_build_command_backward_compatible_bare_placeholder():
    config = Config()

    command = build_command(
        agent_type="claude", prompt='my prompt "with" spaces', config=config
    )

    assert command == [
        "claude",
        "-p",
        "--dangerously-skip-permissions",
        'my prompt "with" spaces',
    ]


def test_build_command_model_inserted_before_prompt_token_not_index_one():
    config = Config(
        agent_templates={"myagent": "myagent exec --json {prompt} --verbose"}
    )

    command = build_command(
        agent_type="myagent", prompt="do thing", config=config, model="gpt-5"
    )

    assert command == [
        "myagent",
        "exec",
        "--json",
        "--model",
        "gpt-5",
        "do thing",
        "--verbose",
    ]


def test_build_command_model_and_harness_allowlist():
    config = Config()

    command = build_command(
        agent_type="harness",
        prompt="do thing",
        config=config,
        model="gpt-5",
        allowed_agents=["codex"],
    )

    assert command == ["th", "run", "--model", "gpt-5", "do thing", "--agents", "codex"]


def test_build_command_harness_without_allowlist():
    config = Config()

    command = build_command(
        agent_type="harness", prompt="do thing", config=config, model="gpt-5"
    )

    assert command == ["th", "run", "--model", "gpt-5", "do thing"]


def test_build_command_missing_placeholder_raises():
    config = Config(agent_templates={"codex": "codex exec"})

    with pytest.raises(ValueError, match="missing \\{prompt\\} placeholder|Template"):
        build_command(agent_type="codex", prompt="prompt", config=config)


def test_build_command_duplicate_model_warning_uses_tokenized_template():
    config = Config(
        agent_templates={"codex": 'codex exec PROMPT="{prompt}" --model existing-model'}
    )

    with pytest.warns(UserWarning):
        command = build_command(
            agent_type="codex", prompt="prompt", config=config, model="override"
        )

    assert command == ["codex", "exec", "PROMPT=prompt", "--model", "existing-model"]


def test_allowed_types_and_depth_guard(monkeypatch):
    config = Config(agent_templates={"myagent": "myagent {prompt}"})

    assert "myagent" in all_agent_types(config)
    config.allowed_agents = ["codex", "myagent"]
    assert get_allowed_types(config) == ["codex", "myagent"]
    monkeypatch.setenv("HARNESS_DEPTH", "3")
    with pytest.raises(ValueError):
        check_harness_depth(Config(max_depth=3))


def test_validate_templates_handles_quoted_default_templates(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/env")

    validate_templates(config=Config(), allowed_types=["codex", "gemini"])
