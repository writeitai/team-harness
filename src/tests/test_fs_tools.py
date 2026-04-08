# pyright: reportMissingParameterType=false

import asyncio

import pytest

from team_harness.tools import fs_tools


@pytest.mark.asyncio
async def test_fs_tools_round_trip(tmp_path):
    fs_tools.setup_fs()
    path = tmp_path / "a" / "file.txt"
    assert (
        await fs_tools.write_file(str(path), "hello") == f"Written 5 bytes to {path}."
    )
    assert await fs_tools.read_file(str(path)) == "hello"
    assert (
        await fs_tools.append_file(str(path), " world")
        == f"Appended 6 bytes to {path}."
    )
    assert await fs_tools.edit_file(str(path), "hello", "hi") == "Edited."
    assert await fs_tools.read_file(str(path)) == "hi world"


@pytest.mark.asyncio
async def test_multi_edit_and_incremental_reads(tmp_path):
    fs_tools.setup_fs()
    path = tmp_path / "file.txt"
    path.write_text("alpha beta gamma")
    assert (
        await fs_tools.multi_edit_file(
            str(path), [{"old": "alpha", "new": "A"}, {"old": "gamma", "new": "G"}]
        )
        == f"Applied 2 edits to {path}."
    )
    assert path.read_text() == "A beta G"
    path.write_text("one\n")
    assert await fs_tools.read_new_file_content(str(path)) == "one\n"
    path.write_text("one\ntwo\n")
    results = await asyncio.gather(
        fs_tools.read_new_file_content(str(path)),
        fs_tools.read_new_file_content(str(path)),
    )
    assert "".join(results) == "two\n"


@pytest.mark.asyncio
async def test_glob_grep_and_ls(tmp_path):
    fs_tools.setup_fs()
    (tmp_path / "dir").mkdir()
    (tmp_path / "dir" / "a.txt").write_text("needle")
    assert await fs_tools.glob("dir/*.txt", cwd=str(tmp_path)) == "dir/a.txt"
    assert "needle" in await fs_tools.grep("needle", str(tmp_path / "dir"))
    listing = await fs_tools.ls(str(tmp_path))
    assert "dir\tdir" in listing
