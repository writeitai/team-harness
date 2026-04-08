# pyright: reportMissingParameterType=false, reportArgumentType=false

import json

import pytest

from team_harness.cli import _run


@pytest.mark.asyncio
async def test_full_run_mock_api(tmp_path, monkeypatch):
    class FakeClient:
        def __init__(self, api_base, api_key, model):
            self.api_base = api_base

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
                                            arguments='{"tasks":[{"id":"1","description":"x","status":"done"}]}',
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

    monkeypatch.setattr("team_harness.cli.CoordinatorClient", FakeClient)
    monkeypatch.setattr("team_harness.cli.RUNS_DIR", tmp_path)
    monkeypatch.setattr("team_harness.cli.load_skills", lambda: [])
    await _run(
        task="hello",
        task_file=None,
        cwd=str(tmp_path),
        api_base="http://localhost:11434/v1",
    )
    run_json = next(tmp_path.iterdir()) / "run.json"
    data = json.loads(run_json.read_text())
    assert data["turns"]
    assert data["turns"][0]["tool_calls"][0]["name"] == "todo_write"
