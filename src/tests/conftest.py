# pyright: reportMissingParameterType=false

import asyncio
from pathlib import Path

import pytest

from team_harness.agents.manager import AgentManager
from team_harness.config import Config
from team_harness.tracking.context import ContextTracker


class DummyToolContext:
    def __init__(self) -> None:
        self.results: list[tuple[str, bool]] = []

    def result(self, text: str, is_error: bool = False) -> None:
        self.results.append((text, is_error))


class DummyUI:
    def __init__(self) -> None:
        self.turns: list[int] = []
        self.messages: list[str] = []
        self.tokens: list[str] = []
        self.tool_calls: list[tuple[str, dict, DummyToolContext]] = []
        self.agent_events: list[tuple[str, str]] = []
        self.warning_count = 0
        self.reset_count = 0
        self.inline_count = 0
        self.pause_count = 0
        self.resume_count = 0

    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None

    def pause_for_input(self) -> None:
        self.pause_count += 1

    def resume_after_input(self) -> None:
        self.resume_count += 1

    def begin_turn(self, n: int) -> None:
        self.turns.append(n)

    def begin_streaming(self) -> None:
        return None

    def stream_token(self, token: str) -> None:
        self.tokens.append(token)

    def end_streaming(self) -> None:
        return None

    def end_turn(self) -> None:
        return None

    def tool_call_start(self, name: str, args: dict) -> DummyToolContext:
        ctx = DummyToolContext()
        self.tool_calls.append((name, args, ctx))
        return ctx

    def agent_event(self, event: str, state) -> None:
        self.agent_events.append((event, state.id))

    def context_warning(self) -> None:
        self.warning_count += 1

    def reset_separator(self) -> None:
        self.reset_count += 1

    def print(self, msg: str) -> None:
        self.messages.append(msg)

    def print_agent_panel_inline(self) -> None:
        self.inline_count += 1


@pytest.fixture
def config(tmp_path: Path) -> Config:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    return Config(
        provider="openai_compat",
        model="test/model",
        api_base="http://localhost:9999",
        api_key="test-key",
        cwd=str(tmp_path),
        run_dir=run_dir,
    )


@pytest.fixture
def manager() -> AgentManager:
    return AgentManager()


@pytest.fixture
def run_dir(tmp_path: Path) -> Path:
    path = tmp_path / "run"
    path.mkdir()
    return path


@pytest.fixture
def ctx() -> ContextTracker:
    return ContextTracker(model_id="test/model", model_limit=100)


@pytest.fixture
def ui() -> DummyUI:
    return DummyUI()


@pytest.fixture
async def sleep_process(tmp_path: Path):
    proc = await asyncio.create_subprocess_exec("sleep", "5")
    yield proc
    if proc.returncode is None:
        proc.kill()
        await proc.wait()
