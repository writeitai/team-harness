# Architecture Decision Log

Decisions made while building and reviewing team-harness, recorded with the context and
rationale a future reader — a human, or an agent with **no memory of the conversation that
produced them** — needs to understand each one cold.

Companion docs:
- `CLAUDE.md` — developer reference (commands, architecture map, release process).
- `design/designs/` — self-contained, binding design docs (the detailed form of decisions).
- `design/analysis/` — working notes (may be messy or superseded).
- `README.md` — user-facing framing.

Each entry states the **Decision** (the conclusion, plainly), the **Context** (what problem
it solves or why the question arose), and the **Consequences**. A `**Refined by**` line
records later decisions that modify an earlier one.

> **Some of these are deliberate choices that read like defects to someone skimming the
> code.** They are recorded here precisely so a future agent does not "fix" them by accident.
> team-harness is a library other projects depend on — before changing a behavior below, read
> the entry and consider the consumer impact.

---

## TH-D1. The coordinator orchestrates; workers implement

**Decision.** The **coordinator** is an LLM that plans and delegates by emitting tool calls;
it never implements the work itself. The actual work is done by **workers** — external coding
CLIs (Codex, Gemini, Claude Code, opencode, pi, OpenHands) spawned as subprocesses. The
coordinator's tools spawn workers, read/write files, run shell, and manage a todo list, but
"do the change" always means "delegate to a worker."

**Context.** Mixing planning and implementation in one agent makes long tasks unfocused and
hard to supervise. Separating a thin orchestrating brain from swappable execution engines lets
team-harness be model- and CLI-agnostic, and keeps responsibility legible.

**Consequences.** The coordinator loop (`coordinator/loop.py`) runs until the LLM returns
content with no tool calls. Workers are the unit of real work and the unit of failure. Adding
a new backend means adding an agent template (`agents/template.py`), not touching the loop.

## TH-D2. Workers are one-shot batch subprocesses, not reattachable sessions

**Decision.** Each worker is launched with `asyncio.create_subprocess_exec` as a **one-shot
batch process**: `stdin=asyncio.subprocess.DEVNULL`, `stdout`/`stderr` redirected to **files**,
no controlling TTY (`agents/spawner.py`). It runs to completion and exits. There is no
interactive channel and nothing to "reattach" to.

**Context.** The worker CLIs are invoked in their non-interactive/exec mode: give them a
prompt, let them run, collect the result. This is simpler and more reproducible than driving an
interactive REPL, and it is what the CLIs are designed for in automation.

**Consequences.** Process control lives entirely in the parent's in-memory
`asyncio.subprocess.Process` handle (held by `agents/manager.py`'s `AgentManager`). **If the
parent dies, you cannot re-adopt a running worker** — there is no stdio to reconnect and no way
to rebuild the asyncio transport; the worker's output is in its log files, but there is no
control channel. Continuing a worker's *work* after an interruption is therefore a *new*
process that resumes the logical session (TH-D4), never a reattachment. This is the premise
behind TH-D5.

## TH-D3. A normal `run()` return means "the loop ended cleanly," not "every worker succeeded"

**Decision.** `TeamHarness.run()` returns a `TeamHarnessResult` when the coordinator loop ends
without a *terminal* error; it raises `TeamHarnessError` only on terminal failures (API/retry
exhaustion, or a recorded `run_log.error`) — see `harness.py`. A **failed worker does not by
itself fail the run**: failed workers survive as entries in `TeamHarnessResult.agents`, and the
coordinator may legitimately finish after a worker failed (synthesize an answer, route around
it, decide it has enough).

**Context.** The coordinator is an orchestrator, not a build system. Whether the *task* was
truly accomplished is a judgment the coordinator (and the caller) make from the workers'
outputs — it is not mechanically decidable from "the loop returned."

**Consequences.** **Consumers must not equate a normal return with task success.** They should
inspect `TeamHarnessResult.agents` (statuses, exit codes) and apply their own acceptance
criteria. `loopy-loop` depends on exactly this contract (its own decision log records that an
iteration's mechanical completion is distinct from the work being good). Changing this — e.g.
making a failed worker raise — is a breaking change to the consumer contract (AGENTS.md Rule 3).

## TH-D4. Worker continuity is via captured session id + resume, not process adoption

**Decision.** team-harness captures each worker's vendor **session id** from its output
(`agents/session_capture.py`) and records resume capability per agent type
(`tracking/worker_sessions.py`, `WorkerResumeInfo`). To continue a worker's work, you spawn a
**new** process that resumes that logical session (the CLIs' `--resume`/`continue` modes) —
never by reattaching to a still-running process (which TH-D2 makes impossible).

Within the same live harness run, a coordinator resumes with
`spawn_agent(mode="resume", resume_from_agent_id="<agent id>")`. The harness resolves that
agent's captured vendor session internally and rejects the request before spawning unless the
source exists in this run, is terminal, has the same agent type, and has a captured session id.
`resume_from_session_id` remains the explicit interface for a raw id obtained from a finalized
`worker_sessions.json`, including a prior run. Both selectors require `mode="resume"`, and the
two selectors are mutually exclusive.
team-harness never silently turns a failed resume into a fresh spawn; the coordinator must make
that fallback explicit with a self-contained prompt.

**Context.** Long tasks and crashes need a way to pick up where a worker left off. The vendor
CLIs already persist their own session state; capturing the id lets team-harness re-enter that
state cleanly. A live coordinator previously had to provide the raw vendor id even though
`worker_sessions.json` is finalized only after the coordinator loop ends and `list_agents`
exposes harness agent ids, not vendor ids. In a real run the coordinator guessed a plausible
Codex thread id, created a process that failed with `no rollout found`, and then had to route
around it with a fresh worker. Resolving the live agent id inside `tools/agent_tools.py` removes
that guessing step without exposing or duplicating provider-specific state in the prompt.

**Consequences.** Process *identity* (is it still running?) and session *continuity* (resume
the work) are two different concerns handled by two different mechanisms — resume by session id
(this decision), and process reaping by pid/pgid (TH-D5). Not every backend supports resume
(`WorkerResumeInfo.supported`); callers must handle the unsupported case. Same-run lookup is
additive and does not change raw-id resume. Invalid source references create no worker process or
agent record. A rejected vendor resume remains a visible failed worker under TH-D3: automatic
fresh retry would be unsafe because a short continuation prompt may depend on context that exists
only in the old vendor session.

## TH-D5. Persist worker process identity and spawn workers in their own process group, to enable orphan reaping

**Decision.** team-harness will **persist each worker's process identity** — `pid`, process
group id (`pgid`), and process `starttime` — into the per-run worker-session manifest
(extending `WorkerSessionRecord` / `WorkerSessionsManifest`, which already persists rich
per-agent metadata but no process identity today), and will **spawn each worker with its own
process group** (`start_new_session=True`). It will expose a **reap** operation that, given a
prior run's manifest, kills any still-alive worker process group — verifying identity by
`(pgid, starttime)` so a recycled id is never killed.

**Context.** The `AgentManager` handle that lets team-harness kill a worker is **in memory
only**; nothing durable records which OS processes a run launched. Because workers are one-shot
subprocesses in the parent's process group (TH-D2), a **hard crash of the parent** (OOM,
SIGKILL, panic) reparents the workers to init and leaves them running — still spending money,
still writing to the target checkout — with nothing tracking them and no startup cleanup
anywhere. Re-adopting them is impossible (TH-D2), so the only durable fix is *prevent and
reap*: know what was launched, and be able to kill leftovers on restart.

**Consequences.**
- The existing worker-session manifest gains `pid`/`pgid`/`starttime`; spawning gains
  `start_new_session=True` so a whole worker (and any nested sub-workers, up to the configured
  `max_depth`) is one killable group.
- Identity + a durable `is_group_alive(pgid, starttime)` liveness check turns the restart
  decision into a **policy per orphan**, not a hardcoded kill: **drain (bounded)** — wait up to a
  timeout for the worker to finish, then finalize its manifest record from the completed output
  files (a worker record and its repo edits — never a fabricated run result; the dead
  coordinator's outcome is not reconstructed); **reap** (SIGTERM→grace→SIGKILL) —
  the escape for force-stop / hung-past-timeout / unsafe-to-finish; or **ignore**. `starttime`
  verification guards every path against pid reuse. team-harness stays mechanism-neutral and does
  not hardcode the default; the recommended default for a cost-conscious, git-is-truth consumer
  is **bounded drain** (it avoids wasting near-complete work and half-applied edits, and the
  serialization objection is moot because draining happens during recovery before new work is
  dispatched). This extends `AgentManager.kill()` from in-memory-only to persisted-and-reapable.
- **Consumer contract (loopy-loop):** the consumer owns *its own* process liveness (e.g. a
  worker pid + heartbeat) and, on crash recovery, chooses a policy per orphan for the interrupted
  run before starting fresh. team-harness provides the manifest, the liveness check, and the
  policy operations; the consumer decides which and when. See
  `design/designs/process-lifecycle-and-reaping.md` for the full contract.
- **Cross-platform:** `start_new_session` + `os.killpg` work on macOS (dev) and Linux (prod);
  cgroup-based supervision would be more bulletproof but is Linux-only and is a documented
  non-goal for now.
- This does **not** add process *adoption* (TH-D2 still holds) — it adds cleanup of the
  processes a dead parent left behind.

Detailed design: `design/designs/process-lifecycle-and-reaping.md`.

## TH-D6. Per-spawn model/effort overrides fail loudly and are audited; the harness never second-guesses the choice

**Decision.** The coordinator may override a worker's model and reasoning effort per spawn
(`spawn_agent(model=…, effort=…)`); the explicit argument always wins over the agent
template's default. Two invariants govern the feature:

1. **Fail loudly, never silently.** An override that cannot take effect as requested
   returns a coordinator-visible ERROR from `spawn_agent` instead of being dropped or
   double-applied: an agent type whose template cannot carry an effort value (no
   `reasoning_effort_flag` with an `{effort}` placeholder — see
   `template_supports_effort()` in `agents/template.py`), a blank level, or a raw
   `flags` entry that carries the same reasoning-effort option the override would render.
2. **The audit trail claims only what actually happened.** Each spawn records
   `requested_model`/`requested_effort` (the coordinator's explicit arguments; null =
   left to the template default) and `effective_model`/`effective_effort` (what was
   actually injected after resolution) on its `run.json` agent record. `effective_model`
   is null when the template has no model-injection surface (`model_flag` /
   `model_env_vars`), and for env-only templates it accounts for caller `env` overrides
   winning the spawn-env merge (null when the override is partial or conflicting) — see
   `_recorded_model()` in `agents/spawner.py`.

**Context.** Built for loopy-loop's model-tier policy (loopy D9): strong harness
coordinators choose cheaper or stronger workers per task from named tiers, and an *outer
reviewer* — not the engine — verifies that e.g. a review actually ran on the strong tier.
That consumer makes honest audit fields the load-bearing part of the feature: a recorded
effort the worker never received, or a model claim for a template that injects nothing,
is worse than no record at all. The same reasoning rejects silent drops: a coordinator
that believes it escalated when it didn't will happily mark the work reviewed.

**Consequences.** The harness validates *renderability*, never *policy* — there is no
model allowlist, no cost fence, no per-depth restriction; whether a choice was wise is
the consumer's judgment call over the audit fields. A template whose
`reasoning_effort_flag` lacks the `{effort}` placeholder now renders nothing rather than
a valueless option (the level never reached the worker either way; the old behavior
could make the CLI eat the next token as the option's value). Anyone adding a new
injection surface to templates must extend `_recorded_model()`/`template_supports_effort()`
so the audit fields keep telling the truth.

## TH-D7. Embedded callers negotiate an explicit run and delegation contract

**Decision.** team-harness exposes named caller capabilities and an additive
`CallerContext`. A caller that selects the v1 contract supplies an absolute
caller-owned trace root plus parent attempt/session/assignment identity. The
harness writes one self-contained run-id child there and returns the canonical
`run.json`, session-output, and generated-coordinator-input paths on success and
structured failure. It persists generated coordinator system/user input before
the first provider operation and automatically writes an assignment envelope
for every direct spawn. Coordinators choose free-form delegated role, task id,
expected outputs, state responsibility, model, effort, and topology; the harness
records those choices but does not turn them into an allowlist or policy gate.

Structured traces preserve the exact coordinator input, direct assignments,
worker prompts, commands, stdout/stderr paths, and provider session identifiers.
JSON artifacts are replaced atomically, and worker streams go directly to the
canonical caller-owned log files. The final run snapshot awaits the worker
watcher and provider session-id capture task, including its last stdout
prefix/tail scan. Worker shutdown is bounded by the caller's configured natural
exit timeout plus a named one-second SIGTERM grace period; the outer bound
includes both so it does not preempt graceful termination. The retained
watcher/capture phase separately uses the configured shutdown timeout. On
timeout the harness uses the process-group id it created to SIGKILL any unreaped
worker without depending on a process-table probe. That releases watchers
blocked in `proc.wait()`; the harness then cancels and settles its own
cancellation-cooperative watcher/capture tasks so `asyncio.run()` teardown does
not inherit pending work. Exceptions and phase-specific timeouts are collected
rather than allowed to bypass cleanup; their classes and exact messages are
persisted because these are raw caller-owned traces, not sanitized export
artifacts. `run.json` and `worker_sessions.json` are written first, then the SDK
raises a structured `TeamHarnessError` containing their canonical caller-owned
paths.

The generated coordinator and worker footers name the harness run lineage. For
the built-in `type=harness`, team-harness propagates a validated
`TEAM_HARNESS_CALLER_CONTEXT` envelope. A nested coordinator gets its direct
assignment, its own trace root, and the parent harness run id while retaining
the same outer session/depth/workflow identity. This is lineage and context,
not a new loop layer or a delegation constraint. Arbitrary workers that launch
`th` independently are outside this automatic propagation contract.

**Context.** Embedding consumers such as loopy-loop own a durable session tree,
but team-harness previously kept the complete coordinator `run.json` in its
global private directory while worker artifacts lived under caller output. The
SDK returned only a run id, forcing consumers to guess internal paths and making
ordinary success, recovery, usage, and future trace export disagree. The
coordinator and workers also depended on prompt authors remembering outer-loop
identity and absolute paths.

**Consequences.** Capability names, not package-version guesses or signature
inspection, are the compatibility boundary. Context-aware runs are
self-contained and every direct agent has durable authored/effective input plus
its place in the outer ecosystem. Accountability remains prompt-and-evidence
based, consistent with TH-D1; no static agent graph, path ACL, model policy, or
semantic acceptance gate is introduced. Legacy callers keep the old layout and
the original three required `TeamHarnessResult` fields. In caller-context runs,
persisted process identity names the worker process-group leader; wait, kill,
session capture, and orphan reap cover that execution group. Captured artifacts
are an exact operational record; the caller owns access, retention, and any
transformation before external export.
Nested harness lineage is automatic only on the explicit built-in harness spawn
path, so the implementation does not guess process ancestry. Full contract:
`design/designs/embedded-caller-run-and-spawn-contract.md`.

## TH-D8. Coordinator shell commands have explicit whole-command deadlines

**Decision.** The coordinator's `bash` tool keeps its historical 120-second
default and accepts an optional positive integer `timeout_seconds`. That value
is the deadline for the entire foreground shell command, including all of its
sequential child work. There is no arbitrary maximum: a caller must be able to
derive a truthful batch deadline from the work it is invoking. Every command
starts in a new process group. Timeout, tool cancellation, or another
post-spawn execution failure sends SIGTERM to the group, waits one named short
grace period, then sends SIGKILL if the group leader remains and reaps the shell
before the tool returns or re-raises. The leader check prevents a freed process
group id from being signalled after the operating system recycles it.

**Context.** The old fixed 120-second deadline was shorter than legitimate
foreground tools while being invisible to their own timeout settings. For
example, an `eval-banana` command may run five or eighty-four checks in
sequence, each with its own judge timeout. Giving each judge 10,800 seconds
does not extend the outer shell call. The shell used to terminate that batch at
120 seconds, after partial prompt files but before the aggregate report, so a
coordinator could neither finish the evaluation nor publish an honest verdict.
Backgrounding the command and guessing its PID would weaken lifecycle cleanup
and output/error attribution rather than fix the contract.

**Consequences.** Existing calls are byte-compatible at the default, including
the timeout error text. A coordinator that knowingly invokes a long batch must
pass a named deadline large enough for the complete foreground operation; the
tool-call arguments and result already remain in `run.json`. A timeout is an
interrupted command, not evidence that the command's semantic task failed.
Because a hard parent-process crash can still outlive in-memory tool cleanup,
durable registration and recovery of arbitrary shell commands remains a
separate future concern; this decision does not turn shell commands into
TH-D2 workers. Full contract:
`design/designs/coordinator-shell-command-lifecycle.md`.

## TH-D9. Coordinator artifact reads are path-driven, bounded, and pageable

**Decision.** Caller envelopes and prompts provide absolute artifact paths, not
artifact contents. The two coordinator tools that can return general file
content are bounded at that path boundary:

- `read_file` returns at most 32,768 decoded characters and 32 KiB after UTF-8
  encoding, plus short metadata. Its named `offset_chars` and `limit_chars`
  arguments provide explicit random-access pagination.
- `read_new_file_content` returns at most the same content limits from its
  per-run append cursor. It preserves unread backlog in FIFO order and tells
  the coordinator to call again with the same path when more is available.

Both tools report the range returned by a partial page and never permit a
content page above either fixed maximum. Small reads still return their exact
contents without a wrapper. The complete source file remains untouched and
available for further pages or a coordinator-chosen focused projection such
as `jq`.

**Context.** A loopy-loop eval runner received only the absolute path to a
1.9 MiB canonical report, then reasonably called `read_file` to inspect it.
The old tool returned the complete file. Because the report embedded verbose
judge transcripts, that one result expanded the next coordinator request past
the model's effective context limit. The five checks had already passed, but
the coordinator failed before it could publish the receipt and goal-check
output. Context tracking could not compact between a tool result and its
required follow-up request, so path-only prompting was not sufficient while
the file tool itself was unbounded. Review of the repair found that
`read_new_file_content` had the same risk on its first call: its cursor began at
zero and it read to EOF. Bounding only `read_file` would therefore have left a
second core path for the same failure.

**Consequences.** The coordinator remains autonomous: the harness does not
choose semantic fields, summarize evidence, or inject file contents before a
tool call. It only makes each read fit the transport it must traverse. Agents
may page sequentially, request a smaller page, grep for a target, or run a
structured projection. Existing small-read consumers are byte-compatible;
consumers that expected one call to return more than 32,768 characters or
32 KiB after UTF-8 encoding must follow `read_file`'s continuation offset or
call `read_new_file_content` again with the same path. Full contract:
`design/designs/bounded-coordinator-file-reading.md`.
