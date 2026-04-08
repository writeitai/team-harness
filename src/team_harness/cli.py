import asyncio
from datetime import datetime
from datetime import timezone
import json
from pathlib import Path
from typing import Any

import click

from team_harness.agents.manager import AgentManager
from team_harness.agents.registry import get_allowed_types
from team_harness.agents.registry import validate_templates
from team_harness.config import _default_config_text
from team_harness.config import _local_config_text
from team_harness.config import CONFIG_PATH
from team_harness.config import load_config
from team_harness.config import LOCAL_CONFIG_DIR_NAME
from team_harness.config import RUNS_DIR
from team_harness.coordinator.client import CoordinatorClient
from team_harness.coordinator.loop import run_one_turn
from team_harness.coordinator.system_prompt import build_system_prompt
from team_harness.harness import _build_registry
from team_harness.harness import _make_run_id
from team_harness.harness import _show_no_config_hint
from team_harness.harness import _warn_missing_api_key
from team_harness.harness import Harness
from team_harness.skills.loader import load_skills
from team_harness.skills.loader import SkillContext
from team_harness.tools import agent_tools
from team_harness.tools import fs_tools
from team_harness.tools import todo_tools
from team_harness.tracking.context import ContextTracker
from team_harness.tracking.context import resolve_model_limit
from team_harness.tracking.run_log import RunLogWriter
from team_harness.ui.console import ConsoleBase
from team_harness.ui.console import make_console


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
@click.option("--model", default=None)
@click.option("--api-base", default=None)
@click.option("--api-key", default=None)
@click.option("--agents", default=None)
@click.option("--max-turns", type=int, default=None)
@click.option("--max-retries", type=int, default=None)
@click.option("--max-depth", type=int, default=None)
@click.option("--system-prompt", default=None)
@click.option("--system-prompt-file", default=None)
@click.option("--cwd", default=".")
def run_cli(
    task: str | None,
    task_file: str | None,
    model: str | None,
    api_base: str | None,
    api_key: str | None,
    agents: str | None,
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
            model=model,
            api_base=api_base,
            api_key=api_key,
            agents=agents,
            max_turns=max_turns,
            max_retries=max_retries,
            max_depth=max_depth,
            system_prompt=system_prompt,
            system_prompt_file=system_prompt_file,
            cwd=cwd,
        )
    )


@main.command()
@click.option("--model", default=None)
@click.option("--api-base", default=None)
@click.option("--api-key", default=None)
@click.option("--agents", default=None)
@click.option("--max-turns", type=int, default=None)
@click.option("--max-retries", type=int, default=None)
@click.option("--max-depth", type=int, default=None)
@click.option("--system-prompt", default=None)
@click.option("--system-prompt-file", default=None)
@click.option("--cwd", default=".")
def repl(
    model: str | None,
    api_base: str | None,
    api_key: str | None,
    agents: str | None,
    max_turns: int | None,
    max_retries: int | None,
    max_depth: int | None,
    system_prompt: str | None,
    system_prompt_file: str | None,
    cwd: str,
) -> None:
    asyncio.run(
        _repl(
            model=model,
            api_base=api_base,
            api_key=api_key,
            agents=agents,
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


def _prepare_task(task: str | None, task_file: str | None) -> str:
    if task and task_file:
        raise click.UsageError("Provide either TASK or --file, not both.")
    if not task and not task_file:
        raise click.UsageError("Provide TASK or --file.")
    if task_file:
        return Path(task_file).read_text()
    assert task is not None
    return task


async def _run(task: str | None, task_file: str | None, **kwargs: Any) -> None:
    resolved_task = _prepare_task(task, task_file)
    harness = Harness(console_mode="auto", **kwargs)
    await harness.run(resolved_task)


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
    run_id = _make_run_id()
    run_dir = RUNS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    config.run_dir = run_dir
    run_log = RunLogWriter(run_id, run_dir, config.model, config.api_base)
    manager = AgentManager()
    client = CoordinatorClient(config.api_base, config.api_key, config.model)
    model_limit = await resolve_model_limit(config.model, client, config)
    ctx = ContextTracker(model_id=config.model, model_limit=model_limit)
    ui = make_console(ctx=ctx, manager=manager, run_dir=run_dir)
    _show_no_config_hint(config, ui)
    _warn_missing_api_key(config, ui)
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
    ui.start()
    try:
        while True:
            try:
                raw = await asyncio.to_thread(input, "\n> ")
            except EOFError:
                break
            raw = raw.strip()
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
