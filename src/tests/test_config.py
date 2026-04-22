# pyright: reportMissingParameterType=false

import click
import pytest

from team_harness import config as config_module
from team_harness.agents.template import DEFAULT_AGENT_TEMPLATES
from team_harness.cli import _prepare_task
from team_harness.config import _deep_merge
from team_harness.config import _default_config_text
from team_harness.config import _local_config_text
from team_harness.config import _parse_provider
from team_harness.config import Config
from team_harness.config import CONFIG_PATH
from team_harness.config import find_local_config
from team_harness.config import load_config
from team_harness.config import PROMPT_FILE_MAX_BYTES
from team_harness.config import RUNS_DIR
from team_harness.config import SKILLS_USER_DIR
from team_harness.coordinator.system_prompt import COORDINATOR_PROMPT
from team_harness.coordinator.system_prompt import DEFAULT_WORKER_FOOTER


def _write_global_config(
    tmp_path, monkeypatch, coordinator_lines: list[str], *, agents_text: str = ""
):
    global_path = tmp_path / "home" / ".team-harness" / "config.toml"
    global_path.parent.mkdir(parents=True, exist_ok=True)
    body = "[coordinator]\n" + "\n".join(coordinator_lines)
    if agents_text:
        body += "\n\n" + agents_text.strip() + "\n"
    global_path.write_text(body, encoding="utf-8")
    monkeypatch.setattr(config_module, "CONFIG_PATH", global_path)
    return global_path


def test_default_model_is_gpt_5_4():
    assert Config().model == "gpt-5.4"
    assert Config().provider == "openai_compat"
    assert Config().output_dir == "_outputs"
    assert Config().coordinator_prompt == COORDINATOR_PROMPT
    assert Config().worker_suffix == ""
    assert Config().worker_footer == DEFAULT_WORKER_FOOTER


def test_default_agent_templates_structured_shape():
    assert DEFAULT_AGENT_TEMPLATES["codex"].command == ("codex", "exec")
    assert "--json" in DEFAULT_AGENT_TEMPLATES["codex"].shared_flags
    assert DEFAULT_AGENT_TEMPLATES["claude"].command == ("claude",)
    assert "--verbose" in DEFAULT_AGENT_TEMPLATES["claude"].shared_flags


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
command = ["codex", "exec"]

[agents.myagent]
command = ["myagent"]
model_flag = false
"""
    )
    monkeypatch.setattr(config_module, "CONFIG_PATH", global_path)
    monkeypatch.setenv("TEAM_HARNESS_MODEL", "env-model")
    monkeypatch.setenv("TEAM_HARNESS_API_BASE", "https://env.example/v1")
    monkeypatch.setenv("OPENROUTER_API_KEY", "env-key")
    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text("from file", encoding="utf-8")

    config = load_config(
        model="cli-model",
        api_base="https://cli.example/v1",
        api_key="cli-key",
        allowed_agents="codex, gemini,,",
        system_prompt="inline",
        cli_system_prompt_file=str(prompt_file),
        cwd=str(tmp_path),
    )

    assert config.model == "cli-model"
    assert config.api_base == "https://cli.example/v1"
    assert config.api_key == "cli-key"
    assert config.allowed_agents == ["codex", "gemini"]
    # The file override sets command via structured form.
    assert config.agent_templates["codex"].command == ("codex", "exec")
    assert config.agent_templates["myagent"].command == ("myagent",)
    assert config.shutdown_timeout_s == 12.5
    assert config.output_dir == "artifacts"
    assert config.system_prompt_extension == "inline\n\nfrom file"
    assert config.coordinator_prompt == COORDINATOR_PROMPT
    assert config.worker_suffix == ""
    assert config.worker_footer == DEFAULT_WORKER_FOOTER
    assert config.global_config_path == global_path.resolve()
    assert config.local_config_path is None


def test_coordinator_prompt_file_supplies_coordinator_prompt(tmp_path, monkeypatch):
    global_path = _write_global_config(
        tmp_path, monkeypatch, ['coordinator_prompt_file = "coordinator_prompt.md"']
    )
    (global_path.parent / "coordinator_prompt.md").write_text(
        "project base prompt", encoding="utf-8"
    )

    config = load_config(cwd=str(tmp_path / "project"))

    assert config.coordinator_prompt == "project base prompt"


def test_local_coordinator_prompt_file_overrides_global(tmp_path, monkeypatch):
    global_path = _write_global_config(
        tmp_path, monkeypatch, ['coordinator_prompt_file = "coordinator_prompt.md"']
    )
    (global_path.parent / "coordinator_prompt.md").write_text(
        "global base prompt", encoding="utf-8"
    )
    local_path = tmp_path / "project" / ".team-harness" / "config.toml"
    local_path.parent.mkdir(parents=True)
    local_path.write_text(
        '[coordinator]\ncoordinator_prompt_file = "coordinator_prompt.md"\n',
        encoding="utf-8",
    )
    (local_path.parent / "coordinator_prompt.md").write_text(
        "local base prompt", encoding="utf-8"
    )

    config = load_config(cwd=str(tmp_path / "project"))

    assert config.coordinator_prompt == "local base prompt"


def test_global_base_prompt_paths_resolve_relative_to_global_config_dir(
    tmp_path, monkeypatch
):
    global_path = _write_global_config(
        tmp_path,
        monkeypatch,
        ['coordinator_prompt_file = "prompts/coordinator_prompt.md"'],
    )
    prompt_path = global_path.parent / "prompts" / "coordinator_prompt.md"
    prompt_path.parent.mkdir()
    prompt_path.write_text("global relative prompt", encoding="utf-8")

    config = load_config(cwd=str(tmp_path / "project"))

    assert config.coordinator_prompt == "global relative prompt"


def test_absolute_base_prompt_paths_work(tmp_path, monkeypatch):
    prompt_path = tmp_path / "absolute-base.md"
    prompt_path.write_text("absolute prompt", encoding="utf-8")
    _write_global_config(
        tmp_path, monkeypatch, [f'coordinator_prompt_file = "{prompt_path}"']
    )

    config = load_config(cwd=str(tmp_path / "project"))

    assert config.coordinator_prompt == "absolute prompt"


def test_missing_explicit_coordinator_prompt_file_warns_and_falls_back(
    tmp_path, monkeypatch
):
    _write_global_config(
        tmp_path,
        monkeypatch,
        ['coordinator_prompt_file = "missing-coordinator-prompt.md"'],
    )

    with pytest.warns(UserWarning, match="coordinator_prompt_file"):
        config = load_config(cwd=str(tmp_path / "project"))

    assert config.coordinator_prompt == COORDINATOR_PROMPT


@pytest.mark.parametrize("file_content", [None, ""])
def test_missing_or_empty_worker_suffix_file_becomes_empty_string(
    tmp_path, monkeypatch, file_content
):
    global_path = _write_global_config(
        tmp_path, monkeypatch, ['worker_suffix_file = "worker_suffix.md"']
    )
    suffix_path = global_path.parent / "worker_suffix.md"
    if file_content is not None:
        suffix_path.write_text(file_content, encoding="utf-8")

    config = load_config(cwd=str(tmp_path / "project"))

    assert config.worker_suffix == ""


def test_prompt_file_permission_errors_raise_system_exit(tmp_path, monkeypatch):
    global_path = _write_global_config(
        tmp_path, monkeypatch, ['coordinator_prompt_file = "coordinator_prompt.md"']
    )
    prompt_path = (global_path.parent / "coordinator_prompt.md").resolve()
    prompt_path.write_text("restricted prompt", encoding="utf-8")
    original_read_text = config_module.Path.read_text

    def fake_read_text(self, *args, **kwargs):
        if self.resolve() == prompt_path:
            raise PermissionError("denied")
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(config_module.Path, "read_text", fake_read_text)

    with pytest.raises(SystemExit, match="permission denied"):
        load_config(cwd=str(tmp_path / "project"))


def test_non_utf8_prompt_files_raise_system_exit(tmp_path, monkeypatch):
    global_path = _write_global_config(
        tmp_path, monkeypatch, ['coordinator_prompt_file = "coordinator_prompt.md"']
    )
    (global_path.parent / "coordinator_prompt.md").write_bytes(b"\xff\xfe\x00\x00")

    with pytest.raises(SystemExit, match="UTF-8"):
        load_config(cwd=str(tmp_path / "project"))


def test_oversized_prompt_files_raise_system_exit(tmp_path, monkeypatch):
    global_path = _write_global_config(
        tmp_path, monkeypatch, ['coordinator_prompt_file = "coordinator_prompt.md"']
    )
    (global_path.parent / "coordinator_prompt.md").write_text(
        "a" * (PROMPT_FILE_MAX_BYTES + 1), encoding="utf-8"
    )

    with pytest.raises(SystemExit, match="exceeds"):
        load_config(cwd=str(tmp_path / "project"))


def test_cli_system_prompt_file_backward_compatibility(tmp_path, monkeypatch):
    _write_global_config(tmp_path, monkeypatch, [])
    cwd = tmp_path / "project"
    cwd.mkdir()
    (cwd / "prompt.txt").write_text("from cwd relative file", encoding="utf-8")

    config = load_config(
        system_prompt="inline", cli_system_prompt_file="prompt.txt", cwd=str(cwd)
    )

    assert config.system_prompt_extension == "inline\n\nfrom cwd relative file"


def test_worker_suffix_file_supplies_worker_suffix(tmp_path, monkeypatch):
    global_path = _write_global_config(
        tmp_path, monkeypatch, ['worker_suffix_file = "worker_suffix.md"']
    )
    (global_path.parent / "worker_suffix.md").write_text(
        "Always verify your work.", encoding="utf-8"
    )

    config = load_config(cwd=str(tmp_path / "project"))

    assert config.worker_suffix == "Always verify your work."


def test_local_worker_suffix_file_overrides_global(tmp_path, monkeypatch):
    global_path = _write_global_config(
        tmp_path, monkeypatch, ['worker_suffix_file = "worker_suffix.md"']
    )
    (global_path.parent / "worker_suffix.md").write_text(
        "global suffix", encoding="utf-8"
    )
    local_path = tmp_path / "project" / ".team-harness" / "config.toml"
    local_path.parent.mkdir(parents=True)
    local_path.write_text(
        '[coordinator]\nworker_suffix_file = "worker_suffix.md"\n', encoding="utf-8"
    )
    (local_path.parent / "worker_suffix.md").write_text(
        "local suffix", encoding="utf-8"
    )

    config = load_config(cwd=str(tmp_path / "project"))

    assert config.worker_suffix == "local suffix"


def test_worker_footer_file_supplies_worker_footer(tmp_path, monkeypatch):
    global_path = _write_global_config(
        tmp_path, monkeypatch, ['worker_footer_file = "worker_footer.md"']
    )
    (global_path.parent / "worker_footer.md").write_text(
        "Artifacts live in {session_output_dir}.", encoding="utf-8"
    )

    config = load_config(cwd=str(tmp_path / "project"))

    assert config.worker_footer == "Artifacts live in {session_output_dir}."


def test_missing_worker_footer_file_uses_default(tmp_path, monkeypatch):
    _write_global_config(
        tmp_path, monkeypatch, ['worker_footer_file = "missing-worker-footer.md"']
    )

    config = load_config(cwd=str(tmp_path / "project"))

    assert config.worker_footer == DEFAULT_WORKER_FOOTER


def test_empty_coordinator_prompt_file_warns_and_falls_back(tmp_path, monkeypatch):
    global_path = _write_global_config(
        tmp_path, monkeypatch, ['coordinator_prompt_file = "coordinator_prompt.md"']
    )
    (global_path.parent / "coordinator_prompt.md").write_text("", encoding="utf-8")

    with pytest.warns(UserWarning, match="coordinator_prompt_file"):
        config = load_config(cwd=str(tmp_path / "project"))

    assert config.coordinator_prompt == COORDINATOR_PROMPT


def test_whitespace_only_coordinator_prompt_file_warns_and_falls_back(
    tmp_path, monkeypatch
):
    global_path = _write_global_config(
        tmp_path, monkeypatch, ['coordinator_prompt_file = "coordinator_prompt.md"']
    )
    (global_path.parent / "coordinator_prompt.md").write_text(
        "   \n  \n", encoding="utf-8"
    )

    with pytest.warns(UserWarning, match="coordinator_prompt_file"):
        config = load_config(cwd=str(tmp_path / "project"))

    assert config.coordinator_prompt == COORDINATOR_PROMPT


def test_cli_system_prompt_file_missing_raises_system_exit(tmp_path, monkeypatch):
    _write_global_config(tmp_path, monkeypatch, [])

    with pytest.raises(SystemExit, match="not found"):
        load_config(cli_system_prompt_file="nonexistent.md", cwd=str(tmp_path))


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
command = ["codex", "global"]

[agents.claude]
command = ["claude", "global"]
"""
    )
    local_path = tmp_path / "project" / ".team-harness" / "config.toml"
    local_path.parent.mkdir(parents=True)
    local_path.write_text('[coordinator]\nallowed_agents = ["claude"]\n')
    monkeypatch.setattr(config_module, "CONFIG_PATH", global_path)

    config = load_config(cwd=str(tmp_path / "project"))

    assert config.agent_templates["codex"].command == ("codex", "global")
    assert config.agent_templates["claude"].command == ("claude", "global")
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
command = ["gemini", "local"]
"""
    )
    monkeypatch.setattr(config_module, "CONFIG_PATH", global_path)

    config = load_config(cwd=str(tmp_path / "project"))

    assert config.model == "gpt-5.4"
    assert config.api_base == "https://openrouter.ai/api/v1"
    assert config.allowed_agents == ["gemini"]
    assert config.agent_templates["gemini"].command == ("gemini", "local")
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


def test_default_agent_templates_use_th_for_harness():
    assert DEFAULT_AGENT_TEMPLATES["harness"].command == ("th", "run")
    assert DEFAULT_AGENT_TEMPLATES["harness"].model_flag == "--model"


def test_default_config_text_uses_structured_agents():
    default_config = _default_config_text()
    assert default_config.startswith("# th")
    assert 'output_dir = "_outputs"' in default_config
    # Legacy single-string form must not appear anywhere.
    import re

    assert re.search(r"(?m)^template\s*=\s*\"", default_config) is None
    # Structured form is visible.
    assert "[agents.codex]" in default_config
    assert 'command = ["codex", "exec"]' in default_config
    assert "[agents.codex.session_capture]" in default_config
    assert 'command = ["th", "run"]' in default_config
    assert (
        'coordinator_system_message_file = "coordinator_system_message.md"'
        in default_config
    )
    assert 'worker_suffix_file = "worker_suffix.md"' in default_config
    assert 'worker_footer_file = "worker_footer.md"' in default_config


def test_local_config_text_uses_structured_agents():
    local_config = _local_config_text()
    assert 'output_dir = "_outputs"' in local_config
    import re

    assert re.search(r"(?m)^template\s*=\s*\"", local_config) is None
    assert "[agents.codex]" in local_config
    assert 'command = ["codex", "exec"]' in local_config


def _write_sample_and_load(
    tmp_path, monkeypatch, sample_text: str, *, as_global: bool = True
) -> Config:
    if as_global:
        path = tmp_path / "home" / ".team-harness" / "config.toml"
    else:
        path = tmp_path / "project" / ".team-harness" / "config.toml"
    path.parent.mkdir(parents=True)
    path.write_text(sample_text, encoding="utf-8")
    if as_global:
        monkeypatch.setattr(config_module, "CONFIG_PATH", path)
        return load_config(cwd=str(tmp_path))
    else:
        monkeypatch.setattr(
            config_module, "CONFIG_PATH", tmp_path / "home" / "config.toml"
        )
        return load_config(cwd=str(path.parent.parent))


def test_default_config_text_roundtrip_matches_builtin_defaults(tmp_path, monkeypatch):
    """The shipped `th init` sample must parse back to the exact built-in
    defaults. This is the canonical check that the docs don't drift from
    the real defaults."""

    config = _write_sample_and_load(
        tmp_path, monkeypatch, _default_config_text(), as_global=True
    )
    for name, expected in DEFAULT_AGENT_TEMPLATES.items():
        assert name in config.agent_templates, f"missing agent {name}"
        assert config.agent_templates[name] == expected, (
            f"agent {name} mismatch: got {config.agent_templates[name]}, "
            f"expected {expected}"
        )


def test_local_config_text_roundtrip_matches_builtin_defaults(tmp_path, monkeypatch):
    config = _write_sample_and_load(
        tmp_path, monkeypatch, _local_config_text(), as_global=False
    )
    for name, expected in DEFAULT_AGENT_TEMPLATES.items():
        assert name in config.agent_templates, f"missing agent {name}"
        assert config.agent_templates[name] == expected


def test_load_config_legacy_template_raises_migration_error(tmp_path, monkeypatch):
    path = tmp_path / "home" / ".team-harness" / "config.toml"
    path.parent.mkdir(parents=True)
    path.write_text(
        '[agents.codex]\ntemplate = "codex exec --yolo {prompt}"\n', encoding="utf-8"
    )
    monkeypatch.setattr(config_module, "CONFIG_PATH", path)

    with pytest.raises(SystemExit) as exc:
        load_config(cwd=str(tmp_path))

    message = str(exc.value)
    assert "agents.codex.template" in message
    assert "no longer supported" in message
    assert "structured form" in message or "command = [" in message
    assert "README.md" in message or "th init --force" in message
    # The error names the offending file so users know WHERE to edit.
    assert str(path) in message


def test_load_config_legacy_global_plus_structured_local_reports_global_file(
    tmp_path, monkeypatch
):
    """A legacy `template` in the GLOBAL config must still hard-error and
    name the global file, even when the local file already uses the
    structured form for the same agent type. Without per-file provenance
    this case would produce a misleading error."""

    global_path = tmp_path / "home" / ".team-harness" / "config.toml"
    global_path.parent.mkdir(parents=True)
    global_path.write_text(
        '[agents.codex]\ntemplate = "codex legacy {prompt}"\n', encoding="utf-8"
    )
    local_path = tmp_path / "project" / ".team-harness" / "config.toml"
    local_path.parent.mkdir(parents=True)
    local_path.write_text(
        '[agents.codex]\ncommand = ["codex", "structured"]\n', encoding="utf-8"
    )
    monkeypatch.setattr(config_module, "CONFIG_PATH", global_path)

    with pytest.raises(SystemExit) as exc:
        load_config(cwd=str(tmp_path / "project"))

    message = str(exc.value)
    assert "agents.codex.template" in message
    assert str(global_path) in message
    # Must NOT point at the local file, which is already migrated.
    assert str(local_path) not in message


def test_load_config_legacy_local_plus_structured_global_reports_local_file(
    tmp_path, monkeypatch
):
    global_path = tmp_path / "home" / ".team-harness" / "config.toml"
    global_path.parent.mkdir(parents=True)
    global_path.write_text(
        '[agents.codex]\ncommand = ["codex", "structured"]\n', encoding="utf-8"
    )
    local_path = tmp_path / "project" / ".team-harness" / "config.toml"
    local_path.parent.mkdir(parents=True)
    local_path.write_text(
        '[agents.codex]\ntemplate = "codex legacy {prompt}"\n', encoding="utf-8"
    )
    monkeypatch.setattr(config_module, "CONFIG_PATH", global_path)

    with pytest.raises(SystemExit) as exc:
        load_config(cwd=str(tmp_path / "project"))

    message = str(exc.value)
    assert "agents.codex.template" in message
    assert str(local_path) in message
    assert str(global_path) not in message


def test_default_config_text_contains_verified_flag_tokens():
    """Explicit raw-text assertion that the shipped sample contains the
    exact flag tokens from DEFAULT_AGENT_TEMPLATES. Guards against
    accidental omissions that the round-trip test alone would mask
    (because `_parse_agent_template` inherits missing fields)."""

    text = _default_config_text()
    # Codex
    assert '"--dangerously-bypass-approvals-and-sandbox"' in text
    assert '"--skip-git-repo-check"' in text
    assert '"--json"' in text
    assert 'resume_prefix = ["resume"]' in text
    assert 'resume_flags = ["{session_id}"]' in text
    assert "[agents.codex.session_capture]" in text
    assert 'match = { type = "thread.started" }' in text
    assert 'field_path = ["thread_id"]' in text
    # Codex default model — the whole point of this follow-up.
    assert 'default_model = "gpt-5.4"' in text
    # Gemini
    assert 'prompt_flag = "-p"' in text
    assert "[agents.gemini.session_capture]" in text
    assert 'match = { type = "init" }' in text
    # Claude (the --verbose requirement is critical)
    assert '"--verbose"' in text
    assert '"--dangerously-skip-permissions"' in text
    assert "[agents.claude.session_capture]" in text
    assert 'match = { type = "system", subtype = "init" }' in text
    # Claude model_env_vars — the 3 'main model' ones, NOT the haiku ones.
    assert '"ANTHROPIC_MODEL"' in text
    assert '"ANTHROPIC_DEFAULT_SONNET_MODEL"' in text
    assert '"ANTHROPIC_DEFAULT_OPUS_MODEL"' in text
    assert '"ANTHROPIC_DEFAULT_HAIKU_MODEL"' not in text
    assert '"ANTHROPIC_SMALL_FAST_MODEL"' not in text
    assert '"CLAUDE_CODE_SUBAGENT_MODEL"' not in text
    # Reasoning effort tokens present (commented default value, flag shape).
    assert 'reasoning_effort_flag = ["-c", "model_reasoning_effort={effort}"]' in text
    assert 'reasoning_effort_flag = ["--effort", "{effort}"]' in text
    assert '# reasoning_effort = "high"' in text
    # OpenRouter recipe for claude is present (even if commented out).
    assert "[agents.claude.provider_env]" in text
    assert "{env:OPENROUTER_API_KEY}" in text
    assert 'ANTHROPIC_BASE_URL = "https://openrouter.ai/api"' in text
    # OpenRouter recipe for codex is also present. Every line of the
    # commented block is prefixed with `# ` so the default sample stays
    # pointed at the native provider.
    assert "OpenRouter recipe for Codex" in text
    assert '#     "-c", "model_provider=openrouter",' in text
    assert 'openrouter.base_url="https://openrouter.ai/api/v1"' in text
    assert '# default_model = "openai/gpt-5.3-codex"' in text
    # TeamHarness
    assert 'command = ["th", "run"]' in text
    assert 'model_flag = "--model"' in text


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
    monkeypatch.setenv("TEAM_HARNESS_PROVIDER", "codex")
    monkeypatch.setenv("TEAM_HARNESS_CODEX_AUTH_PATH", "relative/auth.json")

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
