# pyright: reportMissingParameterType=false

import asyncio
import builtins
from typing import cast

from prompt_toolkit import PromptSession
from prompt_toolkit.application import create_app_session
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
import pytest

from team_harness.ui.prompt import make_prompt_session
from team_harness.ui.prompt import read_user_input


@pytest.mark.asyncio
async def test_make_session_returns_session_when_interactive():
    with create_pipe_input() as inp:
        output = DummyOutput()
        with create_app_session(input=inp, output=output):
            session = make_prompt_session(input=inp, output=output)

    assert session is not None


def test_make_session_returns_none_when_not_tty(monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    monkeypatch.setattr("sys.stdout.isatty", lambda: False)

    assert make_prompt_session() is None


@pytest.mark.asyncio
async def test_single_line_submit():
    with create_pipe_input() as inp:
        output = DummyOutput()
        with create_app_session(input=inp, output=output):
            session = make_prompt_session(input=inp, output=output)
            task = asyncio.create_task(read_user_input(session))
            inp.send_text("hello\r")

            assert await task == "hello"


@pytest.mark.asyncio
async def test_alt_enter_inserts_newline():
    with create_pipe_input() as inp:
        output = DummyOutput()
        with create_app_session(input=inp, output=output):
            session = make_prompt_session(input=inp, output=output)
            task = asyncio.create_task(read_user_input(session))
            inp.send_text("hello\x1b\rworld\r")

            assert await task == "hello\nworld"


@pytest.mark.asyncio
async def test_double_escape_clears_buffer():
    with create_pipe_input() as inp:
        output = DummyOutput()
        with create_app_session(input=inp, output=output):
            session = make_prompt_session(input=inp, output=output)
            task = asyncio.create_task(read_user_input(session))
            inp.send_text("abc\x1b\x1bhello\r")

            assert await task == "hello"


@pytest.mark.asyncio
async def test_eof_returns_none():
    with create_pipe_input() as inp:
        output = DummyOutput()
        with create_app_session(input=inp, output=output):
            session = make_prompt_session(input=inp, output=output)
            task = asyncio.create_task(read_user_input(session))
            inp.close()

            assert await task is None


@pytest.mark.asyncio
async def test_non_tty_fallback(monkeypatch):
    monkeypatch.setattr(builtins, "input", lambda prompt: " hello ")

    assert await read_user_input(None) == "hello"


@pytest.mark.asyncio
async def test_non_tty_eof(monkeypatch):
    def _raise(prompt: str) -> str:
        raise EOFError

    monkeypatch.setattr(builtins, "input", _raise)

    assert await read_user_input(None) is None


@pytest.mark.asyncio
async def test_history_recall():
    with create_pipe_input() as inp:
        output = DummyOutput()
        history = InMemoryHistory()
        with create_app_session(input=inp, output=output):
            session = make_prompt_session(history=history, input=inp, output=output)
            first = asyncio.create_task(read_user_input(session))
            inp.send_text("first\r")
            assert await first == "first"

            second = asyncio.create_task(read_user_input(session))
            inp.send_text("\x1b[A\r")
            assert await second == "first"


@pytest.mark.asyncio
async def test_ctrl_c_reprompts():
    class FakeSession:
        def __init__(self) -> None:
            self.calls = 0

        async def prompt_async(self, prompt: str) -> str:
            self.calls += 1
            if self.calls == 1:
                raise KeyboardInterrupt
            return " ok "

    session = FakeSession()

    assert await read_user_input(cast(PromptSession, session)) == "ok"
    assert session.calls == 2


@pytest.mark.asyncio
async def test_empty_input_returns_empty_string():
    with create_pipe_input() as inp:
        output = DummyOutput()
        with create_app_session(input=inp, output=output):
            session = make_prompt_session(input=inp, output=output)
            task = asyncio.create_task(read_user_input(session))
            inp.send_text("\r")

            assert await task == ""


@pytest.mark.asyncio
async def test_whitespace_only_input_is_stripped_to_empty():
    with create_pipe_input() as inp:
        output = DummyOutput()
        with create_app_session(input=inp, output=output):
            session = make_prompt_session(input=inp, output=output)
            task = asyncio.create_task(read_user_input(session))
            inp.send_text("   \r")

            assert await task == ""


@pytest.mark.asyncio
async def test_multiple_consecutive_prompts():
    with create_pipe_input() as inp:
        output = DummyOutput()
        with create_app_session(input=inp, output=output):
            session = make_prompt_session(input=inp, output=output)

            first = asyncio.create_task(read_user_input(session))
            inp.send_text("first\r")
            assert await first == "first"

            second = asyncio.create_task(read_user_input(session))
            inp.send_text("second\r")
            assert await second == "second"


@pytest.mark.asyncio
async def test_double_escape_on_empty_buffer():
    with create_pipe_input() as inp:
        output = DummyOutput()
        with create_app_session(input=inp, output=output):
            session = make_prompt_session(input=inp, output=output)
            task = asyncio.create_task(read_user_input(session))
            inp.send_text("\x1b\x1bhello\r")

            assert await task == "hello"


@pytest.mark.asyncio
async def test_alt_enter_at_beginning():
    with create_pipe_input() as inp:
        output = DummyOutput()
        with create_app_session(input=inp, output=output):
            session = make_prompt_session(input=inp, output=output)
            task = asyncio.create_task(read_user_input(session))
            inp.send_text("\x1b\rhello\r")

            result = await task
            assert result == "hello"


@pytest.mark.asyncio
async def test_non_tty_empty_input(monkeypatch):
    monkeypatch.setattr(builtins, "input", lambda prompt: "")

    assert await read_user_input(None) == ""
