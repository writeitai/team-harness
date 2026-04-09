import asyncio
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
from team_harness.coordinator.loop import run_one_turn
from team_harness.coordinator.system_prompt import build_system_prompt
from team_harness.harness import _build_registry
from team_harness.harness import _finalize_run
from team_harness.harness import _graceful_shutdown
from team_harness.harness import _make_client
from team_harness.harness import _make_run_id
from team_harness.harness import _prepare_session_output_dir
from team_harness.harness import _show_no_config_hint
from team_harness.harness import _warn_provider_startup
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
from team_harness.ui.prompt import make_prompt_session
from team_harness.ui.prompt import read_user_input


@click.group()
def main() -> None:
    """th \u2014 multi-agent AI orchestration harness."""


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
    _write_config_file(path=path, text=text, force=force)


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
    resolved_task = _prepare_task(task=task, task_file=task_file)
    allowed_agents = kwargs.pop("allowed_agents", None)
    harness = Harness(
        provider=kwargs.get("provider"),
        model=kwargs.get("model"),
        api_base=kwargs.get("api_base"),
        api_key=kwargs.get("api_key"),
        codex_auth_path=kwargs.get("codex_auth_path"),
        agents=allowed_agents,
        max_turns=kwargs.get("max_turns"),
        max_retries=kwargs.get("max_retries"),
        max_depth=kwargs.get("max_depth"),
        system_prompt=kwargs.get("system_prompt"),
        system_prompt_file=kwargs.get("system_prompt_file"),
        cwd=kwargs.get("cwd"),
        console_mode="auto",
    )
    await harness.run(resolved_task)


async def _repl(**kwargs: Any) -> None:
    config = load_config(**kwargs)
    _show_no_config_hint(config, ui=None)
    click.echo(
        "No config file found. Run `team-harness init` to create one."
    ) if config.global_config_path is None and config.local_config_path is None else None
    run_id = _make_run_id()
    run_dir = RUNS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    session_output_dir = _prepare_session_output_dir(config=config, session_id=run_id)
    config.run_dir = run_dir
    manager = AgentManager()
    client = _make_client(config)
    run_log: RunLogWriter | None = None
    ui: ConsoleBase | None = None
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
        ui = make_console(ctx=ctx, manager=manager, run_dir=run_dir, mode="auto")
        _warn_provider_startup(config, ui=ui)
        skills = load_skills(cwd=config.cwd)
        allowed_types = get_allowed_types(config)
        validate_templates(config=config, allowed_types=allowed_types)
        agent_tools.setup(
            manager=manager,
            run_log=run_log,
            config=config,
            ui=ui,
            session_output_dir=str(session_output_dir),
        )
        todo_tools.setup(run_dir=run_dir)
        fs_tools.setup_fs()
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
        messages = [{"role": "system", "content": system_prompt}]
        turn_index = 0
        last_logged_index = 0
        session = make_prompt_session()
        ui.print_welcome(model=config.model, cwd=config.cwd, provider=config.provider)
        # Do NOT call ui.start() — that enables Rich Live, which takes over
        # terminal space and causes the viewport to jump. In REPL mode
        # everything prints inline (like Codex): prompt stays in place,
        # response streams below, next prompt appears after.
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
                            manager=manager,
                            run_log=run_log,
                            ui=ui,
                            timeout=config.shutdown_timeout_s,
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
                                messages=messages,
                                config=config,
                                run_log=run_log,
                                ui=ui,
                                tool_registry=registry,
                                client=client,
                                ctx=ctx,
                                turn_index=turn_index,
                                last_logged_index=last_logged_index,
                            )
                            turn_index += 1
                            if turn_index >= config.max_turns:
                                ui.print(f"Max turns ({config.max_turns}) reached.")
                                should_continue = False
        finally:
            await _finalize_run(
                manager=manager,
                run_log=run_log,
                session_output_dir=session_output_dir,
                shutdown_timeout_s=config.shutdown_timeout_s,
                ui=ui,
            )
            ui.stop()
    finally:
        await client.aclose()
