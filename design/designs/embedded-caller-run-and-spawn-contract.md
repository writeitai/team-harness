# Design: Embedded Caller Run Records and Direct-Spawn Assignments

**Status:** Implemented
**Date:** 2026-07-15
**Decision:** `design/decisions.md` TH-D7
**Primary consumer:** `loopy-loop`, while the API is generic for any embedding
caller that owns a larger session or workflow state machine.

## The problem

The SDK historically split one run across two private locations. The complete
coordinator record lived at `~/.team-harness/runs/<run-id>/run.json`, while
worker logs and `worker_sessions.json` lived at `<output_dir>/<run-id>/`. An
embedding caller therefore could not identify one self-contained canonical run
record without knowing team-harness internals. The SDK result exposed only the
run id, so success and failure paths also required callers to reconstruct paths.

The harness coordinator received the user's task, but did not receive typed
identity from an outer session tree. Its direct `spawn_agent` calls carried only
a free-form prompt. A coordinator could explain the outer workflow to a worker,
but nothing guaranteed that every spawn got the parent attempt, session depth,
absolute assignment path, output location, or an auditable task/role label.

The caller also needed the generated coordinator input, each direct assignment,
worker stdout/stderr, and provider session identifiers to remain together and
discoverable after the in-memory coordinator transcript was gone.

## Public capability negotiation

`team_harness.caller_contract` exports `get_capabilities()` and
`TEAM_HARNESS_CAPABILITIES`. Callers negotiate named semantics rather than
guessing from the installed package version or inspecting constructor
parameters. Caller-contract version 1 advertises:

- `caller_run_record_v1` — a caller can supply an absolute trace root and gets
  the canonical `run.json` path on both success and structured failure;
- `coordinator_input_v1` — generated system/user input is persisted atomically
  before client construction or model discovery;
- `spawn_assignment_v1` — every direct spawn receives an automatic assignment
  envelope and effective prompt footer; and
- `nested_caller_context_v1` — a built-in `type=harness` descendant receives a
  validated outer caller-context envelope and parent harness run lineage.

Names, not the integer contract version, are the compatibility gate. A future
build may add a capability without changing the meaning of these four.

## Caller context and canonical paths

An embedding caller passes the additive SDK argument
`TeamHarness(caller_context=CallerContext(...))`. `CallerContext` requires:

- an absolute caller-owned `trace_root`;
- the absolute `parent_assignment_path`;
- parent attempt, root session, and current session identifiers;
- the current session depth and workflow role; and
- optional absolute `relevant_state_paths`.

`parent_harness_run_id` is optional. An embedding caller such as loopy-loop
omits it for the first harness coordinator. Team-harness fills it when it
derives the context for a nested harness coordinator.

For each invocation, `TeamHarness.run()` creates
`<trace_root>/<run-id>/`. That run-id child is the canonical run and artifact
directory. It contains `run.json`, `coordinator_input.json`,
`worker_sessions.json`, `workers/` logs, and `agents/` assignment envelopes.
The child avoids destructive collisions when a caller retries a logical
attempt. `TeamHarnessResult` returns `run_json_path`, `session_output_dir`, and
`coordinator_input_path`; `TeamHarnessError.detail` returns the same fields.
Callers must consume those explicit paths rather than reconstruct them.

Legacy callers that omit `caller_context` keep the existing split layout and
may continue constructing `TeamHarnessResult` with only `text`, `agents`, and
`run_id`. The new result fields have empty-string defaults.

## Coordinator input and identity

`TeamHarness.run()` now generates its system and user messages, applies the
automatic caller-context system footer, and atomically writes
`coordinator_input.json` before `_make_client()` or
`resolve_model_limit()` can contact a provider. The file contains the exact
logical messages used for the coordinator call.

If configuration or prompt generation fails before a complete system envelope
exists, a context-aware run still returns its canonical paths and writes
`coordinator_input.json` with `status: "incomplete"`, the user task,
and the preflight failure. It does not pretend that an ungenerated system input
was complete. Legacy callers keep their historical exception behavior for
configuration failures.

The automatic system footer tells the coordinator:

- which root/session/depth/workflow assignment it owns;
- where the absolute parent assignment and relevant state paths are;
- the explicit harness run id and where that run is recorded; and
- that spawned agents are delegates while the coordinator remains accountable
  for integration and the loop-layer decision.

This is context and accountability, not a filesystem permission system.

## Dynamic direct-spawn assignment

`spawn_agent` accepts four optional, non-enumerated metadata fields:

- `delegated_role`;
- `delegated_task_id`;
- `expected_outputs`; and
- `state_responsibility`.

The coordinator chooses their values dynamically. Team-harness records them but
does not use them to approve a spawn, choose a model, constrain a path, or judge
the result. Omitting them remains valid for old coordinator prompts, though the
schema asks new coordinators to provide useful values.

Before launching a subprocess, `tools/agent_tools.py` writes
`agents/<agent-id>/agent_assignment.json`. It includes the parent harness run,
outer attempt/session identity when available, absolute parent and per-agent
assignment paths, the agent output directory, relevant state paths, the four
dynamic metadata fields, and both prompt forms:

`assignment_path` always names the spawned agent's own envelope, matching the
same field in `run.json`; `parent_assignment_path` names the outer loopy
assignment. The distinct names prevent a nested coordinator from confusing
its own responsibility with its parent's.

1. `authored_prompt` — exactly what the coordinator delegated; and
2. `effective_prompt` — the authored prompt plus configured suffixes and the
   automatic ecosystem/output footers.

The effective footer points the worker to its own absolute assignment and
output directory, names its parent harness run id, and states that it reports
to the harness coordinator rather than owning the loop-layer decision.
`run.json` retains the same authored and effective forms as the
backward-compatible `prompt` and `full_prompt` fields, plus the assignment path
and delegation metadata. This makes the run id available to eval workers in
their operational prompt, not only in an adjacent file they might forget to
open.

## Prompt, output, and session capture

`tracking/persistence.py` atomically writes structured JSON so a crash cannot
leave a truncated `run.json`, coordinator input, assignment, or worker-session
manifest. `agents/spawner.py` writes worker stdout and stderr directly to the
canonical caller-owned log paths. The run record retains the worker command,
prompt, status, exit code, and those absolute paths; `worker_sessions.json`
adds compact tails and provider-session metadata for recovery and inspection.
Captured artifacts contain the exact operational inputs and outputs. Access
control, retention, and any transformation before external export belong to
the caller that owns the trace directory.

### Provider session-id finalization

Session ids are sometimes emitted only in a provider's final event. Spawned
worker watcher and session-capture coroutines are therefore retained by the
per-run `AgentManager`, rather than launched as fire-and-forget tasks.
`harness._finalize_run()` first makes every worker terminal, then awaits those
tasks. `capture_session_id_from_path()` observes the watcher stop event and
performs one final prefix/tail scan. Only after that scan does team-harness
finalize `run.json` and write `worker_sessions.json`. A caller can consequently
use either final artifact without racing a late provider session id.

A watcher or final session-scan coroutine can itself fail (for example, an OS
error while waiting on the worker), and a process waiter can remain pending
after process-table probing fails. `harness._finalize_run()` therefore gives
the shutdown phase at most the configured `shutdown_timeout_s`. It then gives
the retained watcher/session-capture tasks the same configured bound through
`AgentManager.await_finalization_tasks(timeout_s=...)`. When either deadline
expires, team-harness sends SIGKILL to any still-unreaped worker group using the
process-group id created by this live harness. This does not depend on the
failed process-table probe: the group identity remains trustworthy for the
lifetime of the harness that created it. Killing the worker releases any
watcher blocked in `proc.wait()`. The harness then cancels and settles its own
cancellation-cooperative watcher/capture tasks instead of leaving pending tasks
for `asyncio.run()` to gather during teardown, and continues to write both
durable snapshots.

Lifecycle failures are preserved as a terminal finalization error containing
the exception class and exact exception message. Timeouts additionally name
the phase, configured bound, and unfinished task count. This is intentionally
consistent with the caller-owned trace contract, which captures exact prompts,
commands, and worker output rather than treating trace artifacts as sanitized
export data. After `run.json` and `worker_sessions.json` exist, the SDK's normal
error path raises `TeamHarnessError` with their canonical caller-owned paths.

## Nested harness context propagation

When a caller-context coordinator chooses `spawn_agent(type="harness", ...)`,
`tools/agent_tools.py` derives a new `CallerContext` and writes it to the child
environment as `TEAM_HARNESS_CALLER_CONTEXT`. The child `TeamHarness`
constructor loads and validates it when no explicit SDK context was supplied.
The generated value overrides a free-form `env` value supplied in the tool
call, so lineage cannot accidentally be spoofed or dropped.

The nested context keeps the same parent attempt, root/current session,
session depth, workflow role, and relevant state paths. It changes the parent
assignment to the direct agent assignment, places nested run artifacts under
`<agent-output>/harness_runs/<nested-run-id>/`, and records the current run as
`parent_harness_run_id`. Keeping the loop fields unchanged matters: adding a
harness coordinator is dynamic delegation inside one loop assignment, not the
creation of a new loopy-loop layer. Its coordinator footer explicitly says the
parent harness coordinator retains the loop-layer decision.

This automatic contract is deliberately limited to the built-in
`type=harness` spawn path. A generic worker can execute arbitrary programs; the
harness does not inspect its process tree and guess that it independently
launched `th`. `agents/spawner.py` removes a stale inherited caller-context
variable from generic worker environments, because it would describe the
parent coordinator's assignment rather than that worker's assignment. Such a
worker still has its direct assignment/footer, but any independent nested
harness it creates must be given context explicitly.

## Code map and verification

- `caller_contract.py` — public context, capabilities, and coordinator footer.
- `harness.py` — caller-owned path selection, pre-provider input persistence,
  structured result/error paths, and context propagation.
- `tools/agent_tools.py` — dynamic schema fields, assignment envelope, and
  direct-agent footer.
- `agents/spawner.py` — worker process-group identity and direct stdout/stderr
  capture under the caller-owned run directory.
- `tracking/run_log.py` and `tracking/persistence.py` — atomic structured
  persistence.
- `tracking/worker_sessions.py` — persisted summaries, session metadata, and
  invocation artifacts.
- `tests/test_caller_contract.py` and `tests/test_process_lifecycle.py` —
  capability, path, ordering, failure, assignment, prompt, output,
  session-capture, and process-group coverage.

The contract preserves TH-D1: the coordinator still chooses the team and
workers still implement. It preserves TH-D2 and TH-D3: workers remain one-shot
subprocesses, and a normal harness return still means the coordinator loop
ended cleanly rather than that every worker succeeded.
