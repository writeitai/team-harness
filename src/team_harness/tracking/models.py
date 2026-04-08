from datetime import datetime

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field


class ToolCallRecord(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=False)

    name: str
    arguments: dict
    result: str
    is_error: bool = False


class TurnRecord(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=False)

    index: int
    messages_appended_delta: list[dict]
    response_text: str | None
    usage: dict
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)


class AgentRecord(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=False)

    id: str
    agent_type: str
    status: str = "running"
    cwd: str
    prompt: str
    full_prompt: str
    command: list[str]
    spawned_at: datetime
    finished_at: datetime | None = None
    exit_code: int | None = None
    stdout_log: str
    stderr_log: str


class RunRecord(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=False)

    run_id: str
    start: datetime
    end: datetime | None = None
    error: str | None = None
    coordinator_model: str
    api_base: str
    turns: list[TurnRecord] = Field(default_factory=list)
    agents: list[AgentRecord] = Field(default_factory=list)
