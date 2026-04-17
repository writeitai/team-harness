# pyright: reportMissingParameterType=false

import asyncio
import builtins
from contextlib import contextmanager

from prompt_toolkit.application import create_app_session
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
import pytest

from team_harness.ui.paste import PASTE_LINE_THRESHOLD
from team_harness.ui.paste import PasteBuffer
from team_harness.ui.paste import PLACEHOLDER_FORMAT
from team_harness.ui.prompt import make_prompt_session
from team_harness.ui.prompt import read_user_input


def _lines(count: int, *, prefix: str = "line") -> str:
    return "\n".join(f"{prefix}{index}" for index in range(1, count + 1))


def _bracketed_paste(text: str) -> str:
    return f"\x1b[200~{text}\x1b[201~"


async def _flush_prompt() -> None:
    for _ in range(3):
        await asyncio.sleep(0)


async def _wait_buffer(session, *, expected: str, timeout: float = 1.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if session.default_buffer.text == expected:
            return
        await asyncio.sleep(0.001)
    raise AssertionError(
        "timed out waiting for buffer=="
        f"{expected!r}, got {session.default_buffer.text!r}"
    )


@contextmanager
def _interactive_session(*, history: InMemoryHistory | None = None):
    with create_pipe_input() as inp:
        output = DummyOutput()
        with create_app_session(input=inp, output=output):
            session = make_prompt_session(history=history, input=inp, output=output)
            assert session is not None
            yield inp, session


def test_paste_buffer_below_threshold_returns_raw_text():
    buffer = PasteBuffer()
    raw = _lines(PASTE_LINE_THRESHOLD)

    assert buffer.store_and_placeholder(raw) == raw
    assert buffer.entries == {}
    assert buffer.counter == 0


def test_paste_buffer_at_threshold_returns_placeholder():
    buffer = PasteBuffer()
    pasted = _lines(PASTE_LINE_THRESHOLD + 1)

    placeholder = buffer.store_and_placeholder(pasted)

    assert placeholder == PLACEHOLDER_FORMAT.format(id=1, lines=PASTE_LINE_THRESHOLD)
    assert buffer.entries == {1: pasted}
    assert buffer.counter == 1


def test_paste_buffer_line_count_uses_normalized_newlines():
    buffer = PasteBuffer()

    placeholder = buffer.store_and_placeholder("a\r\nb\r\nc\rd\r\ne")

    assert placeholder == PLACEHOLDER_FORMAT.format(id=1, lines=PASTE_LINE_THRESHOLD)


def test_paste_buffer_expand_round_trips_single_placeholder():
    buffer = PasteBuffer()
    pasted = _lines(PASTE_LINE_THRESHOLD + 1)
    placeholder = buffer.store_and_placeholder(pasted)

    assert buffer.expand(f"before {placeholder} after") == f"before {pasted} after"


def test_paste_buffer_expand_multiple_placeholders_preserves_ordering():
    buffer = PasteBuffer()
    first = "\n".join(
        ["alpha", "[Pasted text #2 +4 lines]", "gamma", "delta", "epsilon"]
    )
    second = _lines(PASTE_LINE_THRESHOLD + 1, prefix="item")
    first_placeholder = buffer.store_and_placeholder(first)
    second_placeholder = buffer.store_and_placeholder(second)

    expanded = buffer.expand(f"{first_placeholder} middle {second_placeholder}")

    assert expanded == f"{first} middle {second}"


def test_paste_buffer_expand_leaves_partial_placeholders_unchanged():
    buffer = PasteBuffer()
    buffer.store_and_placeholder(_lines(PASTE_LINE_THRESHOLD + 1))
    partial = "[Pasted text #1 +4 lines"

    assert buffer.expand(partial) == partial


def test_paste_buffer_expand_leaves_unknown_placeholders_unchanged():
    buffer = PasteBuffer()
    unknown = "[Pasted text #99 +5 lines]"

    assert buffer.expand(unknown) == unknown


def test_paste_buffer_expand_ignores_placeholder_without_line_count():
    buffer = PasteBuffer()
    buffer.store_and_placeholder(_lines(PASTE_LINE_THRESHOLD + 1))
    text = "[Pasted text #1]"

    assert buffer.expand(text) == text


def test_paste_buffer_reset_clears_entries_and_counter():
    buffer = PasteBuffer()
    buffer.store_and_placeholder(_lines(PASTE_LINE_THRESHOLD + 1))

    buffer.reset()

    assert buffer.entries == {}
    assert buffer.counter == 0


def test_paste_buffer_normalizes_crlf_and_cr_in_stored_text():
    buffer = PasteBuffer()
    placeholder = buffer.store_and_placeholder("a\r\nb\r\nc\r\nd\r\ne")

    assert buffer.entries == {1: "a\nb\nc\nd\ne"}
    assert buffer.expand(placeholder) == "a\nb\nc\nd\ne"


@pytest.mark.asyncio
async def test_make_session_returns_session_when_interactive():
    with _interactive_session() as (_, session):
        assert session is not None


def test_make_session_returns_none_when_not_tty(monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    monkeypatch.setattr("sys.stdout.isatty", lambda: False)

    assert make_prompt_session() is None


@pytest.mark.asyncio
async def test_single_line_submit():
    with _interactive_session() as (inp, session):
        task = asyncio.create_task(read_user_input(session))
        inp.send_text("hello\r")

        assert await task == "hello"


@pytest.mark.asyncio
async def test_alt_enter_inserts_newline():
    with _interactive_session() as (inp, session):
        task = asyncio.create_task(read_user_input(session))
        inp.send_text("hello\x1b\rworld\r")

        assert await task == "hello\nworld"


@pytest.mark.asyncio
async def test_double_escape_clears_buffer():
    with _interactive_session() as (inp, session):
        task = asyncio.create_task(read_user_input(session))
        inp.send_text("abc\x1b\x1bhello\r")

        assert await task == "hello"


@pytest.mark.asyncio
async def test_eof_returns_none_and_restores_accept_handler():
    with _interactive_session() as (inp, session):
        original_accept_handler = session.default_buffer.accept_handler
        task = asyncio.create_task(read_user_input(session))
        inp.close()

        assert await task is None
        assert session.default_buffer.accept_handler is original_accept_handler


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
    with _interactive_session(history=InMemoryHistory()) as (inp, session):
        first = asyncio.create_task(read_user_input(session))
        inp.send_text("first\r")
        assert await first == "first"

        second = asyncio.create_task(read_user_input(session))
        inp.send_text("\x1b[A\r")
        assert await second == "first"


@pytest.mark.asyncio
async def test_ctrl_c_exits_and_restores_accept_handler():
    with _interactive_session() as (inp, session):
        original_accept_handler = session.default_buffer.accept_handler
        task = asyncio.create_task(read_user_input(session))
        inp.send_text("\x03")

        assert await task is None
        assert session.default_buffer.accept_handler is original_accept_handler


@pytest.mark.asyncio
async def test_empty_input_returns_empty_string():
    with _interactive_session() as (inp, session):
        task = asyncio.create_task(read_user_input(session))
        inp.send_text("\r")

        assert await task == ""


@pytest.mark.asyncio
async def test_whitespace_only_input_is_stripped_to_empty():
    with _interactive_session() as (inp, session):
        task = asyncio.create_task(read_user_input(session))
        inp.send_text("   \r")

        assert await task == ""


@pytest.mark.asyncio
async def test_multiple_consecutive_prompts():
    with _interactive_session() as (inp, session):
        first = asyncio.create_task(read_user_input(session))
        inp.send_text("first\r")
        assert await first == "first"

        second = asyncio.create_task(read_user_input(session))
        inp.send_text("second\r")
        assert await second == "second"


@pytest.mark.asyncio
async def test_double_escape_on_empty_buffer():
    with _interactive_session() as (inp, session):
        task = asyncio.create_task(read_user_input(session))
        inp.send_text("\x1b\x1bhello\r")

        assert await task == "hello"


@pytest.mark.asyncio
async def test_alt_enter_at_beginning():
    with _interactive_session() as (inp, session):
        task = asyncio.create_task(read_user_input(session))
        inp.send_text("\x1b\rhello\r")

        assert await task == "hello"


@pytest.mark.asyncio
async def test_non_tty_empty_input(monkeypatch):
    monkeypatch.setattr(builtins, "input", lambda prompt: "")

    assert await read_user_input(None) == ""


@pytest.mark.asyncio
async def test_short_bracketed_paste_inserts_raw_text():
    pasted = _lines(PASTE_LINE_THRESHOLD)

    with _interactive_session() as (inp, session):
        task = asyncio.create_task(read_user_input(session))
        await _flush_prompt()
        inp.send_text(_bracketed_paste(pasted))
        await _wait_buffer(session, expected=pasted)

        inp.send_text("\r")
        assert await task == pasted


@pytest.mark.asyncio
async def test_long_bracketed_paste_shows_placeholder_before_submit():
    pasted = _lines(10)

    with _interactive_session() as (inp, session):
        task = asyncio.create_task(read_user_input(session))
        await _flush_prompt()
        inp.send_text(_bracketed_paste(pasted))
        await _wait_buffer(session, expected=PLACEHOLDER_FORMAT.format(id=1, lines=9))

        inp.send_text("\r")
        assert await task == pasted


@pytest.mark.asyncio
async def test_long_bracketed_paste_expands_on_submit():
    pasted = _lines(PASTE_LINE_THRESHOLD + 1)

    with _interactive_session() as (inp, session):
        task = asyncio.create_task(read_user_input(session))
        await _flush_prompt()
        inp.send_text(f"before {_bracketed_paste(pasted)} after\r")

        assert await task == f"before {pasted} after"


@pytest.mark.asyncio
async def test_crlf_bracketed_paste_is_normalized():
    pasted = "a\r\nb\r\nc\r\nd\r\ne"

    with _interactive_session() as (inp, session):
        task = asyncio.create_task(read_user_input(session))
        await _flush_prompt()
        inp.send_text(_bracketed_paste(pasted) + "\r")
        result = await task

        assert result == "a\nb\nc\nd\ne"
        assert "\r" not in result


@pytest.mark.asyncio
async def test_two_long_pastes_use_incrementing_placeholders():
    first = _lines(PASTE_LINE_THRESHOLD + 1, prefix="first")
    second = _lines(PASTE_LINE_THRESHOLD + 1, prefix="second")
    expected_buffer = " ".join(
        [
            PLACEHOLDER_FORMAT.format(id=1, lines=PASTE_LINE_THRESHOLD),
            "middle",
            PLACEHOLDER_FORMAT.format(id=2, lines=PASTE_LINE_THRESHOLD),
        ]
    )

    with _interactive_session() as (inp, session):
        task = asyncio.create_task(read_user_input(session))
        await _flush_prompt()
        inp.send_text(_bracketed_paste(first))
        inp.send_text(" middle ")
        inp.send_text(_bracketed_paste(second))
        await _wait_buffer(session, expected=expected_buffer)

        inp.send_text("\r")
        assert await task == f"{first} middle {second}"


@pytest.mark.asyncio
async def test_deleting_placeholder_drops_pasted_text():
    pasted = _lines(PASTE_LINE_THRESHOLD + 1)
    placeholder = PLACEHOLDER_FORMAT.format(id=1, lines=PASTE_LINE_THRESHOLD)

    with _interactive_session() as (inp, session):
        task = asyncio.create_task(read_user_input(session))
        await _flush_prompt()
        inp.send_text(_bracketed_paste(pasted))
        await _flush_prompt()
        inp.send_text("\x7f" * len(placeholder))
        inp.send_text("\r")

        assert await task == ""


@pytest.mark.asyncio
async def test_history_recall_returns_expanded_pasted_text():
    pasted = _lines(PASTE_LINE_THRESHOLD + 1)

    with _interactive_session(history=InMemoryHistory()) as (inp, session):
        first = asyncio.create_task(read_user_input(session))
        await _flush_prompt()
        inp.send_text(_bracketed_paste(pasted) + "\r")
        assert await first == pasted

        second = asyncio.create_task(read_user_input(session))
        await _flush_prompt()
        inp.send_text("\x1b[A\r")
        assert await second == pasted


@pytest.mark.asyncio
async def test_non_tty_placeholder_like_text_is_unchanged(monkeypatch):
    placeholder = "[Pasted text #99 +5 lines]"
    monkeypatch.setattr(builtins, "input", lambda prompt: placeholder)

    assert await read_user_input(None) == placeholder


@pytest.mark.asyncio
async def test_accept_handler_is_restored_after_submit():
    with _interactive_session() as (inp, session):
        original_accept_handler = session.default_buffer.accept_handler
        task = asyncio.create_task(read_user_input(session))
        await _flush_prompt()
        inp.send_text("hello\r")

        assert await task == "hello"
        assert session.default_buffer.accept_handler is original_accept_handler


@pytest.mark.asyncio
async def test_empty_bracketed_paste_is_a_no_op():
    with _interactive_session() as (inp, session):
        task = asyncio.create_task(read_user_input(session))
        await _flush_prompt()
        inp.send_text(_bracketed_paste(""))
        await _wait_buffer(session, expected="")

        inp.send_text("\r")
        assert await task == ""
