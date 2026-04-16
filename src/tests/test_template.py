# pyright: reportMissingParameterType=false

import pytest

from team_harness.agents.template import AgentTemplate
from team_harness.agents.template import SessionCapture
from team_harness.config import _parse_agent_template
from team_harness.config import load_config


def test_parse_structured_template_fields():
    template = _parse_agent_template(
        "codex",
        {
            "command": ["codex", "exec"],
            "shared_flags": ["--json"],
            "resume_prefix": ["resume"],
            "resume_flags": ["{session_id}"],
            "prompt_position": "tail",
            "model_flag": "--model",
            "session_capture": {
                "strategy": "stream_json_event",
                "match": {"type": "thread.started"},
                "field_path": ["thread_id"],
            },
        },
    )

    # Missing fields (default_model, model_env_vars, reasoning_effort_flag)
    # are inherited from the built-in codex default.
    assert template == AgentTemplate(
        command=("codex", "exec"),
        shared_flags=("--json",),
        resume_prefix=("resume",),
        resume_flags=("{session_id}",),
        model_flag="--model",
        default_model="gpt-5.4",
        reasoning_effort_flag=("-c", "model_reasoning_effort={effort}"),
        session_capture=SessionCapture(
            strategy="stream_json_event",
            match={"type": "thread.started"},
            field_path=("thread_id",),
        ),
    )


def test_parse_default_model_override_as_string():
    template = _parse_agent_template(
        "codex", {"command": ["codex", "exec"], "default_model": "gpt-5.4-turbo"}
    )
    assert template.default_model == "gpt-5.4-turbo"


def test_parse_default_model_cleared_with_false():
    template = _parse_agent_template(
        "codex", {"command": ["codex", "exec"], "default_model": False}
    )
    assert template.default_model is None


def test_parse_default_model_cleared_with_empty_string():
    template = _parse_agent_template(
        "codex", {"command": ["codex", "exec"], "default_model": ""}
    )
    assert template.default_model is None


def test_parse_default_model_inherits_from_base_when_absent():
    template = _parse_agent_template("codex", {"command": ["codex", "exec"]})
    assert template.default_model == "gpt-5.4"


def test_parse_model_env_vars_override():
    template = _parse_agent_template(
        "claude",
        {"command": ["claude"], "model_env_vars": ["ANTHROPIC_MODEL", "CUSTOM_ENV"]},
    )
    assert template.model_env_vars == ("ANTHROPIC_MODEL", "CUSTOM_ENV")


def test_parse_model_env_vars_inherits_from_base_when_absent():
    template = _parse_agent_template("claude", {"command": ["claude"]})
    # Claude's built-in default sets the three main-model env vars and
    # deliberately omits the haiku ones.
    assert template.model_env_vars == (
        "ANTHROPIC_MODEL",
        "ANTHROPIC_DEFAULT_SONNET_MODEL",
        "ANTHROPIC_DEFAULT_OPUS_MODEL",
    )
    assert "ANTHROPIC_DEFAULT_HAIKU_MODEL" not in template.model_env_vars
    assert "ANTHROPIC_SMALL_FAST_MODEL" not in template.model_env_vars


def test_parse_default_model_wrong_type_raises():
    with pytest.raises(SystemExit, match="default_model must be"):
        _parse_agent_template("myagent", {"command": ["myagent"], "default_model": 42})


# ---------------------------------------------------------------------------
# reasoning_effort / reasoning_effort_flag / provider_env
# ---------------------------------------------------------------------------


def test_parse_reasoning_effort_set():
    template = _parse_agent_template(
        "codex", {"command": ["codex", "exec"], "reasoning_effort": "high"}
    )
    assert template.reasoning_effort == "high"


def test_parse_reasoning_effort_cleared_with_false():
    template = _parse_agent_template(
        "codex", {"command": ["codex", "exec"], "reasoning_effort": False}
    )
    assert template.reasoning_effort is None


def test_parse_reasoning_effort_cleared_with_empty_string():
    template = _parse_agent_template(
        "codex", {"command": ["codex", "exec"], "reasoning_effort": ""}
    )
    assert template.reasoning_effort is None


def test_parse_reasoning_effort_inherits_from_base_when_absent():
    # Base has reasoning_effort=None, so inheritance returns None.
    template = _parse_agent_template("codex", {"command": ["codex", "exec"]})
    assert template.reasoning_effort is None


def test_parse_reasoning_effort_wrong_type_raises():
    with pytest.raises(SystemExit, match="reasoning_effort must be"):
        _parse_agent_template(
            "myagent", {"command": ["myagent"], "reasoning_effort": 42}
        )


def test_parse_reasoning_effort_flag_as_list_of_strings():
    template = _parse_agent_template(
        "myagent",
        {"command": ["myagent"], "reasoning_effort_flag": ["--thinking", "{effort}"]},
    )
    assert template.reasoning_effort_flag == ("--thinking", "{effort}")


def test_parse_reasoning_effort_flag_inherits_from_codex_base():
    template = _parse_agent_template("codex", {"command": ["codex", "exec"]})
    assert template.reasoning_effort_flag == ("-c", "model_reasoning_effort={effort}")


def test_parse_reasoning_effort_flag_inherits_from_claude_base():
    template = _parse_agent_template("claude", {"command": ["claude"]})
    assert template.reasoning_effort_flag == ("--effort", "{effort}")


def test_parse_provider_env_as_table():
    template = _parse_agent_template(
        "claude",
        {
            "command": ["claude"],
            "provider_env": {
                "ANTHROPIC_BASE_URL": "https://openrouter.ai/api",
                "ANTHROPIC_AUTH_TOKEN": "{env:OPENROUTER_API_KEY}",
                "ANTHROPIC_API_KEY": "",
            },
        },
    )
    # Order of keys in a Python dict is insertion order.
    assert template.provider_env == (
        ("ANTHROPIC_BASE_URL", "https://openrouter.ai/api"),
        ("ANTHROPIC_AUTH_TOKEN", "{env:OPENROUTER_API_KEY}"),
        ("ANTHROPIC_API_KEY", ""),
    )


def test_parse_provider_env_as_list_of_pairs():
    template = _parse_agent_template(
        "claude",
        {
            "command": ["claude"],
            "provider_env": [
                ["ANTHROPIC_BASE_URL", "https://openrouter.ai/api"],
                ["ANTHROPIC_AUTH_TOKEN", "{env:OPENROUTER_API_KEY}"],
            ],
        },
    )
    assert template.provider_env == (
        ("ANTHROPIC_BASE_URL", "https://openrouter.ai/api"),
        ("ANTHROPIC_AUTH_TOKEN", "{env:OPENROUTER_API_KEY}"),
    )


def test_parse_provider_env_inherits_from_base_when_absent():
    # Base has provider_env=() for all built-ins.
    template = _parse_agent_template("claude", {"command": ["claude"]})
    assert template.provider_env == ()


def test_parse_provider_env_wrong_type_raises():
    with pytest.raises(SystemExit, match="provider_env must be"):
        _parse_agent_template("myagent", {"command": ["myagent"], "provider_env": 42})


def test_parse_provider_env_list_of_pairs_invalid_shape():
    with pytest.raises(SystemExit, match="two-element"):
        _parse_agent_template(
            "myagent",
            {
                "command": ["myagent"],
                "provider_env": [["ANTHROPIC_BASE_URL"]],  # single-element list
            },
        )


def test_parse_provider_env_table_with_non_string_value_raises():
    with pytest.raises(SystemExit, match="string names to string values"):
        _parse_agent_template(
            "myagent", {"command": ["myagent"], "provider_env": {"KEY": 123}}
        )


def test_load_config_legacy_template_raises_migration_error(tmp_path):
    config_path = tmp_path / ".team-harness" / "config.toml"
    config_path.parent.mkdir()
    config_path.write_text(
        '[agents.codex]\ntemplate = "codex exec --yolo {prompt}"\n', encoding="utf-8"
    )

    with pytest.raises(SystemExit) as exc:
        load_config(cwd=str(tmp_path))

    message = str(exc.value)
    assert "agents.codex.template" in message
    assert "no longer supported" in message


def test_load_config_legacy_template_mixed_with_structured_also_errors(tmp_path):
    # Mixing legacy and structured used to warn and prefer structured; now it
    # errors the same way a pure legacy section does. Consistent behavior.
    config_path = tmp_path / ".team-harness" / "config.toml"
    config_path.parent.mkdir()
    config_path.write_text(
        """
[agents.codex]
template = "codex exec --yolo {prompt}"
command = ["codex", "exec"]
""",
        encoding="utf-8",
    )

    with pytest.raises(SystemExit, match="agents.codex.template"):
        load_config(cwd=str(tmp_path))


def test_parse_custom_structured_template_requires_command():
    with pytest.raises(SystemExit, match="command is required"):
        _parse_agent_template("myagent", {"shared_flags": ["--json"]})


def test_stream_json_capture_requires_match_and_field_path():
    with pytest.raises(SystemExit, match="requires match and field_path"):
        _parse_agent_template(
            "myagent",
            {
                "command": ["myagent"],
                "session_capture": {"strategy": "stream_json_event"},
            },
        )
