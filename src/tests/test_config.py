# pyright: reportMissingParameterType=false

import click
import pytest

from team_harness import config as config_module
from team_harness.cli import _prepare_task
from team_harness.config import _deep_merge
from team_harness.config import _default_config_text
from team_harness.config import _parse_provider
from team_harness.config import Config
from team_harness.config import CONFIG_PATH
from team_harness.config import DEFAULT_TEMPLATES
from team_harness.config import find_local_config
from team_harness.config import load_config
from team_harness.config import RUNS_DIR
from team_harness.config import SKILLS_USER_DIR


def test_default_model_is_gpt_5_4():
    assert Config().model == "gpt-5.4"
    assert Config().provider == "openai_compat"
    assert Config().output_dir == "_outputs"


def test_default_templates_updated():
    assert (
        DEFAULT_TEMPLATES["codex"]
        == 'codex exec --yolo --model gpt-5.4 PROMPT="{prompt}"'
    )
    assert DEFAULT_TEMPLATES["gemini"] == 'gemini --approval-mode=yolo -p "{prompt}"'


def test_find_local_config_in_cwd(tmp_path):
    config_path = tmp_path / ".team-harness" / "config.toml"
    config_path.parent.mkdir()
    config_path.write_text("[coordinator]\nmodel = 'local'\n")

    assert find_local_config(tmp_path) == config_path


def test_find_local_config_walks_up(tmp_path):
    config_path = tmp_path / ".team-harness" / "config.toml"
    config_path.parent.mkdir()
    config_path.write_text("[coordinator]\nmodel = 'local'\n")
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)

    assert find_local_config(nested) == config_path


def test_find_local_config_none_when_missing(tmp_path):
    assert find_local_config(tmp_path) is None


def test_deep_merge_nested_dicts():
    merged = _deep_merge(
        base={
            "coordinator": {
                "model": "global-model",
                "allowed_agents": ["codex", "claude"],
            },
            "agents": {"codex": {"template": "codex global {prompt}"}},
        },
        override={
            "coordinator": {"model": "local-model", "allowed_agents": ["gemini"]},
            "agents": {"gemini": {"template": "gemini local {prompt}"}},
        },
    )

    assert merged == {
        "coordinator": {"model": "local-model", "allowed_agents": ["gemini"]},
        "agents": {
            "codex": {"template": "codex global {prompt}"},
            "gemini": {"template": "gemini local {prompt}"},
        },
    }


def test_load_config_precedence(tmp_path, monkeypatch):
    global_path = tmp_path / "global" / "config.toml"
    global_path.parent.mkdir()
    global_path.write_text(
        """
[coordinator]
model = "file-model"
api_base = "https://file.example/v1"
api_key = "file-key"
allowed_agents = ["codex", "claude"]
output_dir = "artifacts"
shutdown_timeout_s = 12.5

[agents.codex]
template = "codex exec --model file-model {prompt}"

[agents.myagent]
template = "myagent {prompt}"
"""
    )
    monkeypatch.setattr(config_module, "CONFIG_PATH", global_path)
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
    assert config.output_dir == "artifacts"
    assert config.system_prompt_extension == "inline\n\nfrom file"
    assert config.global_config_path == global_path.resolve()
    assert config.local_config_path is None


def test_local_overrides_global_model(tmp_path, monkeypatch):
    global_path = tmp_path / "home" / ".team-harness" / "config.toml"
    global_path.parent.mkdir(parents=True)
    global_path.write_text('[coordinator]\nmodel = "global-model"\n')
    local_path = tmp_path / "project" / ".team-harness" / "config.toml"
    local_path.parent.mkdir(parents=True)
    local_path.write_text('[coordinator]\nmodel = "local-model"\n')
    monkeypatch.setattr(config_module, "CONFIG_PATH", global_path)

    config = load_config(cwd=str(tmp_path / "project" / "src"))

    assert config.model == "local-model"


def test_local_preserves_global_agent_templates(tmp_path, monkeypatch):
    global_path = tmp_path / "home" / ".team-harness" / "config.toml"
    global_path.parent.mkdir(parents=True)
    global_path.write_text(
        """
[agents.codex]
template = "codex global {prompt}"

[agents.claude]
template = "claude global {prompt}"
"""
    )
    local_path = tmp_path / "project" / ".team-harness" / "config.toml"
    local_path.parent.mkdir(parents=True)
    local_path.write_text('[coordinator]\nallowed_agents = ["claude"]\n')
    monkeypatch.setattr(config_module, "CONFIG_PATH", global_path)

    config = load_config(cwd=str(tmp_path / "project"))

    assert config.agent_templates == {
        "codex": "codex global {prompt}",
        "claude": "claude global {prompt}",
    }
    assert config.allowed_agents == ["claude"]


def test_local_only_config_uses_defaults_plus_local(tmp_path, monkeypatch):
    global_path = tmp_path / "home" / ".team-harness" / "config.toml"
    local_path = tmp_path / "project" / ".team-harness" / "config.toml"
    local_path.parent.mkdir(parents=True)
    local_path.write_text(
        """
[coordinator]
allowed_agents = ["gemini"]

[agents.gemini]
template = "gemini local {prompt}"
"""
    )
    monkeypatch.setattr(config_module, "CONFIG_PATH", global_path)

    config = load_config(cwd=str(tmp_path / "project"))

    assert config.model == "gpt-5.4"
    assert config.api_base == "https://openrouter.ai/api/v1"
    assert config.allowed_agents == ["gemini"]
    assert config.agent_templates["gemini"] == "gemini local {prompt}"
    assert config.global_config_path is None
    assert config.local_config_path == local_path.resolve()


def test_invalid_global_toml_has_path(tmp_path, monkeypatch):
    global_path = tmp_path / "home" / ".team-harness" / "config.toml"
    global_path.parent.mkdir(parents=True)
    global_path.write_text("[coordinator]\nmodel = [\n")
    monkeypatch.setattr(config_module, "CONFIG_PATH", global_path)

    with pytest.raises(SystemExit, match=f"Invalid TOML in {global_path.resolve()}"):
        load_config(cwd=str(tmp_path))


def test_invalid_local_toml_has_path(tmp_path, monkeypatch):
    global_path = tmp_path / "home" / ".team-harness" / "config.toml"
    monkeypatch.setattr(config_module, "CONFIG_PATH", global_path)
    local_path = tmp_path / "project" / ".team-harness" / "config.toml"
    local_path.parent.mkdir(parents=True)
    local_path.write_text("[coordinator]\nmodel = [\n")

    with pytest.raises(SystemExit, match=f"Invalid TOML in {local_path.resolve()}"):
        load_config(cwd=str(tmp_path / "project"))


def test_home_directory_does_not_double_load_global_file(tmp_path, monkeypatch):
    home_config = tmp_path / ".team-harness" / "config.toml"
    home_config.parent.mkdir()
    home_config.write_text('[coordinator]\nmodel = "home-model"\n')
    monkeypatch.setattr(config_module, "CONFIG_PATH", home_config)

    config = load_config(cwd=str(tmp_path))

    assert config.model == "home-model"
    assert config.global_config_path == home_config.resolve()
    assert config.local_config_path is None


def test_load_config_sets_provenance_paths(tmp_path, monkeypatch):
    global_path = tmp_path / "home" / ".team-harness" / "config.toml"
    global_path.parent.mkdir(parents=True)
    global_path.write_text('[coordinator]\nmodel = "global-model"\n')
    local_path = tmp_path / "project" / ".team-harness" / "config.toml"
    local_path.parent.mkdir(parents=True)
    local_path.write_text('[coordinator]\napi_base = "https://local.example/v1"\n')
    monkeypatch.setattr(config_module, "CONFIG_PATH", global_path)

    config = load_config(cwd=str(tmp_path / "project"))

    assert config.global_config_path == global_path.resolve()
    assert config.local_config_path == local_path.resolve()
    assert config.api_base == "https://local.example/v1"


def test_no_config_uses_defaults_without_creating_global_file(tmp_path, monkeypatch):
    global_path = tmp_path / "home" / ".team-harness" / "config.toml"
    monkeypatch.setattr(config_module, "CONFIG_PATH", global_path)

    config = load_config(cwd=str(tmp_path))

    assert config.model == "gpt-5.4"
    assert config.provider == "openai_compat"
    assert config.global_config_path is None
    assert config.local_config_path is None
    assert not global_path.exists()


def test_default_templates_use_th_for_harness():
    assert DEFAULT_TEMPLATES["harness"] == "th run {prompt}"


def test_default_config_text_uses_th_for_harness():
    default_config = _default_config_text()
    assert default_config.startswith("# th")
    assert 'output_dir = "_outputs"' in default_config
    assert 'template = "th run {prompt}"' in default_config


def test_local_config_text_includes_output_dir():
    assert 'output_dir = "_outputs"' in config_module._local_config_text()


def test_config_paths_remain_under_team_harness_dir():
    assert CONFIG_PATH == config_module.Path.home() / ".team-harness" / "config.toml"
    assert RUNS_DIR == config_module.Path.home() / ".team-harness" / "runs"
    assert SKILLS_USER_DIR == config_module.Path.home() / ".team-harness" / "skills"


def test_provider_aware_codex_defaults(tmp_path, monkeypatch):
    global_path = tmp_path / "home" / ".team-harness" / "config.toml"
    monkeypatch.setattr(config_module, "CONFIG_PATH", global_path)

    config = load_config(provider="codex", cwd=str(tmp_path))

    assert config.provider == "codex"
    assert config.model == "codex-mini-latest"
    assert config.api_base == ""


def test_load_config_reads_provider_and_codex_auth_env(tmp_path, monkeypatch):
    global_path = tmp_path / "home" / ".team-harness" / "config.toml"
    monkeypatch.setattr(config_module, "CONFIG_PATH", global_path)
    monkeypatch.setenv("HARNESS_PROVIDER", "codex")
    monkeypatch.setenv("HARNESS_CODEX_AUTH_PATH", "relative/auth.json")

    config = load_config(cwd=str(tmp_path))

    assert config.provider == "codex"
    assert config.codex_auth_path == "relative/auth.json"
    assert config.model == "codex-mini-latest"


def test_parse_provider_normalizes_openrouter_alias():
    with pytest.warns(UserWarning):
        assert _parse_provider("openrouter") == "openai_compat"


def test_prepare_task_validates_inputs(tmp_path):
    task_file = tmp_path / "task.txt"
    task_file.write_text("file task")

    assert _prepare_task(task=None, task_file=str(task_file)) == "file task"
    with pytest.raises(click.UsageError):
        _prepare_task(task=None, task_file=None)
    with pytest.raises(click.UsageError):
        _prepare_task(task="x", task_file=str(task_file))
