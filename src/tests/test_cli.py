# pyright: reportMissingParameterType=false, reportArgumentType=false

import json

from click.testing import CliRunner
import pytest

from team_harness import config as config_module
from team_harness.cli import _repl
from team_harness.cli import _run
from team_harness.cli import main
from team_harness.config import Config
from team_harness.harness import _warn_provider_startup


def test_help_uses_th_prog_name():
    runner = CliRunner()
    result = runner.invoke(main, ["--help"], prog_name="th")
    assert result.exit_code == 0
    assert "Usage: th" in result.output
    assert "th \u2014 multi-agent AI orchestration harness." in result.output


def test_team_harness_alias_still_works():
    runner = CliRunner()
    result = runner.invoke(main, ["--help"], prog_name="team-harness")
    assert result.exit_code == 0
    assert "Usage: team-harness" in result.output


def test_logs_empty(monkeypatch, tmp_path):
    monkeypatch.setattr("team_harness.cli.RUNS_DIR", tmp_path)
    runner = CliRunner()
    result = runner.invoke(main, ["logs"], prog_name="th")
    assert result.exit_code == 0
    assert "No runs yet." in result.output


def test_init_creates_local_config(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()

    result = runner.invoke(main, ["init"])

    config_path = tmp_path / ".team-harness" / "config.toml"
    assert result.exit_code == 0
    assert config_path.exists()
    assert "Project-level team-harness config." in config_path.read_text()
    assert str(config_path) in result.output


def test_init_refuses_overwrite_without_force(monkeypatch, tmp_path):
    config_path = tmp_path / ".team-harness" / "config.toml"
    config_path.parent.mkdir()
    config_path.write_text("existing")
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()

    result = runner.invoke(main, ["init"])

    assert result.exit_code != 0
    assert "already exists" in result.output
    assert config_path.read_text() == "existing"


def test_init_force_overwrites_local(monkeypatch, tmp_path):
    config_path = tmp_path / ".team-harness" / "config.toml"
    config_path.parent.mkdir()
    config_path.write_text("existing")
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()

    result = runner.invoke(main, ["init", "--force"])

    assert result.exit_code == 0
    assert "Project-level team-harness config." in config_path.read_text()


def test_init_global_creates_global_config(monkeypatch, tmp_path):
    global_path = tmp_path / "home" / ".team-harness" / "config.toml"
    monkeypatch.setattr("team_harness.cli.CONFIG_PATH", global_path)
    runner = CliRunner()

    result = runner.invoke(main, ["init", "--global"])

    assert result.exit_code == 0
    assert global_path.exists()
    assert 'model = "gpt-5.4"' in global_path.read_text()
    assert str(global_path) in result.output


def test_init_global_refuses_overwrite_without_force(monkeypatch, tmp_path):
    global_path = tmp_path / "home" / ".team-harness" / "config.toml"
    global_path.parent.mkdir(parents=True)
    global_path.write_text("existing")
    monkeypatch.setattr("team_harness.cli.CONFIG_PATH", global_path)
    runner = CliRunner()

    result = runner.invoke(main, ["init", "--global"])

    assert result.exit_code != 0
    assert "already exists" in result.output
    assert global_path.read_text() == "existing"


def test_init_global_force_overwrites(monkeypatch, tmp_path):
    global_path = tmp_path / "home" / ".team-harness" / "config.toml"
    global_path.parent.mkdir(parents=True)
    global_path.write_text("existing")
    monkeypatch.setattr("team_harness.cli.CONFIG_PATH", global_path)
    runner = CliRunner()

    result = runner.invoke(main, ["init", "--global", "--force"])

    assert result.exit_code == 0
    assert 'template = "codex exec --yolo --model gpt-5.4 PROMPT=\\"{prompt}\\""' in (
        global_path.read_text()
    )


@pytest.mark.asyncio
async def test_run_without_config_prints_no_config_hint(monkeypatch, tmp_path):
    class FakeClient:
        api_base = "http://localhost:11434/v1"
        model = "test/model"
        provider = "openai_compat"

        async def aclose(self):
            return None

    printed_messages: list[str] = []

    class FakeConsole:
        def start(self):
            return None

        def stop(self):
            return None

        def print(self, message):
            printed_messages.append(message)

        def begin_turn(self, n):
            return None

        def begin_streaming(self):
            return None

        def stream_token(self, token):
            return None

        def end_streaming(self):
            return None

        def end_turn(self):
            return None

        def tool_call_start(self, name, args):
            return None

        def agent_event(self, event, state):
            return None

        def context_warning(self):
            return None

        def reset_separator(self):
            return None

        def print_agent_panel_inline(self):
            return None

    async def fake_resolve_model_limit(model_id, client, config):
        return 128_000

    async def fake_run(messages, config, run_log, ui, tool_registry, client, ctx):
        return None

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.setattr(config_module, "CONFIG_PATH", tmp_path / "home" / "config.toml")
    monkeypatch.setattr("team_harness.harness.RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(
        "team_harness.harness._make_client", lambda config: FakeClient()
    )
    monkeypatch.setattr(
        "team_harness.harness.resolve_model_limit", fake_resolve_model_limit
    )
    monkeypatch.setattr("team_harness.harness.make_console", lambda **_: FakeConsole())
    monkeypatch.setattr("team_harness.harness.load_skills", lambda cwd=None: [])
    monkeypatch.setattr(
        "team_harness.harness.validate_templates", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(
        "team_harness.harness.build_system_prompt", lambda *args, **kwargs: "system"
    )
    monkeypatch.setattr("team_harness.harness.run", fake_run)

    await _run(
        task="hello",
        task_file=None,
        cwd=str(project_dir),
        api_base="http://localhost:11434/v1",
    )

    no_config_messages = [m for m in printed_messages if "No config file found" in m]
    assert len(no_config_messages) == 1


@pytest.mark.asyncio
async def test_repl_creates_session_output_dir_and_passes_it_to_prompt(
    monkeypatch, tmp_path
):
    class FakeClient:
        api_base = "http://localhost:11434/v1"
        model = "test/model"
        provider = "openai_compat"

        async def aclose(self):
            return None

    class FakeConsole:
        def start(self):
            return None

        def stop(self):
            return None

        def print(self, message):
            return None

        def print_welcome(self, model, cwd, provider):
            return None

        def pause_for_input(self):
            return None

        def resume_after_input(self):
            return None

        def begin_turn(self, n):
            return None

        def begin_streaming(self):
            return None

        def stream_token(self, token):
            return None

        def end_streaming(self):
            return None

        def end_turn(self):
            return None

        def tool_call_start(self, name, args):
            return None

        def agent_event(self, event, state):
            return None

        def context_warning(self):
            return None

        def reset_separator(self):
            return None

        def print_agent_panel_inline(self):
            return None

    async def fake_resolve_model_limit(model_id, client, config):
        return 128_000

    async def fake_read_user_input(session):
        return None

    captured: dict[str, str] = {}
    output_root = tmp_path / "project" / "artifacts"
    project_dir = tmp_path / "project"
    project_dir.mkdir()

    def fake_build_system_prompt(*, config, allowed_types, skills, session_output_dir):
        captured["session_output_dir"] = session_output_dir
        return "system"

    monkeypatch.setattr("team_harness.cli.RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(config_module, "CONFIG_PATH", tmp_path / "home" / "config.toml")
    monkeypatch.setattr("team_harness.cli._make_client", lambda config: FakeClient())
    monkeypatch.setattr(
        "team_harness.cli.resolve_model_limit", fake_resolve_model_limit
    )
    monkeypatch.setattr("team_harness.cli.make_console", lambda **_: FakeConsole())
    monkeypatch.setattr("team_harness.cli.load_skills", lambda cwd=None: [])
    monkeypatch.setattr(
        "team_harness.cli.validate_templates", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(
        "team_harness.cli.build_system_prompt", fake_build_system_prompt
    )
    monkeypatch.setattr("team_harness.cli._build_registry", lambda **_: object())
    monkeypatch.setattr("team_harness.cli.make_prompt_session", lambda: object())
    monkeypatch.setattr("team_harness.cli.read_user_input", fake_read_user_input)

    local_config = project_dir / ".team-harness" / "config.toml"
    local_config.parent.mkdir()
    local_config.write_text('[coordinator]\noutput_dir = "artifacts"\n')

    await _repl(cwd=str(project_dir), api_base="http://localhost:11434/v1")

    session_output_dir = config_module.Path(captured["session_output_dir"])
    assert "session_output_dir" in captured
    assert session_output_dir.parent == output_root
    assert session_output_dir.is_dir()
    manifest = session_output_dir / "worker_sessions.json"
    assert manifest.exists()
    assert json.loads(manifest.read_text())["workers"] == []


@pytest.mark.asyncio
async def test_repl_exception_path_still_writes_worker_manifest(monkeypatch, tmp_path):
    class FakeClient:
        api_base = "http://localhost:11434/v1"
        model = "test/model"
        provider = "openai_compat"

        async def aclose(self):
            return None

    class FakeConsole:
        def start(self):
            return None

        def stop(self):
            return None

        def print(self, message):
            return None

        def print_welcome(self, model, cwd, provider):
            return None

        def pause_for_input(self):
            return None

        def resume_after_input(self):
            return None

        def begin_turn(self, n):
            return None

        def begin_streaming(self):
            return None

        def stream_token(self, token):
            return None

        def end_streaming(self):
            return None

        def end_turn(self):
            return None

        def tool_call_start(self, name, args):
            return None

        def agent_event(self, event, state):
            return None

        def context_warning(self):
            return None

        def reset_separator(self):
            return None

        def print_agent_panel_inline(self):
            return None

    async def fake_resolve_model_limit(model_id, client, config):
        return 128_000

    async def fake_read_user_input(session):
        raise RuntimeError("input failed")

    captured: dict[str, str] = {}
    project_dir = tmp_path / "project"
    project_dir.mkdir()

    def fake_build_system_prompt(*, config, allowed_types, skills, session_output_dir):
        captured["session_output_dir"] = session_output_dir
        return "system"

    monkeypatch.setattr("team_harness.cli.RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(config_module, "CONFIG_PATH", tmp_path / "home" / "config.toml")
    monkeypatch.setattr("team_harness.cli._make_client", lambda config: FakeClient())
    monkeypatch.setattr(
        "team_harness.cli.resolve_model_limit", fake_resolve_model_limit
    )
    monkeypatch.setattr("team_harness.cli.make_console", lambda **_: FakeConsole())
    monkeypatch.setattr("team_harness.cli.load_skills", lambda cwd=None: [])
    monkeypatch.setattr(
        "team_harness.cli.validate_templates", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(
        "team_harness.cli.build_system_prompt", fake_build_system_prompt
    )
    monkeypatch.setattr("team_harness.cli._build_registry", lambda **_: object())
    monkeypatch.setattr("team_harness.cli.make_prompt_session", lambda: object())
    monkeypatch.setattr("team_harness.cli.read_user_input", fake_read_user_input)

    with pytest.raises(RuntimeError, match="input failed"):
        await _repl(cwd=str(project_dir), api_base="http://localhost:11434/v1")

    manifest = (
        config_module.Path(captured["session_output_dir"]) / "worker_sessions.json"
    )
    assert manifest.exists()
    assert json.loads(manifest.read_text())["workers"] == []


def test_run_command_passes_provider_flags(monkeypatch):
    captured = {}

    async def fake_run(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr("team_harness.cli._run", fake_run)
    runner = CliRunner()

    result = runner.invoke(
        main, ["run", "--provider", "codex", "--codex-auth-path", "auth.json", "hello"]
    )

    assert result.exit_code == 0
    assert captured["provider"] == "codex"
    assert captured["codex_auth_path"] == "auth.json"
    assert captured["task"] == "hello"


def test_warn_provider_startup_for_codex_unknown_model():
    messages: list[str] = []

    class _CaptureUI:
        def print(self, msg: str) -> None:
            messages.append(msg)

    _warn_provider_startup(
        Config(provider="codex", model="custom-codex-model"), ui=_CaptureUI()
    )

    combined = "\n".join(messages)
    assert "provider=codex is experimental" in combined
    assert "context tracking may be inaccurate" in combined
