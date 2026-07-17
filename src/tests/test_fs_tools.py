# pyright: reportMissingParameterType=false

import asyncio

import pytest

from team_harness.tools import fs_tools


@pytest.mark.asyncio
async def test_fs_tools_round_trip(tmp_path):
    """Core file tools preserve their established small-file behavior."""
    fs_tools.setup_fs()
    path = tmp_path / "a" / "file.txt"
    assert (
        await fs_tools.write_file(path=str(path), content="hello")
        == f"Written 5 bytes to {path}."
    )
    assert await fs_tools.read_file(path=str(path)) == "hello"
    assert (
        await fs_tools.append_file(path=str(path), content=" world")
        == f"Appended 6 bytes to {path}."
    )
    assert await fs_tools.edit_file(path=str(path), old="hello", new="hi") == "Edited."
    assert await fs_tools.read_file(path=str(path)) == "hi world"


@pytest.mark.asyncio
async def test_read_file_preserves_empty_and_exact_limit_files(tmp_path):
    """Empty and exact-boundary files retain the historical unwrapped result."""
    path = tmp_path / "boundary.txt"
    path.write_text(data="")
    assert await fs_tools.read_file(path=str(path)) == ""

    exact_content = "x" * fs_tools.READ_FILE_MAX_LIMIT_CHARS
    path.write_text(data=exact_content)
    assert await fs_tools.read_file(path=str(path)) == exact_content


@pytest.mark.asyncio
async def test_read_file_pages_large_content_with_explicit_continuation(tmp_path):
    """Large reads return one page and an exact continuation offset."""
    path = tmp_path / "large.txt"
    first_page = "a" * fs_tools.READ_FILE_DEFAULT_LIMIT_CHARS
    path.write_text(data=first_page + "tail")

    first_result = await fs_tools.read_file(path=str(path))
    second_result = await fs_tools.read_file(
        path=str(path), offset_chars=fs_tools.READ_FILE_DEFAULT_LIMIT_CHARS
    )

    assert first_result.startswith(first_page)
    assert "tail" not in first_result
    assert f"characters [0, {fs_tools.READ_FILE_DEFAULT_LIMIT_CHARS})" in first_result
    assert (
        f"continue with offset_chars={fs_tools.READ_FILE_DEFAULT_LIMIT_CHARS}"
        in first_result
    )
    assert second_result.startswith("tail\n")
    assert "end of file" in second_result


@pytest.mark.asyncio
async def test_read_file_supports_smaller_named_pages(tmp_path):
    """Coordinators can request a smaller bounded page with named arguments."""
    path = tmp_path / "paged.txt"
    path.write_text(data="abcdefghij")

    result = await fs_tools.read_file(path=str(path), offset_chars=2, limit_chars=4)

    assert result.startswith("cdef\n")
    assert "characters [2, 6) of 10" in result
    assert "continue with offset_chars=6" in result


@pytest.mark.asyncio
async def test_read_file_offsets_are_unicode_characters(tmp_path):
    """Pagination never splits a decoded Unicode character."""
    path = tmp_path / "unicode.txt"
    path.write_text(data="aé🙂z")

    result = await fs_tools.read_file(path=str(path), offset_chars=2, limit_chars=1)

    assert result.startswith("🙂\n")
    assert "characters [2, 3) of 4" in result


@pytest.mark.asyncio
async def test_read_file_caps_utf8_bytes_without_splitting_unicode(tmp_path):
    """A non-ASCII page obeys the byte ceiling and resumes by character offset."""
    path = tmp_path / "unicode-large.txt"
    characters_per_page = fs_tools.READ_FILE_MAX_CONTENT_BYTES // len(
        "🙂".encode(encoding="utf-8")
    )
    path.write_text(data="🙂" * (characters_per_page + 1))

    result = await fs_tools.read_file(path=str(path))
    content, metadata = result.split("\n[read_file page:", maxsplit=1)

    assert len(content.encode(encoding="utf-8")) == fs_tools.READ_FILE_MAX_CONTENT_BYTES
    assert content == "🙂" * characters_per_page
    assert f"characters [0, {characters_per_page})" in metadata
    assert f"continue with offset_chars={characters_per_page}" in metadata
    assert f"truncated at {fs_tools.READ_FILE_MAX_CONTENT_BYTES}-byte" in metadata


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("offset_chars", "limit_chars", "message"),
    [
        (-1, 1, "offset_chars must be greater than or equal to 0"),
        (True, 1, "offset_chars must be an integer"),
        (0, 0, "limit_chars must be between"),
        (0, True, "limit_chars must be an integer"),
        (0, fs_tools.READ_FILE_MAX_LIMIT_CHARS + 1, "limit_chars must be between"),
    ],
)
async def test_read_file_rejects_invalid_page_arguments_before_file_access(
    tmp_path, offset_chars, limit_chars, message
):
    """Invalid pagination fails before an absent path is accessed."""
    missing_path = tmp_path / "missing.txt"

    with pytest.raises(ValueError, match=message):
        await fs_tools.read_file(
            path=str(missing_path), offset_chars=offset_chars, limit_chars=limit_chars
        )


def test_read_file_schema_exposes_bounded_pagination():
    """The coordinator schema advertises the same bounds as the implementation."""
    properties = fs_tools.READ_FILE_SCHEMA["function"]["parameters"]["properties"]

    assert properties["offset_chars"]["default"] == 0
    assert properties["offset_chars"]["minimum"] == 0
    assert (
        properties["limit_chars"]["default"] == fs_tools.READ_FILE_DEFAULT_LIMIT_CHARS
    )
    assert properties["limit_chars"]["minimum"] == 1
    assert properties["limit_chars"]["maximum"] == fs_tools.READ_FILE_MAX_LIMIT_CHARS
    incremental_description = fs_tools.READ_NEW_FILE_CONTENT_SCHEMA["function"][
        "description"
    ]
    assert "bounded FIFO page" in incremental_description
    assert "call again with the same path" in incremental_description


@pytest.mark.asyncio
async def test_multi_edit_and_incremental_reads(tmp_path):
    """Incremental reads return only unseen small appends under concurrency."""
    fs_tools.setup_fs()
    path = tmp_path / "file.txt"
    path.write_text("alpha beta gamma")
    assert (
        await fs_tools.multi_edit_file(
            path=str(path),
            edits=[{"old": "alpha", "new": "A"}, {"old": "gamma", "new": "G"}],
        )
        == f"Applied 2 edits to {path}."
    )
    assert path.read_text() == "A beta G"
    path.write_text("one\n")
    assert await fs_tools.read_new_file_content(path=str(path)) == "one\n"
    path.write_text("one\ntwo\n")
    results = await asyncio.gather(
        fs_tools.read_new_file_content(path=str(path)),
        fs_tools.read_new_file_content(path=str(path)),
    )
    assert "".join(results) == "two\n"


@pytest.mark.asyncio
async def test_read_new_file_content_pages_backlog_without_loss(tmp_path):
    """Incremental reads preserve FIFO backlog and exact final-page behavior."""
    fs_tools.setup_fs()
    path = tmp_path / "large-incremental.txt"
    first_page = "a" * fs_tools.READ_FILE_MAX_CONTENT_BYTES
    path.write_text(data=first_page + "tail")

    first_result = await fs_tools.read_new_file_content(path=str(path))
    second_result = await fs_tools.read_new_file_content(path=str(path))
    third_result = await fs_tools.read_new_file_content(path=str(path))

    assert first_result.startswith(first_page)
    assert "tail" not in first_result
    assert f"bytes [0, {fs_tools.READ_FILE_MAX_CONTENT_BYTES})" in first_result
    assert "call again with the same path" in first_result
    assert second_result == "tail"
    assert third_result == ""


@pytest.mark.asyncio
async def test_read_new_file_content_preserves_unicode_across_raw_page_boundary(
    tmp_path,
):
    """A multi-byte code point crossing the raw page boundary is returned whole."""
    fs_tools.setup_fs()
    path = tmp_path / "unicode-incremental.txt"
    ascii_prefix = "a" * (fs_tools.READ_FILE_MAX_CONTENT_BYTES - 1)
    path.write_text(data=ascii_prefix + "🙂tail")

    first_result = await fs_tools.read_new_file_content(path=str(path))
    second_result = await fs_tools.read_new_file_content(path=str(path))

    assert first_result.startswith(ascii_prefix)
    assert "�" not in first_result
    assert f"bytes [0, {fs_tools.READ_FILE_MAX_CONTENT_BYTES - 1})" in first_result
    assert second_result == "🙂tail"


@pytest.mark.asyncio
async def test_read_new_file_content_bounds_invalid_utf8_expansion(tmp_path):
    """Replacement characters stay byte-bounded without replaying raw input."""
    fs_tools.setup_fs()
    path = tmp_path / "invalid-incremental.bin"
    raw_content = b"\xff" * (fs_tools.READ_FILE_MAX_CONTENT_BYTES + 7)
    path.write_bytes(data=raw_content)
    decoded_parts: list[str] = []

    for _ in range(10):
        result = await fs_tools.read_new_file_content(path=str(path))
        if not result:
            break
        marker = "[read_new_file_content page:"
        if marker in result:
            content = result.partition(marker)[0].removesuffix("\n")
        else:
            content = result
        assert len(content.encode(encoding="utf-8")) <= (
            fs_tools.READ_FILE_MAX_CONTENT_BYTES
        )
        decoded_parts.append(content)
    else:
        pytest.fail(reason="incremental reader did not exhaust invalid UTF-8 backlog")

    assert "".join(decoded_parts) == raw_content.decode(
        encoding="utf-8", errors="replace"
    )


@pytest.mark.asyncio
async def test_multi_edit_is_atomic_on_error(tmp_path):
    fs_tools.setup_fs()
    path = tmp_path / "file.txt"
    original = "alpha beta gamma"
    path.write_text(original)

    result = await fs_tools.multi_edit_file(
        path=str(path),
        edits=[{"old": "alpha", "new": "A"}, {"old": "missing", "new": "X"}],
    )

    assert result == "ERROR: string not found: 'missing'"
    assert path.read_text() == original


@pytest.mark.asyncio
async def test_glob_grep_and_ls(tmp_path):
    fs_tools.setup_fs()
    (tmp_path / "dir").mkdir()
    (tmp_path / "dir" / "a.txt").write_text("needle")
    assert await fs_tools.glob(pattern="dir/*.txt", cwd=str(tmp_path)) == "dir/a.txt"
    assert "needle" in await fs_tools.grep(pattern="needle", path=str(tmp_path / "dir"))
    listing = await fs_tools.ls(str(tmp_path))
    assert "dir\tdir" in listing
