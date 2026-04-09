import asyncio
from collections.abc import Awaitable
from collections.abc import Callable
import os
from pathlib import Path

_file_cursors: dict[str, int] = {}
_file_locks: dict[str, asyncio.Lock] = {}

READ_FILE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "read_file",
        "description": "Read a file and return its contents.",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
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
            "Read only the new content appended to a file since the last call "
            "for this path. Useful for incremental reading of worker-generated "
            "artifacts. "
            "Returns empty string if nothing new."
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


async def read_file(path: str) -> str:
    return await asyncio.to_thread(Path(path).read_text, errors="replace")


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


async def read_new_file_content(path: str) -> str:
    lock = _file_locks.setdefault(path, asyncio.Lock())
    async with lock:
        target = Path(path)

        def _read() -> tuple[int, bytes]:
            if not target.exists():
                return 0, b""
            cursor = _file_cursors.get(path, 0)
            size = target.stat().st_size
            if size <= cursor:
                return cursor, b""
            with target.open("rb") as handle:
                handle.seek(cursor)
                data = handle.read()
            return cursor + len(data), data

        new_cursor, data = await asyncio.to_thread(_read)
        if not data:
            return ""
        _file_cursors[path] = new_cursor
        return data.decode("utf-8", errors="replace")


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
        lock = file_locks.setdefault(path, asyncio.Lock())
        async with lock:
            target = Path(path)

            def _read() -> tuple[int, bytes]:
                if not target.exists():
                    return 0, b""
                cursor = file_cursors.get(path, 0)
                size = target.stat().st_size
                if size <= cursor:
                    return cursor, b""
                with target.open("rb") as handle:
                    handle.seek(cursor)
                    data = handle.read()
                return cursor + len(data), data

            new_cursor, data = await asyncio.to_thread(_read)
            if not data:
                return ""
            file_cursors[path] = new_cursor
            return data.decode("utf-8", errors="replace")

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
