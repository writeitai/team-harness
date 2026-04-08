from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
import json
from typing import Any
import uuid

from team_harness.agents.manager import AgentManager
from team_harness.agents.registry import get_allowed_types
from team_harness.agents.registry import validate_templates
from team_harness.config import Config
from team_harness.config import load_config
from team_harness.config import RUNS_DIR
from team_harness.coordinator.client import CoordinatorClient
from team_harness.coordinator.loop import run
from team_harness.coordinator.system_prompt import build_system_prompt
from team_harness.skills.loader import load_skills
from team_harness.skills.loader import Skill
from team_harness.skills.loader import SkillContext
from team_harness.tools import agent_tools
from team_harness.tools import shell_tools
from team_harness.tools import todo_tools
from team_harness.tools.agent_tools import AGENT_TOOL_SCHEMAS
from team_harness.tools.agent_tools import build_agent_tool_bindings
from team_harness.tools.agent_tools import spawn_agent_schema
from team_harness.tools.fs_tools import build_fs_tool_bindings
from team_harness.tools.fs_tools import FS_TOOL_SCHEMAS
from team_harness.tools.registry import ToolRegistry
from team_harness.tools.todo_tools import build_todo_tool_bindings
from team_harness.tracking.context import ContextTracker
from team_harness.tracking.context import resolve_model_limit
from team_harness.tracking.run_log import RunLogWriter
from team_harness.ui.console import ConsoleBase
from team_harness.ui.console import make_console


@dataclass
class AgentSummary:
    id: str
    agent_type: str
    cwd: str
    status: str
    exit_code: int | None
    prompt: str
    spawned_at: datetime
    finished_at: datetime | None


@dataclass
class HarnessResult:
    text: str
    agents: list[AgentSummary]
    run_id: str


class HarnessError(RuntimeError):
    pass


def _make_run_id() -> str:
    return (
        datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        + "_"
        + uuid.uuid4().hex[:8]
    )


def _make_skill_wrapper(skill: Skill, ctx: SkillContext):
    async def _wrapper(**args: object) -> str:
        return await skill.execute(ctx=ctx, **args)

    return _wrapper


def _build_registry(
    allowed_types: list[str],
    skills: list[Skill],
    skill_ctx: SkillContext,
    *,
    agent_bindings: list[tuple[dict, Any]] | None = None,
    fs_bindings: list[tuple[dict, Any]] | None = None,
    todo_bindings: list[tuple[dict, Any]] | None = None,
) -> ToolRegistry:
    registry = ToolRegistry()
    for schema, fn in (
        agent_bindings
        if agent_bindings is not None
        else [(spawn_agent_schema(allowed_types), agent_tools.spawn_agent)]
        + AGENT_TOOL_SCHEMAS
    ):
        registry.register(schema, fn)
    for schema, fn in fs_bindings if fs_bindings is not None else FS_TOOL_SCHEMAS:
        registry.register(schema, fn)
    registry.register(shell_tools.BASH_SCHEMA, shell_tools.bash)
    for schema, fn in (
        todo_bindings
        if todo_bindings is not None
        else [
            (todo_tools.TODO_WRITE_SCHEMA, todo_tools.todo_write),
            (todo_tools.TODO_READ_SCHEMA, todo_tools.todo_read),
        ]
    ):
        registry.register(schema, fn)
    for skill in skills:
        registry.register(
            {
                "type": "function",
                "function": {
                    "name": skill.name,
                    "description": skill.description,
                    "parameters": skill.parameters_schema,
                },
            },
            _make_skill_wrapper(skill, skill_ctx),
        )
    return registry


def _warn_missing_api_key(config: Config, ui: ConsoleBase) -> None:
    if config.api_key:
        return
    if config.api_base.startswith("http://localhost") or config.api_base.startswith(
        "http://127.0.0.1"
    ):
        return
    ui.print(
        "WARNING: No API key configured. Set OPENROUTER_API_KEY env var or "
        "configure api_key in a team-harness config file."
    )


def _show_no_config_hint(config: Config, ui: ConsoleBase) -> None:
    if config.global_config_path is None and config.local_config_path is None:
        ui.print("No config file found. Run `team-harness init` to create one.")


def _extract_final_text(messages: list[dict[str, Any]]) -> str:
    for message in reversed(messages):
        if message.get("role") != "assistant":
            continue
        if "tool_calls" in message:
            continue
        content = message.get("content")
        if content:
            return str(content)
    return ""


def _to_agent_summaries(manager: AgentManager) -> list[AgentSummary]:
    manager.poll_exit_codes()
    return [
        AgentSummary(
            id=state.id,
            agent_type=state.agent_type,
            cwd=state.cwd,
            status=state.status,
            exit_code=state.exit_code,
            prompt=state.prompt,
            spawned_at=state.spawn_time,
            finished_at=state.finished_at,
        )
        for state in manager.list_all()
    ]


class Harness:
    def __init__(
        self,
        *,
        model: str | None = None,
        api_base: str | None = None,
        api_key: str | None = None,
        agents: str | list[str] | None = None,
        max_turns: int | None = None,
        max_retries: int | None = None,
        max_depth: int | None = None,
        system_prompt: str | None = None,
        system_prompt_file: str | None = None,
        cwd: str | None = None,
        console_mode: str = "silent",
    ) -> None:
        self._model = model
        self._api_base = api_base
        self._api_key = api_key
        self._agents = agents
        self._max_turns = max_turns
        self._max_retries = max_retries
        self._max_depth = max_depth
        self._system_prompt = system_prompt
        self._system_prompt_file = system_prompt_file
        self._cwd = cwd
        self._console_mode = console_mode

    async def run(self, task: str) -> HarnessResult:
        allowed_agents = self._normalize_allowed_agents(self._agents)
        config = load_config(
            model=self._model,
            api_base=self._api_base,
            api_key=self._api_key,
            max_turns=self._max_turns,
            max_retries=self._max_retries,
            max_depth=self._max_depth,
            system_prompt=self._system_prompt,
            system_prompt_file=self._system_prompt_file,
            allowed_agents=allowed_agents,
            cwd=self._cwd,
        )

        run_id = _make_run_id()
        run_dir = RUNS_DIR / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        config.run_dir = run_dir

        run_log = RunLogWriter(run_id, run_dir, config.model, config.api_base)
        manager = AgentManager()
        client = CoordinatorClient(config.api_base, config.api_key, config.model)
        model_limit = await resolve_model_limit(config.model, client, config)
        ctx = ContextTracker(model_id=config.model, model_limit=model_limit)
        ui = make_console(
            ctx=ctx, manager=manager, run_dir=run_dir, mode=self._console_mode
        )

        _show_no_config_hint(config, ui)
        _warn_missing_api_key(config, ui)

        skills = load_skills(cwd=config.cwd)
        allowed_types = get_allowed_types(config)
        validate_templates(config, allowed_types)
        skill_ctx = SkillContext(client=client, config=config)
        registry = _build_registry(
            allowed_types,
            skills,
            skill_ctx,
            agent_bindings=build_agent_tool_bindings(
                manager, run_log, config, ui, allowed_types
            ),
            fs_bindings=build_fs_tool_bindings(),
            todo_bindings=build_todo_tool_bindings(run_dir),
        )
        messages = [
            {
                "role": "system",
                "content": build_system_prompt(config, allowed_types, skills),
            },
            {"role": "user", "content": task},
        ]

        run_error: Exception | None = None
        ui.start()
        try:
            try:
                await run(messages, config, run_log, ui, registry, client, ctx)
            except HarnessError:
                raise
            except Exception as exc:
                raise HarnessError(str(exc)) from exc
            log_data = json.loads(run_log.path.read_text())
            error = log_data.get("error")
            if error:
                raise HarnessError(str(error))
            return HarnessResult(
                text=_extract_final_text(messages),
                agents=_to_agent_summaries(manager),
                run_id=run_id,
            )
        except Exception as exc:
            run_error = exc
            raise
        finally:
            run_log.finalize(error=str(run_error) if run_error is not None else None)
            ui.stop()

    @staticmethod
    def _normalize_allowed_agents(agents: str | list[str] | None) -> str | None:
        if agents is None:
            return None
        if isinstance(agents, str):
            return agents
        return ",".join(agent for agent in agents if agent)
