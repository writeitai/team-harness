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

    assert template == AgentTemplate(
        command=("codex", "exec"),
        shared_flags=("--json",),
        resume_prefix=("resume",),
        resume_flags=("{session_id}",),
        model_flag="--model",
        session_capture=SessionCapture(
            strategy="stream_json_event",
            match={"type": "thread.started"},
            field_path=("thread_id",),
        ),
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
