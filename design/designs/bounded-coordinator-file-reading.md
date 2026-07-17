# Bounded coordinator file reading

**Status:** Binding design, implementing TH-D9.

## Problem

Team Harness deliberately gives its coordinator file paths and tools rather
than preloading repository or trace contents into the prompt. That keeps the
model in control of what it inspects. Before TH-D9, however, both general
content readers in `src/team_harness/tools/fs_tools.py` could return an entire
text file as one tool result. `read_file` always read to EOF, while
`read_new_file_content` read from its append cursor to EOF.

That made a path-only assignment unsafe at the next boundary. In a real
loopy-loop eval-runner attempt, the coordinator opened a 1.9 MiB canonical
evaluation report after all five checks had passed. The report contained the
full provider transcript for each check. One unbounded tool result expanded
the following coordinator request beyond the effective model context, so the
harness failed before the eval receipt and goal-check output were published.
The report was valid and durable; transporting all of it through one model
turn was the defect. Although that incident used `read_file`, the incremental
reader had the same failure mode on its first call because a fresh cursor is
zero. The contract therefore covers both tools.

## Contract

Both readers return at most 32,768 decoded file-content characters and at most
32 KiB after UTF-8 encoding. Short pagination metadata is outside those content
limits. A multi-byte or invalid source may therefore yield fewer characters or
consume fewer raw source bytes than an ASCII page.

### Explicit reads

`read_file` is a random-access character-page interface:

- `path` remains the only required argument and may be absolute or relative.
- `offset_chars` is a named, zero-based decoded-character offset and defaults
  to zero.
- `limit_chars` is a named positive page size. It defaults to 32,768 and may be
  smaller, but never larger than 32,768.
- A file that fits in the initial page is returned exactly as before, including
  the empty string for an empty file.
- A truncated or explicitly offset page appends plain continuation metadata:
  the half-open character range, total decoded length, whether EOF was reached,
  and the next offset when more content remains.
- Invalid pagination is rejected before filesystem access. Booleans are not
  accepted as integers.

Offsets use decoded Python characters after `errors="replace"`, not raw bytes.
That keeps page boundaries legible and avoids splitting a Unicode code point.
The implementation may read the source into process memory; the invariant is
on the returned page that crosses into coordinator context.

### Incremental reads

`read_new_file_content` is a stateful FIFO page interface for files that grow:

- `path` remains its only argument. Each production binding created by
  `build_fs_tool_bindings()` owns an isolated raw-byte cursor per path.
- A call reads from that cursor without skipping backlog. When more observed
  content remains, metadata reports the returned raw-byte range and tells the
  coordinator to call again with the same path.
- The cursor advances only past raw bytes fully represented in the returned
  text. A UTF-8 code point crossing a page boundary is held for the next page,
  rather than split into replacement characters. Invalid input still uses the
  historical replacement-character behavior without exceeding the output cap.
- A new delta that fits returns exactly as before. No new content returns the
  empty string. Metadata appears only while backlog remains.

The incremental reader reads no more than one raw page at a time. Its cursor is
process state, not a second durable trace: the caller-owned file remains the
canonical complete artifact.

## Why a fixed maximum is appropriate here

The shell deadline in TH-D8 has no arbitrary maximum because a legitimate
foreground batch may truthfully require many hours. File-tool output has the
opposite constraint: every returned character must enter the next model
request. A fixed page maximum therefore describes a real transport capacity,
not a policy judgment about how much evidence the agent is allowed to inspect.
The complete file remains available through additional pages.

The harness does not decide which evidence matters and does not replace the
report with a programmatic semantic verdict. For structured data, the
coordinator may choose a focused command such as `jq` and then open individual
supporting fields or pages. That choice stays with the coordinator.

## Compatibility and traces

Small-read return values are unchanged. The `read_file` schema change is
additive because both pagination arguments are optional; the incremental tool
keeps its existing path-only schema. Large callers that depended on a single
complete result must follow the explicit continuation offset, call the
incremental reader again, or select a focused projection.

`run.json` continues to record the exact tool arguments and returned page. The
canonical source file is neither rewritten nor copied by `read_file`, so a
caller-owned trace retains the full evidence independently of what entered the
coordinator context.

## Verification

`src/tests/test_fs_tools.py` covers exact small-file compatibility, explicit
default and smaller pages, empty and exact-boundary reads, continuation/EOF
metadata, Unicode boundaries, invalid UTF-8 expansion, FIFO backlog, schema
bounds, and invalid pagination. `src/tests/test_harness.py` proves per-run
incremental cursors remain isolated. `src/tests/test_loop.py` proves that both
tools reduce a multi-megabyte source to one bounded page in the next model
request and `run.json`. Repository CI additionally runs Ruff, format checking,
Pyright, and the full test suite on every supported Python version.
