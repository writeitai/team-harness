# pyright: reportMissingParameterType=false


import click
import pytest

from team_harness import config as config_module
from team_harness.cli import _prepare_task
from team_harness.config import load_config


def test_load_config_precedence(tmp_path, monkeypatch):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[coordinator]
model = "file-model"
api_base = "https://file.example/v1"
api_key = "file-key"
allowed_agents = ["codex", "claude"]
shutdown_timeout_s = 12.5

[agents.codex]
template = "codex exec --model file-model {prompt}"

[agents.myagent]
template = "myagent {prompt}"
"""
    )
    monkeypatch.setattr(config_module, "CONFIG_PATH", config_path)
    monkeypatch.setenv("HARNESS_MODEL", "env-model")
    monkeypatch.setenv("HARNESS_API_BASE", "https://env.example/v1")
    monkeypatch.setenv("OPENROUTER_API_KEY", "env-key")
    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text("from file")

    config = load_config(
        model="cli-model",
        api_base="https://cli.example/v1",
        api_key="cli-key",
        allowed_agents="codex, gemini,,",
        system_prompt="inline",
        system_prompt_file=str(prompt_file),
        cwd=str(tmp_path),
    )

    assert config.model == "cli-model"
    assert config.api_base == "https://cli.example/v1"
    assert config.api_key == "cli-key"
    assert config.allowed_agents == ["codex", "gemini"]
    assert config.agent_templates["codex"] == "codex exec --model file-model {prompt}"
    assert config.agent_templates["myagent"] == "myagent {prompt}"
    assert config.shutdown_timeout_s == 12.5
    assert config.system_prompt_extension == "inline\n\nfrom file"


def test_missing_config_creates_default(tmp_path, monkeypatch, capsys):
    config_path = tmp_path / "config.toml"
    monkeypatch.setattr(config_module, "CONFIG_PATH", config_path)

    config = load_config()

    assert config_path.exists()
    assert config.model == "openai/gpt-4o"
    assert "Created default config" in capsys.readouterr().out


def test_prepare_task_validates_inputs(tmp_path):
    task_file = tmp_path / "task.txt"
    task_file.write_text("file task")
    assert _prepare_task(None, str(task_file)) == "file task"
    with pytest.raises(click.UsageError):
        _prepare_task(None, None)
    with pytest.raises(click.UsageError):
        _prepare_task("x", str(task_file))
