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

    # The built-in codex default includes default_model="gpt-5.4" so the
    # --model flag is injected even without an explicit override.
    assert command == [
        "codex",
        "exec",
        "--dangerously-bypass-approvals-and-sandbox",
        "--skip-git-repo-check",
        "--json",
        "--model",
        "gpt-5.4",
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
        "--model",
        "gpt-5.4",
        "sid-123",
        "continue",
    ]


def test_build_command_codex_explicit_model_overrides_default():
    command = build_command(
        agent_type="codex", prompt="do thing", config=Config(), model="gpt-5.4-mini"
    )

    # Explicit model wins over template.default_model.
    assert "--model" in command
    assert "gpt-5.4-mini" in command
    assert "gpt-5.4" not in command


def test_build_command_codex_default_model_cleared_by_user_override():
    from team_harness.agents.template import AgentTemplate
    from team_harness.agents.template import DEFAULT_AGENT_TEMPLATES

    # A user override that explicitly sets default_model=None should
    # restore the "no --model flag at all" behavior.
    base = DEFAULT_AGENT_TEMPLATES["codex"]
    override = AgentTemplate(
        command=base.command,
        shared_flags=base.shared_flags,
        resume_prefix=base.resume_prefix,
        resume_flags=base.resume_flags,
        model_flag=base.model_flag,
        default_model=None,
        reasoning_effort_flag=base.reasoning_effort_flag,
        session_capture=base.session_capture,
    )
    config = Config(agent_templates={"codex": override})

    command = build_command(agent_type="codex", prompt="do thing", config=config)

    assert "--model" not in command
    assert "gpt-5.4" not in command


# ---------------------------------------------------------------------------
# reasoning_effort injection
# ---------------------------------------------------------------------------


def test_build_command_codex_with_reasoning_effort_high():
    from team_harness.agents.template import AgentTemplate
    from team_harness.agents.template import DEFAULT_AGENT_TEMPLATES

    base = DEFAULT_AGENT_TEMPLATES["codex"]
    override = AgentTemplate(
        command=base.command,
        shared_flags=base.shared_flags,
        resume_prefix=base.resume_prefix,
        resume_flags=base.resume_flags,
        model_flag=base.model_flag,
        default_model=base.default_model,
        reasoning_effort="high",
        reasoning_effort_flag=base.reasoning_effort_flag,
        session_capture=base.session_capture,
    )
    config = Config(agent_templates={"codex": override})
    command = build_command(agent_type="codex", prompt="do thing", config=config)

    assert "-c" in command
    i = command.index("-c")
    assert command[i + 1] == "model_reasoning_effort=high"


def test_build_command_claude_with_reasoning_effort_high():
    from team_harness.agents.template import AgentTemplate
    from team_harness.agents.template import DEFAULT_AGENT_TEMPLATES

    base = DEFAULT_AGENT_TEMPLATES["claude"]
    override = AgentTemplate(
        command=base.command,
        shared_flags=base.shared_flags,
        resume_prefix=base.resume_prefix,
        resume_flags=base.resume_flags,
        model_flag=base.model_flag,
        model_env_vars=base.model_env_vars,
        reasoning_effort="high",
        reasoning_effort_flag=base.reasoning_effort_flag,
        session_capture=base.session_capture,
    )
    config = Config(agent_templates={"claude": override})
    command = build_command(agent_type="claude", prompt="review", config=config)

    assert "--effort" in command
    i = command.index("--effort")
    assert command[i + 1] == "high"


def test_build_command_codex_no_reasoning_effort_by_default():
    """The built-in codex default has reasoning_effort=None so even
    though reasoning_effort_flag is set, no tokens are injected."""

    command = build_command(agent_type="codex", prompt="do thing", config=Config())
    assert "model_reasoning_effort=high" not in command
    assert "-c" not in command


def test_build_command_reasoning_effort_equals_form_single_token():
    """A template that uses `--effort={effort}` as a single token (rather
    than `--effort {effort}` as two tokens) still gets the substitution."""

    from team_harness.agents.registry import build_command_from_template
    from team_harness.agents.template import AgentTemplate

    template = AgentTemplate(
        command=("myagent",),
        reasoning_effort="high",
        reasoning_effort_flag=("--effort={effort}",),
        model_flag=None,
    )
    command = build_command_from_template(template=template, prompt="go")
    assert "--effort=high" in command
    assert "--effort={effort}" not in command


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
