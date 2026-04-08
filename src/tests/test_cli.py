# pyright: reportMissingParameterType=false

from click.testing import CliRunner
import pytest

from team_harness import config as config_module
from team_harness.cli import _run
from team_harness.cli import main


def test_logs_empty(monkeypatch, tmp_path):
    monkeypatch.setattr("team_harness.cli.RUNS_DIR", tmp_path)
    runner = CliRunner()

    result = runner.invoke(main, ["logs"])

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
async def test_run_without_config_prints_no_config_hint(monkeypatch, tmp_path, capsys):
    class FakeClient:
        def __init__(self, api_base, api_key, model):
            self.api_base = api_base

    class FakeConsole:
        def start(self):
            return None

        def stop(self):
            return None

        def print(self, message):
            return None

    async def fake_resolve_model_limit(model, client, config):
        return 128_000

    async def fake_run(messages, config, run_log, ui, registry, client, ctx):
        return None

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.setattr(config_module, "CONFIG_PATH", tmp_path / "home" / "config.toml")
    monkeypatch.setattr("team_harness.cli.RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr("team_harness.cli.CoordinatorClient", FakeClient)
    monkeypatch.setattr(
        "team_harness.cli.resolve_model_limit", fake_resolve_model_limit
    )
    monkeypatch.setattr("team_harness.cli.make_console", lambda **_: FakeConsole())
    monkeypatch.setattr("team_harness.cli.load_skills", lambda cwd=None: [])
    monkeypatch.setattr("team_harness.cli.validate_templates", lambda *args: None)
    monkeypatch.setattr("team_harness.cli.build_system_prompt", lambda *args: "system")
    monkeypatch.setattr("team_harness.cli.run", fake_run)

    await _run(
        task="hello",
        task_file=None,
        cwd=str(project_dir),
        api_base="http://localhost:11434/v1",
    )

    captured = capsys.readouterr().out
    assert (
        captured.count("No config file found. Run `team-harness init` to create one.")
        == 1
    )
