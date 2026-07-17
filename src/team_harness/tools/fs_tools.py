import asyncio
import codecs
from collections.abc import Awaitable
from collections.abc import Callable
import os
from pathlib import Path

_file_cursors: dict[str, int] = {}
_file_locks: dict[str, asyncio.Lock] = {}

READ_FILE_DEFAULT_LIMIT_CHARS = 32_768
READ_FILE_MAX_LIMIT_CHARS = 32_768
READ_FILE_MAX_CONTENT_BYTES = 32 * 1024

READ_FILE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "read_file",
        "description": (
            "Read one bounded page of a text file. Small files are returned unchanged. "
            "Large files include continuation metadata; use offset_chars to read the "
            "next page instead of loading the whole file into coordinator context."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "offset_chars": {
                    "type": "integer",
                    "minimum": 0,
                    "default": 0,
                    "description": "Zero-based character offset for this page.",
                },
                "limit_chars": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": READ_FILE_MAX_LIMIT_CHARS,
                    "default": READ_FILE_DEFAULT_LIMIT_CHARS,
                    "description": (
                        "Maximum file-content characters to return in this page. The "
                        "hard character and UTF-8 byte maxima keep one tool result "
                        "within coordinator context."
                    ),
                },
            },
            "required": ["path"],
        },
    },
}
WRITE_FILE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "write_file",
        "description": "Write a file, creating parent directories automatically.",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"],
        },
    },
}
APPEND_FILE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "append_file",
        "description": "Append to a file, creating it and its parents if needed.",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"],
        },
    },
}
EDIT_FILE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "edit_file",
        "description": "Replace the first occurrence of an exact string in a file.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old": {"type": "string"},
                "new": {"type": "string"},
            },
            "required": ["path", "old", "new"],
        },
    },
}
MULTI_EDIT_FILE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "multi_edit_file",
        "description": "Apply multiple exact string replacements to a file atomically.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "edits": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "old": {"type": "string"},
                            "new": {"type": "string"},
                        },
                        "required": ["old", "new"],
                    },
                },
            },
            "required": ["path", "edits"],
        },
    },
}
LS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "ls",
        "description": "List directory contents with file sizes.",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
}
GLOB_SCHEMA = {
    "type": "function",
    "function": {
        "name": "glob",
        "description": "List files matching a glob pattern.",
        "parameters": {
            "type": "object",
            "properties": {"pattern": {"type": "string"}, "cwd": {"type": "string"}},
            "required": ["pattern"],
        },
    },
}
GREP_SCHEMA = {
    "type": "function",
    "function": {
        "name": "grep",
        "description": "Search file contents for a regex pattern.",
        "parameters": {
            "type": "object",
            "properties": {"pattern": {"type": "string"}, "path": {"type": "string"}},
            "required": ["pattern", "path"],
        },
    },
}
READ_NEW_FILE_CONTENT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "read_new_file_content",
        "description": (
            "Read one bounded FIFO page of content appended since the last call for "
            "this path. Useful for incremental reading of worker-generated artifacts. "
            "If continuation metadata says more content is available, call again with "
            "the same path. Returns an empty string if nothing is new."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute file path"}
            },
            "required": ["path"],
        },
    },
}


def setup_fs() -> None:
    _file_cursors.clear()
    _file_locks.clear()


def _validate_read_file_page(*, offset_chars: object, limit_chars: object) -> None:
    """Validate bounded file-read pagination before touching the filesystem."""
    if isinstance(offset_chars, bool) or not isinstance(offset_chars, int):
        raise ValueError("offset_chars must be an integer")
    if offset_chars < 0:
        raise ValueError("offset_chars must be greater than or equal to 0")
    if isinstance(limit_chars, bool) or not isinstance(limit_chars, int):
        raise ValueError("limit_chars must be an integer")
    if limit_chars < 1 or limit_chars > READ_FILE_MAX_LIMIT_CHARS:
        raise ValueError(
            f"limit_chars must be between 1 and {READ_FILE_MAX_LIMIT_CHARS}"
        )


def _page_end_within_utf8_limit(
    *, text: str, start_chars: int, requested_end_chars: int
) -> int:
    """Return the largest character boundary within the UTF-8 content limit."""
    candidate = text[start_chars:requested_end_chars]
    if len(candidate.encode(encoding="utf-8")) <= READ_FILE_MAX_CONTENT_BYTES:
        return requested_end_chars

    low = start_chars
    high = requested_end_chars
    while low < high:
        midpoint = (low + high + 1) // 2
        encoded_size = len(text[start_chars:midpoint].encode(encoding="utf-8"))
        if encoded_size <= READ_FILE_MAX_CONTENT_BYTES:
            low = midpoint
        else:
            high = midpoint - 1
    return low


def _read_file_page(*, path: str, offset_chars: int, limit_chars: int) -> str:
    """Read and render one character page within the hard UTF-8 byte ceiling."""
    text = Path(path).read_text(errors="replace")
    total_chars = len(text)
    if offset_chars >= total_chars:
        if offset_chars == 0:
            return ""
        return (
            f"[read_file page: offset_chars={offset_chars} is at or beyond "
            f"end of file ({total_chars} characters).]"
        )

    requested_end_chars = min(offset_chars + limit_chars, total_chars)
    end_chars = _page_end_within_utf8_limit(
        text=text, start_chars=offset_chars, requested_end_chars=requested_end_chars
    )
    content = text[offset_chars:end_chars]
    if offset_chars == 0 and end_chars == total_chars:
        return content

    separator = "" if content.endswith("\n") else "\n"
    if end_chars < total_chars:
        if end_chars < requested_end_chars:
            reason = (
                f"truncated at {READ_FILE_MAX_CONTENT_BYTES}-byte UTF-8 content cap"
            )
        else:
            reason = "truncated"
        page_status = f"{reason}; continue with offset_chars={end_chars}"
    else:
        page_status = "end of file"
    return (
        f"{content}{separator}[read_file page: characters [{offset_chars}, "
        f"{end_chars}) of {total_chars}; {page_status}.]"
    )


async def read_file(
    path: str, offset_chars: int = 0, limit_chars: int = READ_FILE_DEFAULT_LIMIT_CHARS
) -> str:
    """Read one bounded page from a text file without unbounded context injection."""
    _validate_read_file_page(offset_chars=offset_chars, limit_chars=limit_chars)
    return await asyncio.to_thread(
        _read_file_page, path=path, offset_chars=offset_chars, limit_chars=limit_chars
    )


async def write_file(path: str, content: str) -> str:
    target = Path(path)

    def _write() -> str:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        return f"Written {len(content)} bytes to {path}."

    return await asyncio.to_thread(_write)


async def append_file(path: str, content: str) -> str:
    target = Path(path)

    def _append() -> str:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as handle:
            handle.write(content)
        return f"Appended {len(content)} bytes to {path}."

    return await asyncio.to_thread(_append)


async def edit_file(path: str, old: str, new: str) -> str:
    target = Path(path)

    def _edit() -> str:
        text = target.read_text()
        if old not in text:
            return f"ERROR: string not found in {path}."
        target.write_text(text.replace(old, new, 1))
        return "Edited."

    return await asyncio.to_thread(_edit)


async def multi_edit_file(path: str, edits: list[dict]) -> str:
    target = Path(path)

    def _multi_edit() -> str:
        text = target.read_text()
        updated = text
        for edit in edits:
            old = str(edit["old"])
            new = str(edit["new"])
            if old not in updated:
                return f"ERROR: string not found: {old!r}"
            updated = updated.replace(old, new, 1)
        target.write_text(updated)
        return f"Applied {len(edits)} edits to {path}."

    return await asyncio.to_thread(_multi_edit)


async def ls(path: str) -> str:
    def _scan() -> str:
        rows: list[tuple[int, str]] = []
        with os.scandir(path) as entries:
            for entry in entries:
                prefix = 0 if entry.is_dir() else 1
                if entry.is_dir():
                    rows.append((prefix, f"{entry.name}\tdir"))
                else:
                    rows.append((prefix, f"{entry.name}\tfile\t{entry.stat().st_size}"))
        return "\n".join(
            row for _, row in sorted(rows, key=lambda item: (item[0], item[1]))
        )

    return await asyncio.to_thread(_scan)


async def glob(pattern: str, cwd: str = ".") -> str:
    def _glob() -> str:
        base = Path(cwd)
        matches = sorted(str(match.relative_to(base)) for match in base.glob(pattern))
        return "\n".join(matches) if matches else "(no matches)"

    return await asyncio.to_thread(_glob)


async def grep(pattern: str, path: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        "grep",
        "-rn",
        pattern,
        path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdout, _ = await proc.communicate()
    text = stdout.decode(errors="replace")
    if not text:
        return "(no matches)"
    if len(text) > 8192:
        return text[:8192] + "\n[output truncated at 8 KB]"
    return text


def _decode_incremental_utf8_page(*, data: bytes, at_eof: bool) -> tuple[str, int]:
    """Decode a bounded UTF-8 page and report the raw bytes it fully consumed."""
    decoder = codecs.getincrementaldecoder(encoding="utf-8")(errors="replace")
    content_parts: list[str] = []
    content_bytes = 0
    processed_bytes = 0

    for byte in data:
        prior_state = decoder.getstate()
        decoded = decoder.decode(bytes((byte,)), final=False)
        decoded_bytes = len(decoded.encode(encoding="utf-8"))
        if content_bytes + decoded_bytes > READ_FILE_MAX_CONTENT_BYTES:
            decoder.setstate(prior_state)
            break
        if decoded:
            content_parts.append(decoded)
        content_bytes += decoded_bytes
        processed_bytes += 1

    pending_bytes = len(decoder.getstate()[0])
    consumed_bytes = processed_bytes - pending_bytes
    if at_eof and processed_bytes == len(data):
        prior_state = decoder.getstate()
        decoded_tail = decoder.decode(b"", final=True)
        decoded_tail_bytes = len(decoded_tail.encode(encoding="utf-8"))
        if content_bytes + decoded_tail_bytes <= READ_FILE_MAX_CONTENT_BYTES:
            if decoded_tail:
                content_parts.append(decoded_tail)
            consumed_bytes = processed_bytes
        else:
            decoder.setstate(prior_state)

    return "".join(content_parts), consumed_bytes


def _read_new_file_content_page(*, path: str, cursor: int) -> tuple[int, str]:
    """Read one FIFO page after a raw-byte cursor without discarding backlog."""
    target = Path(path)
    if not target.exists():
        return cursor, ""
    observed_size = target.stat().st_size
    if observed_size <= cursor:
        return cursor, ""

    with target.open(mode="rb") as handle:
        handle.seek(cursor)
        data = handle.read(READ_FILE_MAX_CONTENT_BYTES)
    if not data:
        return cursor, ""
    at_eof = cursor + len(data) >= observed_size
    content, consumed_bytes = _decode_incremental_utf8_page(data=data, at_eof=at_eof)
    new_cursor = cursor + consumed_bytes
    if new_cursor >= observed_size:
        return new_cursor, content

    separator = "" if content.endswith("\n") else "\n"
    result = (
        f"{content}{separator}[read_new_file_content page: bytes [{cursor}, "
        f"{new_cursor}) of {observed_size}; more content is available; call again "
        f"with the same path.]"
    )
    return new_cursor, result


async def _read_new_file_content_with_state(
    *, path: str, file_cursors: dict[str, int], file_locks: dict[str, asyncio.Lock]
) -> str:
    """Read an incremental page using caller-owned cursor and lock state."""
    lock = file_locks.setdefault(path, asyncio.Lock())
    async with lock:
        cursor = file_cursors.get(path, 0)
        new_cursor, result = await asyncio.to_thread(
            _read_new_file_content_page, path=path, cursor=cursor
        )
        if new_cursor != cursor:
            file_cursors[path] = new_cursor
        return result


async def read_new_file_content(path: str) -> str:
    """Read one bounded page of newly appended content using module-level state."""
    return await _read_new_file_content_with_state(
        path=path, file_cursors=_file_cursors, file_locks=_file_locks
    )


FS_TOOL_SCHEMAS = [
    (READ_FILE_SCHEMA, read_file),
    (WRITE_FILE_SCHEMA, write_file),
    (APPEND_FILE_SCHEMA, append_file),
    (EDIT_FILE_SCHEMA, edit_file),
    (MULTI_EDIT_FILE_SCHEMA, multi_edit_file),
    (LS_SCHEMA, ls),
    (GLOB_SCHEMA, glob),
    (GREP_SCHEMA, grep),
    (READ_NEW_FILE_CONTENT_SCHEMA, read_new_file_content),
]


def build_fs_tool_bindings() -> list[tuple[dict, Callable[..., Awaitable[str]]]]:
    """Build per-run file system tool bindings.

    Only read_new_file_content needs per-run closure state (cursor tracking).
    All other tools are stateless and reuse the module-level functions.
    """
    file_cursors: dict[str, int] = {}
    file_locks: dict[str, asyncio.Lock] = {}

    async def _read_new_file_content(path: str) -> str:
        """Read one bounded incremental page using this binding set's state."""
        return await _read_new_file_content_with_state(
            path=path, file_cursors=file_cursors, file_locks=file_locks
        )

    return [
        (READ_FILE_SCHEMA, read_file),
        (WRITE_FILE_SCHEMA, write_file),
        (APPEND_FILE_SCHEMA, append_file),
        (EDIT_FILE_SCHEMA, edit_file),
        (MULTI_EDIT_FILE_SCHEMA, multi_edit_file),
        (LS_SCHEMA, ls),
        (GLOB_SCHEMA, glob),
        (GREP_SCHEMA, grep),
        (READ_NEW_FILE_CONTENT_SCHEMA, _read_new_file_content),
    ]
