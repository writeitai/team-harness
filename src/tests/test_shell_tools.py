# pyright: reportMissingParameterType=false

import asyncio

import pytest

from team_harness.tools.shell_tools import bash


@pytest.mark.asyncio
async def test_bash_echo_and_stdin_devnull():
    assert await bash("echo hello") == "hello\n"
    output = await bash('python3 -c "input()"')
    assert "EOFError" in output


@pytest.mark.asyncio
async def test_bash_timeout(monkeypatch):
    async def fake_wait_for(coro, timeout):
        coro.close()
        raise asyncio.TimeoutError

    monkeypatch.setattr("asyncio.wait_for", fake_wait_for)
    assert await bash("sleep 200") == "ERROR: command timed out after 120 seconds."
