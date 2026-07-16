from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import field_validator

CALLER_CONTRACT_VERSION = 1
CALLER_RUN_RECORD_CAPABILITY = "caller_run_record_v1"
COORDINATOR_INPUT_CAPABILITY = "coordinator_input_v1"
SPAWN_ASSIGNMENT_CAPABILITY = "spawn_assignment_v1"
NESTED_CALLER_CONTEXT_CAPABILITY = "nested_caller_context_v1"
INHERITED_CALLER_CONTEXT_ENV = "TEAM_HARNESS_CALLER_CONTEXT"
TEAM_HARNESS_CAPABILITIES = frozenset(
    {
        CALLER_RUN_RECORD_CAPABILITY,
        COORDINATOR_INPUT_CAPABILITY,
        SPAWN_ASSIGNMENT_CAPABILITY,
        NESTED_CALLER_CONTEXT_CAPABILITY,
    }
)


@dataclass(frozen=True)
class HarnessCapabilities:
    """Public, versioned feature declaration for embedding callers.

    Capability names are the compatibility boundary. Callers should check a
    required name rather than infer support from the package version or from a
    constructor signature.
    """

    caller_contract_version: int
    capabilities: frozenset[str]

    def supports(self, *names: str) -> bool:
        """Return whether every requested named capability is available."""

        return all(name in self.capabilities for name in names)


def get_capabilities() -> HarnessCapabilities:
    """Return the immutable caller-contract capabilities of this build."""

    return HarnessCapabilities(
        caller_contract_version=CALLER_CONTRACT_VERSION,
        capabilities=TEAM_HARNESS_CAPABILITIES,
    )


class CallerContext(BaseModel):
    """Identity and caller-owned paths for one embedded harness run.

    ``trace_root`` is a caller-owned absolute directory. Team-harness creates
    one run-id child beneath it and keeps the canonical ``run.json``, generated
    coordinator input, direct-agent assignments, and worker artifacts together
    in that child. The explicit resulting paths are returned to the caller.
    """

    model_config = ConfigDict(
        arbitrary_types_allowed=False, extra="forbid", frozen=True
    )

    schema_version: Literal[1] = 1
    trace_root: Path
    parent_assignment_path: Path
    parent_attempt_id: str
    root_session_id: str
    session_id: str
    session_depth: int
    workflow_role: str
    relevant_state_paths: tuple[Path, ...] = ()
    parent_harness_run_id: str | None = None

    @field_validator("trace_root", "parent_assignment_path")
    @classmethod
    def _require_absolute_path(cls, value: Path) -> Path:
        """Normalize a required caller-owned path and reject relative input."""

        path = value.expanduser()
        if not path.is_absolute():
            raise ValueError("caller contract paths must be absolute")
        return path.resolve()

    @field_validator("relevant_state_paths")
    @classmethod
    def _require_absolute_state_paths(
        cls, values: tuple[Path, ...]
    ) -> tuple[Path, ...]:
        """Normalize every declared state path and reject relative entries."""

        resolved: list[Path] = []
        for value in values:
            path = value.expanduser()
            if not path.is_absolute():
                raise ValueError("relevant_state_paths must contain absolute paths")
            resolved.append(path.resolve())
        return tuple(resolved)

    @field_validator(
        "parent_attempt_id",
        "root_session_id",
        "session_id",
        "workflow_role",
        "parent_harness_run_id",
    )
    @classmethod
    def _require_identity(cls, value: str | None) -> str | None:
        """Reject blank identity values while preserving optional absence."""

        if value is None:
            return None
        if not value.strip():
            raise ValueError("caller context identity fields must not be blank")
        return value

    @field_validator("session_depth")
    @classmethod
    def _require_non_negative_depth(cls, value: int) -> int:
        """Require session depth to describe a real, non-negative layer."""

        if value < 0:
            raise ValueError("session_depth must be greater than or equal to zero")
        return value


def inherited_caller_context() -> CallerContext | None:
    """Load the context propagated to a nested ``type=harness`` process."""

    payload = os.environ.get(INHERITED_CALLER_CONTEXT_ENV)
    if payload is None:
        return None
    return CallerContext.model_validate_json(json_data=payload)


def build_nested_caller_context(
    *,
    context: CallerContext,
    parent_harness_run_id: str,
    agent_assignment_path: Path,
    agent_output_dir: Path,
) -> CallerContext:
    """Derive identity for a nested harness without inventing a loop layer."""

    return CallerContext(
        trace_root=(agent_output_dir / "harness_runs").resolve(),
        parent_assignment_path=agent_assignment_path.resolve(),
        parent_attempt_id=context.parent_attempt_id,
        root_session_id=context.root_session_id,
        session_id=context.session_id,
        session_depth=context.session_depth,
        workflow_role=context.workflow_role,
        relevant_state_paths=context.relevant_state_paths,
        parent_harness_run_id=parent_harness_run_id,
    )


def build_coordinator_context_footer(
    *, context: CallerContext, harness_run_id: str, harness_run_dir: Path
) -> str:
    """Render automatic ecosystem context for the harness coordinator."""

    state_paths = (
        "\n".join(f"- {path}" for path in context.relevant_state_paths)
        if context.relevant_state_paths
        else "- (none declared; consult the parent assignment)"
    )
    coordinator_role = (
        "You are a delegated nested harness coordinator. You own orchestration and "
        "integration for this delegated assignment, while the parent harness "
        "coordinator retains the loop-layer decision."
        if context.parent_harness_run_id is not None
        else "You are the harness coordinator for one workflow assignment inside a "
        "larger session tree. You own orchestration and integration for this "
        "assignment."
    )
    parent_run_line = (
        f"- Parent harness run id: {context.parent_harness_run_id}\n"
        if context.parent_harness_run_id is not None
        else ""
    )
    return f"""# Embedded caller assignment context

{coordinator_role} Agents you spawn are ephemeral delegates; they do not
independently own the loop-layer decision.

- Parent assignment (absolute): {context.parent_assignment_path}
- Parent attempt id: {context.parent_attempt_id}
{parent_run_line}- Harness run id: {harness_run_id}
- Root session id: {context.root_session_id}
- Current session id: {context.session_id}
- Current session depth: {context.session_depth}
- Workflow role: {context.workflow_role}
- Harness run directory (absolute): {harness_run_dir}

Read the parent assignment before delegating. Use its absolute state paths; do
not infer loop-layer state from the current working directory. Team-harness
automatically writes an assignment envelope for every direct spawn, but you
must supply useful delegated_role, delegated_task_id, expected_outputs, and
state_responsibility metadata when calling spawn_agent.

Relevant state paths declared by the caller:
{state_paths}"""
