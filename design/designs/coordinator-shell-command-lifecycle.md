# Coordinator shell-command lifecycle

Status: binding design, implementing TH-D8.

## Problem

The coordinator uses `bash` for local commands that are not worker-agent
assignments: validation, tests, evaluators, and repository utilities. Before
TH-D8, `tools/shell_tools.py` applied an unconditional 120-second timeout.
That was reasonable for a quick command but incorrect for a known long-running
foreground batch.

Nested timeout flags do not solve this mismatch. Suppose `eval-banana` runs
five checks sequentially and gives each judge up to 10,800 seconds. Its flag
controls one judge subprocess; it does not change Team Harness's outer shell
deadline. Terminating the outer command after 120 seconds can leave prompt
artifacts but no aggregate report. The absence of that report means the run was
interrupted, not that the evaluated work failed.

## Tool contract

`tools/shell_tools.py::bash` accepts:

- `command`: the shell command;
- `cwd`: its working directory, defaulting to the current directory; and
- `timeout_seconds`: a positive integer deadline for the complete foreground
  command, defaulting to 120 seconds.

The default and its error text preserve existing callers. The schema has no
arbitrary maximum because a batch deadline must cover the number of sequential
operations it contains. It is still bounded: every call has one explicit,
finite deadline. Invalid booleans, non-integers, and values below one fail
before a subprocess is created.

The deadline belongs to the outer shell call. It is distinct from any timeout
inside the command. A coordinator running a sequential evaluator therefore
derives the outer value from the validated inventory. For example, with `N`
checks whose per-check ceiling is 10,800 seconds, the caller can invoke the
tool with `timeout_seconds=N * 10800 + 600`, leaving ten minutes for validation,
report assembly, and process overhead.

The command stays in the foreground. Coordinators must not replace a truthful
tool deadline with `nohup`, shell backgrounding, or PID polling: those patterns
detach command completion and errors from the tool result and make cleanup
less reliable.

## Process lifecycle

Each command is created with `start_new_session=True`, making its shell the
leader of a dedicated process group. This matters because many command-line
programs create their own children. Killing only the shell can leave the real
work running and writing after Team Harness has declared a timeout.

When the deadline expires, the coordinator tool is cancelled, or execution
otherwise fails after spawn, Team Harness:

1. sends SIGTERM to the complete command process group;
2. waits up to the named one-second termination grace period for the group
   leader;
3. if that leader remains, sends SIGKILL to its still-owned process group; and
4. awaits the shell so it is reaped.

The leader check is a safety boundary. Once the leader exits, its numeric
process-group id can be recycled for an unrelated process; Team Harness must
not send a delayed SIGKILL to that id. SIGTERM was already delivered to every
original member. A descendant that deliberately ignores SIGTERM while its
leader exits is not safely attributable after that point and is part of the
hard-crash/durable-registration limitation below.

Timeout returns the existing coordinator-visible `ERROR` string with the
actual deadline. Cancellation and unexpected exceptions are re-raised after
cleanup so the harness cannot fabricate a successful tool result.

## Evidence and caller responsibilities

The existing coordinator loop records the complete tool-call arguments and
result in `run.json`; no parallel trace schema is needed. Combined stdout and
stderr retain the existing 32 KiB return limit.

The caller must interpret the invoked tool's own completion artifact. For an
evaluator, a complete, valid report can prove semantic pass, semantic failure,
or evaluator error. A missing report after the outer tool timed out proves only
interruption. The caller must not manufacture report bytes, hashes, receipts,
or semantic conclusions for that attempt; it should let the infrastructure
attempt fail visibly and retry in a fresh output location.

## Deliberate boundary

This mechanism is for synchronous coordinator tools, not worker agents. Worker
lifecycle, durable session capture, and crash reaping remain governed by
TH-D2, TH-D4, TH-D5, and `process-lifecycle-and-reaping.md`.

If the Team Harness parent is killed without running cancellation cleanup, an
arbitrary shell command is not yet durably registered for later reaping. Adding
that recovery surface would require persisted identities and policy comparable
to worker reaping; it is explicitly outside this focused correction.

## Code and verification map

- `src/team_harness/tools/shell_tools.py` defines the schema, validates the
  deadline, starts the process group, and owns cleanup.
- `src/team_harness/tools/registry.py` forwards the coordinator's named tool
  arguments to `bash`.
- `src/team_harness/coordinator/loop.py` records tool arguments and results in
  the normal run trace.
- `src/tests/test_shell_tools.py` covers compatibility, explicit long
  deadlines, invalid values, process-group cleanup, and cancellation.
