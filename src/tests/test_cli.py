# pyright: reportMissingParameterType=false

from click.testing import CliRunner

from team_harness.cli import main


def test_logs_empty(monkeypatch, tmp_path):
    monkeypatch.setattr("team_harness.cli.RUNS_DIR", tmp_path)
    runner = CliRunner()
    result = runner.invoke(main, ["logs"])
    assert result.exit_code == 0
    assert "No runs yet." in result.output
