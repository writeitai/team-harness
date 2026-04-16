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
    coordinator_turn_index: int | None = None
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
    session_id: str | None = None
    resume: "WorkerResumeInfo | None" = None


class WorkerResumeInfo(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=False)

    supported: bool
    preferred_mode: str | None


class WorkerSessionInfo(BaseModel):
    """What the worker's session actually is -- enough to identify and resume it."""

    model_config = ConfigDict(arbitrary_types_allowed=False)

    log_path: str
    """Absolute path to the worker's stdout log (the raw session transcript)."""
    provider_session_id: str | None = None
    """Vendor session ID extracted from the worker's output (null until stdout parsing)."""
    provider_session_path: str | None = None
    """Path to the vendor's own session file, e.g. ~/.codex/sessions/*.json (null until post-hoc harvesting)."""


class WorkerSessionRecord(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=False)

    agent_id: str
    agent_type: str
    coordinator_turn_index: int | None = None
    prompt: str
    status: str
    exit_code: int | None = None
    cwd: str
    spawned_at: datetime
    finished_at: datetime | None = None
    stdout_path: str
    stderr_path: str
    session: WorkerSessionInfo
    resume: WorkerResumeInfo


class WorkerSessionsManifest(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=False)

    schema_version: int = 1
    run_id: str
    generated_at: datetime
    session_output_dir: str
    workers: list[WorkerSessionRecord] = Field(default_factory=list)


class RunRecord(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=False)

    run_id: str
    start: datetime
    end: datetime | None = None
    error: str | None = None
    provider: str
    coordinator_model: str
    api_base: str
    turns: list[TurnRecord] = Field(default_factory=list)
    agents: list[AgentRecord] = Field(default_factory=list)
