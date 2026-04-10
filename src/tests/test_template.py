# pyright: reportMissingParameterType=false

import warnings

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


def test_load_config_legacy_template_only_emits_no_warning(tmp_path):
    config_path = tmp_path / ".team-harness" / "config.toml"
    config_path.parent.mkdir()
    config_path.write_text(
        '[agents.codex]\ntemplate = "codex exec --yolo {prompt}"\n',
        encoding="utf-8",
    )

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        config = load_config(cwd=str(tmp_path))

    assert config.agent_templates["codex"] == "codex exec --yolo {prompt}"
    assert len(caught) == 0


def test_load_config_structured_template_wins_over_legacy_with_warning(
    tmp_path, monkeypatch
):
    config_path = tmp_path / ".team-harness" / "config.toml"
    config_path.parent.mkdir()
    config_path.write_text(
        """
[agents.codex]
template = "codex exec --yolo {prompt}"
command = ["codex", "exec"]
shared_flags = ["--json"]
resume_prefix = ["resume"]
resume_flags = ["{session_id}"]
""",
        encoding="utf-8",
    )

    with pytest.warns(UserWarning, match="structured form wins"):
        config = load_config(cwd=str(tmp_path))

    assert config.agent_templates["codex"] == AgentTemplate(
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
