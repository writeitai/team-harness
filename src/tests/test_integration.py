# pyright: reportMissingParameterType=false, reportArgumentType=false

import json

import pytest

from team_harness import config as config_module
from team_harness.cli import _run


@pytest.mark.asyncio
async def test_full_run_mock_api(tmp_path, monkeypatch):
    class FakeClient:
        def __init__(self, api_base, api_key, model):
            self.api_base = api_base
            self.model = model
            self.provider = "openai_compat"

        async def chat(self, messages, tools=None, stream=False, token_callback=None):
            from team_harness.coordinator.client import ChatResponse
            from team_harness.coordinator.client import ChoiceRecord
            from team_harness.coordinator.client import FunctionRecord
            from team_harness.coordinator.client import MessageRecord
            from team_harness.coordinator.client import ToolCallRecord
            from team_harness.coordinator.client import UsageRecord

            if len(messages) == 2:
                return ChatResponse(
                    choices=[
                        ChoiceRecord(
                            message=MessageRecord(
                                content="working",
                                tool_calls=[
                                    ToolCallRecord(
                                        id="1",
                                        function=FunctionRecord(
                                            name="todo_write",
                                            arguments='{"tasks":[{"id":"1","description":"x","status":"completed"}]}',
                                        ),
                                    )
                                ],
                            ),
                            finish_reason="stop",
                        )
                    ],
                    usage=UsageRecord(prompt_tokens=1, completion_tokens=1),
                )
            return ChatResponse(
                choices=[ChoiceRecord(message=MessageRecord(content="done"))],
                usage=UsageRecord(prompt_tokens=1, completion_tokens=1),
            )

        async def get_models(self):
            return {"data": []}

        async def aclose(self):
            return None

    monkeypatch.setattr(
        "team_harness.harness._make_client",
        lambda config: FakeClient(
            api_base=config.api_base, api_key=config.api_key, model=config.model
        ),
    )
    monkeypatch.setattr("team_harness.harness.RUNS_DIR", tmp_path)
    monkeypatch.setattr(
        config_module,
        "CONFIG_PATH",
        tmp_path / "home" / ".team-harness" / "config.toml",
    )
    captured: dict[str, str | None] = {"cwd": None}

    def fake_load_skill_metadata(*, cwd=None):
        captured["cwd"] = str(cwd) if cwd is not None else None
        return []

    other_cwd = tmp_path / "elsewhere"
    other_cwd.mkdir()
    monkeypatch.chdir(other_cwd)
    monkeypatch.setattr(
        "team_harness.harness.load_skill_metadata", fake_load_skill_metadata
    )
    await _run(
        task="hello",
        task_file=None,
        cwd=str(tmp_path),
        api_base="http://localhost:11434/v1",
    )
    run_json = next(
        path / "run.json" for path in tmp_path.iterdir() if (path / "run.json").exists()
    )
    data = json.loads(run_json.read_text())
    assert data["turns"]
    assert data["turns"][0]["tool_calls"][0]["name"] == "todo_write"
    assert captured["cwd"] == str(tmp_path.resolve())
