import asyncio
from datetime import datetime
from datetime import timezone
import json
from pathlib import Path
from typing import Any
import uuid

import click

from team_harness.agents.manager import AgentManager
from team_harness.agents.registry import get_allowed_types
from team_harness.agents.registry import validate_templates
from team_harness.config import _default_config_text
from team_harness.config import _local_config_text
from team_harness.config import Config
from team_harness.config import CONFIG_PATH
from team_harness.config import load_config
from team_harness.config import LOCAL_CONFIG_DIR_NAME
from team_harness.config import RUNS_DIR
from team_harness.coordinator.auth import CodexAuthError
from team_harness.coordinator.auth import load_codex_auth
from team_harness.coordinator.client import CoordinatorAPIError
from team_harness.coordinator.client import CoordinatorClient
from team_harness.coordinator.codex_client import CodexCoordinatorClient
from team_harness.coordinator.loop import run
from team_harness.coordinator.loop import run_one_turn
from team_harness.coordinator.protocols import CoordinatorLike
from team_harness.coordinator.system_prompt import build_system_prompt
from team_harness.skills.loader import load_skills
from team_harness.skills.loader import Skill
from team_harness.skills.loader import SkillContext
from team_harness.tools import agent_tools
from team_harness.tools import fs_tools
from team_harness.tools import shell_tools
from team_harness.tools import todo_tools
from team_harness.tools.agent_tools import AGENT_TOOL_SCHEMAS
from team_harness.tools.agent_tools import spawn_agent_schema
from team_harness.tools.fs_tools import FS_TOOL_SCHEMAS
from team_harness.tools.registry import ToolRegistry
from team_harness.tracking.context import ContextTracker
from team_harness.tracking.context import KNOWN_CODEX_MODELS
from team_harness.tracking.context import resolve_model_limit
from team_harness.tracking.run_log import RunLogWriter
from team_harness.ui.console import ConsoleBase
from team_harness.ui.console import make_console
from team_harness.ui.prompt import make_prompt_session
from team_harness.ui.prompt import read_user_input


@click.group()
def main() -> None:
    """th — multi-agent AI orchestration harness."""


def _write_config_file(path: Path, text: str, force: bool) -> None:
    if path.exists() and not force:
        raise click.ClickException(
            f"Config file already exists at {path}. Use --force to overwrite."
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    click.echo(f"Created config at {path}")


def _show_no_config_hint(config: Config) -> None:
    if config.global_config_path is None and config.local_config_path is None:
        click.echo("No config file found. Run `team-harness init` to create one.")


@main.command()
@click.option("--global", "use_global", is_flag=True, help="Create global config.")
@click.option("--force", is_flag=True, help="Overwrite an existing config file.")
def init(use_global: bool, force: bool) -> None:
    path = (
        CONFIG_PATH
        if use_global
        else Path.cwd() / LOCAL_CONFIG_DIR_NAME / "config.toml"
    )
    text = _default_config_text() if use_global else _local_config_text()
    _write_config_file(path, text, force)


@main.command(name="run")
@click.argument("task", required=False)
@click.option("--file", "-f", "task_file", type=click.Path())
@click.option("--provider", default=None)
@click.option("--model", default=None)
@click.option("--api-base", default=None)
@click.option("--api-key", default=None)
@click.option("--codex-auth-path", default=None)
@click.option("--agents", "allowed_agents", default=None)
@click.option("--max-turns", type=int, default=None)
@click.option("--max-retries", type=int, default=None)
@click.option("--max-depth", type=int, default=None)
@click.option("--system-prompt", default=None)
@click.option("--system-prompt-file", default=None)
@click.option("--cwd", default=".")
def run_cli(
    task: str | None,
    task_file: str | None,
    provider: str | None,
    model: str | None,
    api_base: str | None,
    api_key: str | None,
    codex_auth_path: str | None,
    allowed_agents: str | None,
    max_turns: int | None,
    max_retries: int | None,
    max_depth: int | None,
    system_prompt: str | None,
    system_prompt_file: str | None,
    cwd: str,
) -> None:
    asyncio.run(
        _run(
            task=task,
            task_file=task_file,
            provider=provider,
            model=model,
            api_base=api_base,
            api_key=api_key,
            codex_auth_path=codex_auth_path,
            allowed_agents=allowed_agents,
            max_turns=max_turns,
            max_retries=max_retries,
            max_depth=max_depth,
            system_prompt=system_prompt,
            system_prompt_file=system_prompt_file,
            cwd=cwd,
        )
    )


@main.command()
@click.option("--provider", default=None)
@click.option("--model", default=None)
@click.option("--api-base", default=None)
@click.option("--api-key", default=None)
@click.option("--codex-auth-path", default=None)
@click.option("--agents", "allowed_agents", default=None)
@click.option("--max-turns", type=int, default=None)
@click.option("--max-retries", type=int, default=None)
@click.option("--max-depth", type=int, default=None)
@click.option("--system-prompt", default=None)
@click.option("--system-prompt-file", default=None)
@click.option("--cwd", default=".")
def repl(
    provider: str | None,
    model: str | None,
    api_base: str | None,
    api_key: str | None,
    codex_auth_path: str | None,
    allowed_agents: str | None,
    max_turns: int | None,
    max_retries: int | None,
    max_depth: int | None,
    system_prompt: str | None,
    system_prompt_file: str | None,
    cwd: str,
) -> None:
    asyncio.run(
        _repl(
            provider=provider,
            model=model,
            api_base=api_base,
            api_key=api_key,
            codex_auth_path=codex_auth_path,
            allowed_agents=allowed_agents,
            max_turns=max_turns,
            max_retries=max_retries,
            max_depth=max_depth,
            system_prompt=system_prompt,
            system_prompt_file=system_prompt_file,
            cwd=cwd,
        )
    )


@main.command()
@click.argument("run_id", required=False)
def logs(run_id: str | None) -> None:
    if run_id:
        run_json = RUNS_DIR / run_id / "run.json"
        if not run_json.exists():
            click.echo(f"ERROR: Run '{run_id}' not found at {run_json}", err=True)
            raise SystemExit(1)
        click.echo(json.dumps(json.loads(run_json.read_text()), indent=2))
        return
    if not RUNS_DIR.exists():
        click.echo("No runs yet.")
        return
    runs = sorted(RUNS_DIR.iterdir(), reverse=True)[:20]
    printed = False
    for run_dir in runs:
        run_json = run_dir / "run.json"
        if not run_json.exists():
            continue
        data = json.loads(run_json.read_text())
        click.echo(
            f"{run_dir.name}  start={str(data.get('start', '?'))[:19]}  "
            f"model={data.get('coordinator_model', '?')}  "
            f"turns={len(data.get('turns', []))}  agents={len(data.get('agents', []))}"
        )
        printed = True
    if not printed:
        click.echo("No runs yet.")


def _emit_provider_warning(message: str, ui: ConsoleBase | None = None) -> None:
    if ui is None:
        click.echo(message)
        return
    ui.print(message)


def _warn_provider_startup(config: Config, ui: ConsoleBase | None = None) -> None:
    if config.provider == "openai_compat":
        if config.api_key:
            return
        if config.api_base.startswith("http://localhost") or config.api_base.startswith(
            "http://127.0.0.1"
        ):
            return
        _emit_provider_warning(
            "WARNING: No API key configured. Set OPENROUTER_API_KEY or OPENAI_API_KEY "
            "or configure api_key in a team-harness config file.",
            ui,
        )
        return
    _emit_provider_warning("WARNING: provider=codex is experimental.", ui)
    if config.model not in KNOWN_CODEX_MODELS:
        _emit_provider_warning(
            f"WARNING: Codex model {config.model!r} is not in the built-in known "
            "model list; context tracking may be inaccurate.",
            ui,
        )


def _make_client(config: Config) -> CoordinatorLike:
    if config.provider == "openai_compat":
        return CoordinatorClient(config.api_base, config.api_key, config.model)
    if config.provider == "codex":
        try:
            auth = load_codex_auth(config.codex_auth_path or None, cwd=config.cwd)
        except CodexAuthError as exc:
            raise CoordinatorAPIError(str(exc)) from exc
        return CodexCoordinatorClient(config.model, auth, api_base=config.api_base)
    raise CoordinatorAPIError(f"Unsupported provider: {config.provider}")


def _make_skill_wrapper(skill: Skill, ctx: SkillContext):
    async def _wrapper(**args: object) -> str:
        return await skill.execute(ctx=ctx, **args)

    return _wrapper


def _build_registry(
    allowed_types: list[str], skills: list[Skill], skill_ctx: SkillContext
) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(spawn_agent_schema(allowed_types), agent_tools.spawn_agent)
    for schema, fn in AGENT_TOOL_SCHEMAS:
        registry.register(schema, fn)
    for schema, fn in FS_TOOL_SCHEMAS:
        registry.register(schema, fn)
    registry.register(shell_tools.BASH_SCHEMA, shell_tools.bash)
    registry.register(todo_tools.TODO_WRITE_SCHEMA, todo_tools.todo_write)
    registry.register(todo_tools.TODO_READ_SCHEMA, todo_tools.todo_read)
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


def _prepare_task(task: str | None, task_file: str | None) -> str:
    if task and task_file:
        raise click.UsageError("Provide either TASK or --file, not both.")
    if not task and not task_file:
        raise click.UsageError("Provide TASK or --file.")
    if task_file:
        return Path(task_file).read_text()
    assert task is not None
    return task


def _make_run_id() -> str:
    return (
        datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        + "_"
        + uuid.uuid4().hex[:8]
    )


async def _run(task: str | None, task_file: str | None, **kwargs: Any) -> None:
    resolved_task = _prepare_task(task, task_file)
    config = load_config(**kwargs)
    _show_no_config_hint(config)
    run_id = _make_run_id()
    run_dir = RUNS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    config.run_dir = run_dir
    manager = AgentManager()
    client = _make_client(config)
    try:
        run_log = RunLogWriter(
            run_id, run_dir, config.provider, config.model, client.api_base
        )
        model_limit = await resolve_model_limit(config.model, client, config)
        ctx = ContextTracker(model_id=config.model, model_limit=model_limit)
        ui = make_console(ctx=ctx, manager=manager, run_dir=run_dir)
        _warn_provider_startup(config, ui)
        skills = load_skills(cwd=config.cwd)
        allowed_types = get_allowed_types(config)
        validate_templates(config, allowed_types)
        agent_tools.setup(manager, run_log, config, ui)
        todo_tools.setup(run_dir)
        fs_tools.setup_fs()
        skill_ctx = SkillContext(client=client, config=config)
        registry = _build_registry(allowed_types, skills, skill_ctx)
        system_prompt = build_system_prompt(config, allowed_types, skills)
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": resolved_task},
        ]
        ui.start()
        try:
            await run(messages, config, run_log, ui, registry, client, ctx)
        finally:
            run_log.finalize()
            ui.stop()
    finally:
        await client.aclose()


async def _graceful_shutdown(
    manager: AgentManager, run_log: RunLogWriter, ui: ConsoleBase, timeout: float = 10.0
) -> None:
    running = [state.id for state in manager.list_all() if state.status == "running"]
    if not running:
        return
    try:
        await asyncio.wait_for(
            asyncio.gather(*(manager.wait_one(agent_id) for agent_id in running)),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        for agent_id in running:
            state = manager.get(agent_id)
            if state.proc.returncode is None:
                try:
                    state.proc.terminate()
                    state.status = "killed"
                    state.finished_at = datetime.now(timezone.utc)
                except ProcessLookupError:
                    pass
                else:
                    run_log.update_agent(
                        agent_id,
                        exit_code=-1,
                        finished_at=state.finished_at,
                        status="killed",
                    )
                    ui.agent_event("killed", state)


async def _repl(**kwargs: Any) -> None:
    config = load_config(**kwargs)
    _show_no_config_hint(config)
    run_id = _make_run_id()
    run_dir = RUNS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    config.run_dir = run_dir
    manager = AgentManager()
    client = _make_client(config)
    try:
        run_log = RunLogWriter(
            run_id, run_dir, config.provider, config.model, client.api_base
        )
        model_limit = await resolve_model_limit(config.model, client, config)
        ctx = ContextTracker(model_id=config.model, model_limit=model_limit)
        ui = make_console(ctx=ctx, manager=manager, run_dir=run_dir)
        _warn_provider_startup(config, ui)
        skills = load_skills(cwd=config.cwd)
        allowed_types = get_allowed_types(config)
        validate_templates(config, allowed_types)
        agent_tools.setup(manager, run_log, config, ui)
        todo_tools.setup(run_dir)
        fs_tools.setup_fs()
        skill_ctx = SkillContext(client=client, config=config)
        registry = _build_registry(allowed_types, skills, skill_ctx)
        system_prompt = build_system_prompt(config, allowed_types, skills)
        messages = [{"role": "system", "content": system_prompt}]
        turn_index = 0
        last_logged_index = 0
        session = make_prompt_session()
        ui.start()
        try:
            while True:
                ui.pause_for_input()
                try:
                    raw = await read_user_input(session)
                finally:
                    ui.resume_after_input()
                if raw is None:
                    break
                if not raw:
                    continue
                match raw:
                    case "/reset":
                        messages.clear()
                        messages.append({"role": "system", "content": system_prompt})
                        ctx.reset()
                        last_logged_index = 0
                        ui.reset_separator()
                        ui.print("Context reset. Agent state and run log preserved.")
                    case "/quit":
                        await _graceful_shutdown(
                            manager, run_log, ui, timeout=config.shutdown_timeout_s
                        )
                        break
                    case "/agents":
                        ui.print_agent_panel_inline()
                    case "/log":
                        ui.print(str(run_log.path))
                    case _:
                        messages.append({"role": "user", "content": raw})
                        should_continue = True
                        while should_continue:
                            should_continue, last_logged_index = await run_one_turn(
                                messages,
                                config,
                                run_log,
                                ui,
                                registry,
                                client,
                                ctx,
                                turn_index,
                                last_logged_index,
                            )
                            turn_index += 1
                            if turn_index >= config.max_turns:
                                ui.print(f"Max turns ({config.max_turns}) reached.")
                                should_continue = False
        finally:
            run_log.finalize()
            ui.stop()
    finally:
        await client.aclose()
