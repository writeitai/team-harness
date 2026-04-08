# pyright: reportMissingParameterType=false


import pytest

from team_harness.agents.registry import all_agent_types
from team_harness.agents.registry import build_command
from team_harness.agents.registry import check_harness_depth
from team_harness.agents.registry import get_allowed_types
from team_harness.agents.registry import validate_templates
from team_harness.config import Config


def test_build_command_respects_prompt_token():
    config = Config()
    command = build_command("claude", 'my prompt "with" spaces', config)
    assert command == [
        "claude",
        "-p",
        "--dangerously-skip-permissions",
        'my prompt "with" spaces',
    ]


def test_build_command_model_and_harness_allowlist():
    config = Config()
    command = build_command(
        "harness", "do thing", config, model="gpt-5", allowed_agents=["codex"]
    )
    assert command == [
        "team-harness",
        "run",
        "--model",
        "gpt-5",
        "do thing",
        "--agents",
        "codex",
    ]


def test_build_command_harness_without_allowlist():
    config = Config()
    command = build_command("harness", "do thing", config, model="gpt-5")
    assert command == ["team-harness", "run", "--model", "gpt-5", "do thing"]


def test_build_command_missing_placeholder_raises():
    config = Config(agent_templates={"codex": "codex exec"})
    with pytest.raises(ValueError):
        build_command("codex", "prompt", config)


def test_build_command_skips_duplicate_model(monkeypatch):
    config = Config(agent_templates={"codex": "codex exec --model existing {prompt}"})
    with pytest.warns(UserWarning):
        command = build_command("codex", "prompt", config, model="override")
    assert command == ["codex", "exec", "--model", "existing", "prompt"]


def test_allowed_types_and_depth_guard(monkeypatch):
    config = Config(agent_templates={"myagent": "myagent {prompt}"})
    assert "myagent" in all_agent_types(config)
    config.allowed_agents = ["codex", "myagent"]
    assert get_allowed_types(config) == ["codex", "myagent"]
    monkeypatch.setenv("HARNESS_DEPTH", "3")
    with pytest.raises(ValueError):
        check_harness_depth(Config(max_depth=3))


def test_validate_templates_warns(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: None)
    with pytest.warns(UserWarning):
        validate_templates(Config(), ["codex"])
