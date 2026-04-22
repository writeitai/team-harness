from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any
from typing import Literal
import uuid

from team_harness.agents.manager import AgentManager
from team_harness.agents.registry import get_allowed_types
from team_harness.agents.registry import validate_templates
from team_harness.config import Config
from team_harness.config import load_config
from team_harness.config import RUNS_DIR
from team_harness.coordinator.auth import CodexAuthError
from team_harness.coordinator.auth import load_codex_auth
from team_harness.coordinator.client import CoordinatorAPIError
from team_harness.coordinator.client import CoordinatorClient
from team_harness.coordinator.codex_client import CodexCoordinatorClient
from team_harness.coordinator.loop import run
from team_harness.coordinator.protocols import CoordinatorLike
from team_harness.coordinator.system_prompt import build_system_prompt
from team_harness.skills.loader import load_skills
from team_harness.skills.loader import Skill
from team_harness.skills.loader import SkillContext
from team_harness.tools import shell_tools
from team_harness.tools.agent_tools import build_agent_tool_bindings
from team_harness.tools.fs_tools import build_fs_tool_bindings
from team_harness.tools.registry import ToolRegistry
from team_harness.tools.todo_tools import build_todo_tool_bindings
from team_harness.tracking.context import ContextTracker
from team_harness.tracking.context import KNOWN_CODEX_MODELS
from team_harness.tracking.context import resolve_model_limit
from team_harness.tracking.run_log import RunLogWriter
from team_harness.tracking.worker_sessions import write_worker_sessions_manifest
from team_harness.ui.console import ConsoleBase
from team_harness.ui.console import make_console


class TeamHarness:
    """Python SDK entry point for team-harness orchestration runs.

    Wraps the full run lifecycle: config resolution, client creation, tool
    registration, coordinator loop execution, and result extraction.

    All parameters mirror CLI flags and environment variables so that every
    option available on the command line is also reachable from Python.
    """

    def __init__(
        self,
        *,
        provider: str | None = None,
        model: str | None = None,
        api_base: str | None = None,
        api_key: str | None = None,
        codex_auth_path: str | None = None,
        agents: str | list[str] | None = None,
        max_retries: int | None = None,
        max_depth: int | None = None,
        system_prompt: str | None = None,
        system_prompt_file: str | None = None,
        cwd: str | None = None,
        console_mode: Literal["silent", "auto", "plain", "rich"] = "silent",
    ) -> None:
        self._provider = provider
        self._model = model
        self._api_base = api_base
        self._api_key = api_key
        self._codex_auth_path = codex_auth_path
        self._agents = agents
        self._max_retries = max_retries
        self._max_depth = max_depth
        self._system_prompt = system_prompt
        self._system_prompt_file = system_prompt_file
        self._cwd = cwd
        self._console_mode = console_mode

    async def run(self, task: str) -> TeamHarnessResult:
        """Execute a single orchestration run and return structured results.

        Raises TeamHarnessError on terminal failures (API errors, retries
        exhausted, etc.). The run log is always finalized in a finally block.
        """
        allowed_agents_str = _normalize_agents(self._agents)
        config = load_config(
            provider=self._provider,
            model=self._model,
            api_base=self._api_base,
            api_key=self._api_key,
            codex_auth_path=self._codex_auth_path,
            max_retries=self._max_retries,
            max_depth=self._max_depth,
            system_prompt=self._system_prompt,
            cli_system_prompt_file=self._system_prompt_file,
            allowed_agents=allowed_agents_str,
            cwd=self._cwd,
        )
        run_id = _make_run_id()
        run_dir = RUNS_DIR / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        session_output_dir = _prepare_session_output_dir(
            config=config, session_id=run_id
        )
        config.run_dir = run_dir
        manager = AgentManager()
        client = _make_client(config)
        run_log: RunLogWriter | None = None
        ui: ConsoleBase | None = None
        messages: list[dict[str, Any]] = []
        try:
            run_log = RunLogWriter(
                run_id=run_id,
                run_dir=run_dir,
                provider=config.provider,
                model=config.model,
                api_base=client.api_base,
            )
            model_limit = await resolve_model_limit(
                model_id=config.model, client=client, config=config
            )
            ctx = ContextTracker(model_id=config.model, model_limit=model_limit)
            ui = make_console(
                ctx=ctx, manager=manager, run_dir=run_dir, mode=self._console_mode
            )
            _show_no_config_hint(config, ui=ui)
            _warn_provider_startup(config, ui=ui)
            skills = load_skills(cwd=config.cwd)
            allowed_types = get_allowed_types(config)
            validate_templates(config=config, allowed_types=allowed_types)
            skill_ctx = SkillContext(client=client, config=config)
            registry = _build_registry(
                allowed_types=allowed_types,
                skills=skills,
                skill_ctx=skill_ctx,
                manager=manager,
                run_log=run_log,
                config=config,
                ui=ui,
                run_dir=run_dir,
                session_output_dir=str(session_output_dir),
            )
            system_prompt = build_system_prompt(
                config=config,
                allowed_types=allowed_types,
                skills=skills,
                session_output_dir=str(session_output_dir),
            )
            messages: list[dict[str, Any]] = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": task},
            ]
            ui.start()
            await run(
                messages=messages,
                config=config,
                run_log=run_log,
                ui=ui,
                tool_registry=registry,
                client=client,
                ctx=ctx,
            )
        except Exception as exc:
            raise TeamHarnessError(str(exc)) from exc
        finally:
            if run_log is not None:
                await _finalize_run(
                    manager=manager,
                    run_log=run_log,
                    session_output_dir=session_output_dir,
                    shutdown_timeout_s=config.shutdown_timeout_s,
                    ui=ui,
                )
            if ui is not None:
                ui.stop()
            await client.aclose()
        if run_log.error:
            raise TeamHarnessError(run_log.error)
        text = _extract_final_text(messages)
        agent_summaries = _build_agent_summaries(manager)
        return TeamHarnessResult(text=text, agents=agent_summaries, run_id=run_id)


@dataclass
class TeamHarnessResult:
    """Structured output from a completed TeamHarness.run() call."""

    text: str
    agents: list[AgentSummary]
    run_id: str


@dataclass
class AgentSummary:
    """Summary of a spawned agent, without the subprocess handle."""

    id: str
    agent_type: str
    status: str
    exit_code: int | None
    cwd: str


class TeamHarnessError(Exception):
    """Raised when a harness run terminates due to an unrecoverable error."""


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _normalize_agents(agents: str | list[str] | None) -> str | None:
    """Convert agents parameter to the comma-separated string load_config expects."""
    if agents is None:
        return None
    if isinstance(agents, list):
        return ",".join(agents)
    return agents


def _extract_final_text(messages: list[dict[str, Any]]) -> str:
    """Return the content of the last assistant message, or empty string."""
    for message in reversed(messages):
        if message.get("role") == "assistant":
            content = message.get("content")
            if isinstance(content, str):
                return content
    return ""


def _build_agent_summaries(manager: AgentManager) -> list[AgentSummary]:
    """Build a list of AgentSummary from the manager state."""
    return [
        AgentSummary(
            id=state.id,
            agent_type=state.agent_type,
            status=state.status,
            exit_code=state.exit_code,
            cwd=state.cwd,
        )
        for state in manager.list_all()
    ]


def _prepare_session_output_dir(config: Config, session_id: str) -> Path:
    """Resolve and create the per-session artifact directory."""
    output_root = Path(config.output_dir).expanduser()
    if not output_root.is_absolute():
        output_root = Path(config.cwd) / output_root
    session_output_dir = (output_root / session_id).resolve()
    session_output_dir.mkdir(parents=True, exist_ok=True)
    return session_output_dir


def _sync_terminal_agents(manager: AgentManager, run_log: RunLogWriter) -> None:
    manager.poll_exit_codes()
    for state in manager.list_all():
        if state.status == "running":
            continue
        if state.finished_at is None:
            state.finished_at = datetime.now(timezone.utc)
        exit_code = state.exit_code
        if exit_code is None and state.status == "killed":
            exit_code = (
                state.proc.returncode if state.proc.returncode is not None else -1
            )
            state.exit_code = exit_code
        if exit_code is None:
            continue
        run_log.update_agent(
            state.id,
            exit_code=exit_code,
            finished_at=state.finished_at,
            status=state.status,
        )


async def _graceful_shutdown(
    manager: AgentManager,
    run_log: RunLogWriter,
    ui: ConsoleBase | None,
    timeout: float = 10.0,
    terminate_wait: float = 1.0,
) -> None:
    manager.poll_exit_codes()
    running = [state.id for state in manager.list_all() if state.status == "running"]
    if running:
        try:
            await asyncio.wait_for(
                asyncio.gather(*(manager.wait_one(agent_id) for agent_id in running)),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            manager.poll_exit_codes()
            stragglers: list[str] = []
            for agent_id in running:
                state = manager.get(agent_id)
                if state.status != "running" or state.proc.returncode is not None:
                    continue
                try:
                    state.proc.terminate()
                except ProcessLookupError:
                    continue
                state.status = "killed"
                stragglers.append(agent_id)
            if stragglers:
                try:
                    await asyncio.wait_for(
                        asyncio.gather(
                            *(manager.wait_one(agent_id) for agent_id in stragglers)
                        ),
                        timeout=terminate_wait,
                    )
                except asyncio.TimeoutError:
                    pass
                finally:
                    for agent_id in stragglers:
                        state = manager.get(agent_id)
                        state.status = "killed"
                        if state.finished_at is None:
                            state.finished_at = datetime.now(timezone.utc)
                        if ui is not None:
                            ui.agent_event(event="killed", state=state)
    _sync_terminal_agents(manager, run_log)


async def _finalize_run(
    *,
    manager: AgentManager,
    run_log: RunLogWriter,
    session_output_dir: str | Path,
    shutdown_timeout_s: float,
    ui: ConsoleBase | None,
    error: str | None = None,
) -> None:
    await _graceful_shutdown(
        manager=manager, run_log=run_log, ui=ui, timeout=shutdown_timeout_s
    )
    run_log.finalize(error=error)
    write_worker_sessions_manifest(
        run_id=run_log.run_id,
        session_output_dir=session_output_dir,
        agents=run_log.snapshot_agents(),
    )


def _emit_provider_warning(message: str, ui: ConsoleBase | None = None) -> None:
    """Emit a warning message via the console or silently discard it."""
    if ui is None:
        return
    ui.print(message)


def _warn_provider_startup(config: Config, ui: ConsoleBase | None = None) -> None:
    """Warn about provider-specific issues at startup."""
    if config.provider == "openai_compat":
        if config.api_key:
            return
        if config.api_base.startswith("http://localhost") or config.api_base.startswith(
            "http://127.0.0.1"
        ):
            return
        _emit_provider_warning(
            message="WARNING: No API key configured. Set OPENROUTER_API_KEY or OPENAI_API_KEY "
            "or configure api_key in a team-harness config file.",
            ui=ui,
        )
        return
    _emit_provider_warning(message="WARNING: provider=codex is experimental.", ui=ui)
    if config.model not in KNOWN_CODEX_MODELS:
        _emit_provider_warning(
            message=f"WARNING: Codex model {config.model!r} is not in the built-in known "
            "model list; context tracking may be inaccurate.",
            ui=ui,
        )


def _show_no_config_hint(config: Config, ui: ConsoleBase | None = None) -> None:
    """Print a hint when no config file is found."""
    if config.global_config_path is None and config.local_config_path is None:
        if ui is not None:
            ui.print("No config file found. Run `team-harness init` to create one.")


def _make_client(config: Config) -> CoordinatorLike:
    """Create the appropriate coordinator client based on provider."""
    if config.provider == "openai_compat":
        return CoordinatorClient(
            api_base=config.api_base, api_key=config.api_key, model=config.model
        )
    if config.provider == "codex":
        try:
            auth = load_codex_auth(config.codex_auth_path or None, cwd=config.cwd)
        except CodexAuthError as exc:
            raise CoordinatorAPIError(str(exc)) from exc
        # Only pass api_base if the user explicitly overrode it; the default
        # OpenRouter URL is not valid for the Codex provider.
        codex_api_base = config.api_base if config.api_base != Config.api_base else ""
        return CodexCoordinatorClient(
            model=config.model, auth=auth, api_base=codex_api_base
        )
    raise CoordinatorAPIError(f"Unsupported provider: {config.provider}")


def _make_run_id() -> str:
    """Generate a timestamped unique run identifier."""
    return (
        datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        + "_"
        + uuid.uuid4().hex[:8]
    )


def _make_skill_wrapper(skill: Skill, ctx: SkillContext) -> Any:
    """Create an async wrapper that invokes a skill with its context."""

    async def _wrapper(**args: object) -> str:
        return await skill.execute(ctx=ctx, **args)

    return _wrapper


def _build_registry(
    *,
    allowed_types: list[str],
    skills: list[Skill],
    skill_ctx: SkillContext,
    manager: AgentManager,
    run_log: RunLogWriter,
    config: Config,
    ui: ConsoleBase,
    run_dir: Path,
    session_output_dir: str = "",
) -> ToolRegistry:
    """Build a ToolRegistry with per-run tool closures for concurrent safety."""
    registry = ToolRegistry()

    # Agent tools (per-run closures)
    agent_bindings = build_agent_tool_bindings(
        manager=manager,
        run_log=run_log,
        config=config,
        ui=ui,
        allowed_types=allowed_types,
        session_output_dir=session_output_dir,
    )
    for schema, fn in agent_bindings:
        registry.register(schema=schema, fn=fn)

    # File system tools (per-run closures for stateful tools)
    fs_bindings = build_fs_tool_bindings()
    for schema, fn in fs_bindings:
        registry.register(schema=schema, fn=fn)

    # Shell tools (stateless)
    registry.register(schema=shell_tools.BASH_SCHEMA, fn=shell_tools.bash)

    # Todo tools (per-run closures)
    todo_bindings = build_todo_tool_bindings(run_dir=run_dir)
    for schema, fn in todo_bindings:
        registry.register(schema=schema, fn=fn)

    # Skills
    for skill in skills:
        registry.register(
            schema={
                "type": "function",
                "function": {
                    "name": skill.name,
                    "description": skill.description,
                    "parameters": skill.parameters_schema,
                },
            },
            fn=_make_skill_wrapper(skill=skill, ctx=skill_ctx),
        )
    return registry
