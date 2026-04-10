# pyright: reportMissingParameterType=false

import pytest

from team_harness.agents.registry import all_agent_types
from team_harness.agents.registry import build_command
from team_harness.agents.registry import build_command_from_template
from team_harness.agents.registry import check_harness_depth
from team_harness.agents.registry import get_allowed_types
from team_harness.agents.registry import validate_templates
from team_harness.agents.template import AgentTemplate
from team_harness.agents.template import DEFAULT_AGENT_TEMPLATES
from team_harness.config import Config


def test_build_command_default_codex_fresh():
    command = build_command(agent_type="codex", prompt="do thing", config=Config())

    assert command == [
        "codex",
        "exec",
        "--dangerously-bypass-approvals-and-sandbox",
        "--skip-git-repo-check",
        "--json",
        "do thing",
    ]


def test_build_command_default_codex_resume():
    command = build_command(
        agent_type="codex",
        prompt="continue",
        config=Config(),
        mode="resume",
        resume_session_id="sid-123",
    )

    assert command == [
        "codex",
        "exec",
        "resume",
        "--dangerously-bypass-approvals-and-sandbox",
        "--skip-git-repo-check",
        "--json",
        "sid-123",
        "continue",
    ]


def test_build_command_default_gemini_resume_and_model_override():
    command = build_command(
        agent_type="gemini",
        prompt="continue",
        config=Config(),
        mode="resume",
        resume_session_id="sid-123",
        model="gemini-3",
    )

    assert command == [
        "gemini",
        "--approval-mode",
        "yolo",
        "--output-format",
        "stream-json",
        "--model",
        "gemini-3",
        "--resume",
        "sid-123",
        "-p",
        "continue",
    ]


def test_build_command_default_claude_contains_verbose():
    command = build_command(agent_type="claude", prompt="review", config=Config())

    assert command == [
        "claude",
        "-p",
        "--dangerously-skip-permissions",
        "--output-format",
        "stream-json",
        "--verbose",
        "review",
    ]


def test_build_command_model_override_noop_when_model_flag_is_none():
    command = build_command(
        agent_type="pi", prompt="do thing", config=Config(), model="ignored"
    )

    assert command == ["pi", "--print", "--no-session", "do thing"]


def test_build_command_harness_model_and_allowlist():
    command = build_command(
        agent_type="harness",
        prompt="do thing",
        config=Config(),
        model="gpt-5",
        allowed_agents=["codex"],
    )

    assert command == ["th", "run", "--model", "gpt-5", "do thing", "--agents", "codex"]


def test_build_command_from_template_after_command_prompt_position():
    template = AgentTemplate(
        command=("myagent", "exec"),
        shared_flags=("--json",),
        prompt_position="after_command",
        model_flag=None,
    )

    command = build_command_from_template(template=template, prompt="work item")

    assert command == ["myagent", "exec", "work item", "--json"]


def test_build_command_from_template_substitutes_generated_uuid_and_session_id():
    template = AgentTemplate(
        command=("tool",),
        shared_flags=("--session-id", "{generated_uuid}"),
        resume_flags=("--resume", "{session_id}"),
        model_flag=None,
    )

    command = build_command_from_template(
        template=template,
        prompt="go",
        mode="resume",
        session_id="sid-123",
        generated_uuid="gen-456",
    )

    assert command == ["tool", "--session-id", "gen-456", "--resume", "sid-123", "go"]


def test_build_command_missing_resume_session_id_raises():
    with pytest.raises(ValueError, match="session_id"):
        build_command(
            agent_type="codex", prompt="resume", config=Config(), mode="resume"
        )


def test_allowed_types_and_depth_guard(monkeypatch):
    config = Config(
        agent_templates={
            "myagent": AgentTemplate(command=("myagent",), model_flag=None)
        }
    )

    assert "myagent" in all_agent_types(config)
    config.allowed_agents = ["codex", "myagent"]
    assert get_allowed_types(config) == ["codex", "myagent"]
    monkeypatch.setenv("HARNESS_DEPTH", "3")
    with pytest.raises(ValueError):
        check_harness_depth(Config(max_depth=3))


def test_validate_templates_handles_structured_defaults(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/env")

    validate_templates(config=Config(), allowed_types=list(DEFAULT_AGENT_TEMPLATES))
