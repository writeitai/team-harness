from datetime import datetime
from typing import Any

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
    # Durable process identity (TH-D5): the worker is its own process-group
    # leader (pgid == pid); starttime guards liveness/kill against pid reuse.
    pid: int | None = None
    pgid: int | None = None
    starttime: str | None = None
    # Model/effort audit trail: `requested_*` is what the coordinator passed
    # to spawn_agent (None = it left the choice to the template default);
    # `effective_*` is what was actually injected after resolution. Lets an
    # outer reviewer verify a task ran on the intended model tier without
    # parsing the argv.
    requested_model: str | None = None
    requested_effort: str | None = None
    effective_model: str | None = None
    effective_effort: str | None = None
    # Direct-spawn assignment contract. These fields are additive so records
    # written before caller-contract v1 remain readable.
    assignment_path: str | None = None
    delegated_role: str | None = None
    delegated_task_id: str | None = None
    expected_outputs: list[str] = Field(default_factory=list)
    state_responsibility: str | None = None


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
    outcome: str
    elapsed_seconds: float | None = None
    summary: str | None = None
    stdout_tail: str = ""
    stderr_tail: str = ""
    invocation_path: str | None = None
    exit_code_path: str | None = None
    stdout_tail_path: str | None = None
    stderr_tail_path: str | None = None
    session: WorkerSessionInfo
    resume: WorkerResumeInfo
    # Durable process identity (TH-D5); None for runs recorded before v0.2.11.
    pid: int | None = None
    pgid: int | None = None
    starttime: str | None = None


class WorkerSessionsManifest(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=False)

    schema_version: int = 3
    run_id: str
    generated_at: datetime
    session_output_dir: str
    workers: list[WorkerSessionRecord] = Field(default_factory=list)


class CoordinatorRetryRecord(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=False)

    attempt: int
    max_retries: int
    will_retry: bool
    sleep_seconds: float | None = None
    provider: str
    model: str
    api_base: str
    host: str | None = None
    error_type: str
    cause_type: str | None = None
    status_code: int | None = None
    retryable: bool
    message: str
    recorded_at: datetime


class RunFailureRecord(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=False)

    kind: str
    message: str
    provider: str
    model: str
    api_base: str
    host: str | None = None
    error_type: str
    cause_type: str | None = None
    status_code: int | None = None
    retryable: bool | None = None
    retry_attempts: int = 0
    max_retries: int | None = None


class RateLimitedFamilyRecord(BaseModel):
    """One hard worker-provider rate-limit interval observed during a run."""

    model_config = ConfigDict(arbitrary_types_allowed=False)

    family: str
    model: str | None = None
    tripped_at: datetime
    resets_at: datetime
    reason: str


class RunRecord(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=False)

    run_id: str
    start: datetime
    end: datetime | None = None
    error: str | None = None
    failure: RunFailureRecord | None = None
    provider: str
    coordinator_model: str
    api_base: str
    # Recorded at run start so a post-crash reap can refresh the
    # worker_sessions.json manifest in the right place (TH-D5).
    session_output_dir: str | None = None
    # Identity of the process that owns this run, recorded at run start.
    # reap_run refuses to touch a run whose parent is still alive (verified
    # by pid + starttime) unless forced — guarding live runs from being reaped.
    parent_pid: int | None = None
    parent_starttime: str | None = None
    caller_context: dict[str, Any] | None = None
    capabilities: list[str] = Field(default_factory=list)
    coordinator_input_path: str | None = None
    coordinator_retries: list[CoordinatorRetryRecord] = Field(default_factory=list)
    # Additive audit history. Expired entries remain queryable; resets_at makes
    # the interval boundary explicit without changing any existing run fields.
    rate_limited_families: list[RateLimitedFamilyRecord] = Field(default_factory=list)
    turns: list[TurnRecord] = Field(default_factory=list)
    agents: list[AgentRecord] = Field(default_factory=list)
