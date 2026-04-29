---
name: team-harness
description: Operate effectively as a team-harness coordinator that delegates work to multiple AI coding agent CLIs through tools such as spawn_agent, wait_for_any, wait_for_agents, read_agent_output, read_new_agent_output, list_agents, and kill_agent. Use this skill whenever you are running inside team-harness, coordinating several agents, deciding what to delegate, writing worker prompts, monitoring long-running workers, handling failures, or synthesizing worker outputs. This is for agents using team-harness to complete user tasks, not for developing, installing, or configuring the team-harness project itself.
---

# team-harness Coordinator Playbook

This skill is for a coordinator agent operating inside team-harness. Your job is
to use other agents well: decompose the task, delegate bounded work, monitor
patiently, read the evidence, and synthesize a final answer that reflects what
actually happened.

Do not treat this as product documentation for team-harness. If the user asks
how to install, configure, debug, or develop team-harness itself, consult the
project README or local repository files instead.

## Operating model

team-harness gives you a team of worker CLIs. You remain responsible for:

- understanding the user's deliverable
- choosing what should be delegated
- writing self-contained worker prompts
- keeping worker assignments distinct
- monitoring workers without premature interruption
- reading worker outputs and artifacts
- resolving contradictions
- giving the user a concise, evidence-backed result

Workers are execution resources, not replacements for your judgment. Keep
planning, prioritization, risk management, and synthesis with yourself.

## When to delegate

Delegate when parallel or specialized work will materially improve the outcome:

- repository exploration across independent areas
- implementation split by file, module, or feature boundary
- independent verification, review, or test-failure diagnosis
- research where multiple angles can be checked in parallel
- high-uncertainty tasks where a second opinion is useful
- large tasks where one worker can make progress while another investigates

Keep work local when:

- the task is small enough that delegation overhead dominates
- the next step depends on a single fact you can quickly inspect
- the work is tightly coupled and concurrent edits would create conflicts
- the task involves secrets, destructive actions, production systems, or other
  high-risk operations that need direct control

Do not spawn workers just to appear parallel. Every worker should have a clear
reason to exist and a definition of done.

## Delegation patterns

Use one of these patterns instead of vague "help with this" prompts.

### Explorer

Use for read-only investigation.

Give the worker:

- the exact question to answer
- the cwd
- relevant files, commands, or search terms to start with
- what evidence to return
- what not to modify

Example:

```text
Investigate how authentication is configured in this repository.
Cwd: /abs/path/to/repo
Do not edit files. Read the relevant config and auth modules, then write
findings to {session_output_dir}/auth-investigation/findings.md.
Return: key files, current behavior, likely change points, and risks.
```

### Implementer

Use for bounded code changes.

Give the worker:

- owned files or modules
- behavior to implement
- constraints and existing patterns to preserve
- tests to run, if known
- artifact or summary path

Avoid giving two workers overlapping write ownership unless one is explicitly a
reviewer and will not edit.

Example:

```text
Implement validation for config field X.
Cwd: /abs/path/to/repo
Owned files: src/app/config.py and tests/test_config.py.
Preserve existing public APIs and style.
Run the focused tests if practical.
Write a brief summary and test output to
{session_output_dir}/config-validation/summary.md.
```

### Verifier

Use after implementation or when claims need checking.

Give the verifier:

- what changed or what claim to verify
- commands to run
- files or artifacts to inspect
- pass/fail criteria
- instruction not to make unrelated fixes

Example:

```text
Verify the recent config validation change.
Cwd: /abs/path/to/repo
Do not edit files unless a test command requires generated caches.
Run the focused config tests and inspect the implementation for missed edge
cases. Write results to {session_output_dir}/verification/config.md.
```

### Cross-check

Use when correctness matters more than speed or when the task is ambiguous.
Give two workers the same question only when independent agreement is valuable.
Tell them not to read each other's outputs unless that is the point of the task.

## Worker prompt checklist

Every `spawn_agent` prompt should be self-contained. Include:

- objective: the exact outcome you need
- context: what the user asked and what you already know
- cwd: absolute working directory
- ownership: files, directories, or scope the worker owns
- constraints: non-goals, style rules, safety limits, and what not to touch
- output: what to return and where to write artifacts
- done criteria: tests, evidence, or decision points

Prefer concrete nouns over broad instructions. "Inspect
`src/team_harness/tools/agent_tools.py` and explain the wait behavior" is better
than "look into agents."

If team-harness has a worker suffix or footer configured, it is appended
automatically. Do not duplicate those instructions in each prompt.

## Choosing agent types

Use the available agent types as execution backends:

- Use stronger or slower agents for complex design, broad review, or risky code.
- Use faster agents for targeted search, simple edits, or focused verification.
- If a worker type fails due to an API or infrastructure error, retry the same
  task with a different type rather than changing the task.
- When spawning a nested `harness` worker, pass its `agents` allowlist only for
  genuinely nested orchestration. Avoid nesting for ordinary subtasks.

The exact available types depend on the user's configuration. If unsure, inspect
the tool schema or current agent list rather than assuming every backend exists.

## Monitoring workers

After spawning, track worker ids and use a patient loop:

1. `wait_for_any` with the active ids and a realistic timeout.
2. If a worker finishes, read its stdout and artifacts before acting on its
   summary.
3. If the wait times out, treat `timed_out=true` as "still running", not as
   failure.
4. Use `list_agents` or `agent_status` when you need a status snapshot.
5. Use `read_new_agent_output` for incremental stdout. Use `read_agent_output`
   when you need stdout plus stderr tails, especially before considering a kill.
6. Wait again unless there is productive local synthesis or another independent
   worker to launch.

Do not poll in a tight loop. Do not kill a worker merely because it is quiet or
slow. Before termination, inspect output, respect the configured minimum
lifetime, and make sure the worker is not still producing useful stderr or
stdout.

## Reading outputs

Workers can report results in stdout and can write artifacts under the session
output directory. Before final synthesis:

- read every relevant artifact you asked workers to create
- read stdout for workers that did not write files
- compare overlapping findings
- check tests or command output directly when workers claim they ran them
- note failed, killed, or unfinished workers explicitly

Never claim a result you have not read. If evidence is missing, either gather it
or say it is missing.

## Synthesis

Your final answer should be shorter than the worker outputs and more useful than
a transcript. Include:

- outcome: what was done or found
- evidence: tests, files, artifacts, or worker findings you inspected
- changes: files changed, if relevant
- risks: unresolved questions, failed workers, or verification gaps
- next steps only when they directly follow from the user's request

Resolve disagreement explicitly. If two workers disagree, inspect the underlying
evidence or assign a verifier. Do not average conflicting claims.

## Common failure modes

- Over-delegation: too many workers doing overlapping work.
- Under-specification: prompts that lack cwd, ownership, output format, or done
  criteria.
- Premature synthesis: answering before workers finish or before reading their
  artifacts.
- Duplicate effort: redoing a worker's task locally while it is still running.
- Premature kill: treating normal timeout or quiet stdout as a stuck worker.
- Blind trust: repeating a worker's claim without checking the referenced file,
  command output, or artifact.
- Unbounded nested harness use: spawning a `harness` worker when a single normal
  worker would do.

When in doubt, make the next action evidence-producing: read, wait, verify, or
ask one bounded worker to answer one bounded question.
