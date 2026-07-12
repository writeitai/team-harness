# AGENTS.md — working agreement for this repo

`team-harness` is a coordination layer for other coding harnesses: a **coordinator LLM**
receives a task, breaks it into work units, and delegates execution to **worker CLIs**
(Codex, Gemini, Claude Code, opencode, pi, OpenHands, …) spawned as subprocesses. It is a
library and CLI; **other projects depend on it** (notably `loopy-loop`).

`CLAUDE.md` is the developer reference (commands, architecture, release process). **This
file is the working agreement** — how to make changes here. `design/decisions.md` is the
canonical record of *why the system is the way it is*. Three things are non-negotiable.

## Rule 1 — Record decisions, and respect the deliberate ones

`design/decisions.md` is the Architecture Decision Log (TH-D1, TH-D2, …). When you make a
non-obvious architectural choice — or reverse or refine an existing one — **add or update an
entry** (Decision / Context / Consequences). A future agent with no memory of this session,
or a human who wasn't here, must be able to understand why.

Some entries record choices that **look like defects if you only skim the code, and exist so
they don't get "fixed" by accident.** Before changing behavior in these areas, read the
entry:

- **TH-D2 — workers are one-shot batch subprocesses, not reattachable sessions.** Don't add
  logic that assumes you can reconnect to a running worker's stdio.
- **TH-D3 — a normal `run()` return means the coordinator loop ended without a terminal
  error, not that every worker succeeded.** Don't make callers infer task success from a
  normal return; failed workers survive as summaries in `TeamHarnessResult.agents`.

If you believe a decision is wrong, propose amending `design/decisions.md` (state what
changes and why) — don't silently contradict it in code.

## Rule 2 — Design and decision docs must be understandable cold, by future agents AND humans

A design or decision doc is read by someone who was **not** in the conversation that produced
it. Write for them.

- **Explain, don't just name.** Naming a mechanism ("session capture", "compaction", "the
  tool registry") is not explaining it. State, in plain language, *what it is, what problem
  it solves, and why we chose it*, with a concrete example where it helps.
- **The reasoning lives in the doc, not in your head.** A decision-log entry may state the
  conclusion tersely; the companion design section must be self-contained.
- **Anchor claims in the code.** Reference the file/function (`agents/spawner.py`,
  `harness.py`, `tracking/worker_sessions.py`) so a reader can verify — but lead with the
  plain-English meaning.
- **Keep `design/` honest about status.** `design/designs/` is binding; `design/analysis/`
  is working notes (may be superseded); a decision-log entry is authoritative. Don't cite a
  note as if it were a decision.

## Rule 3 — Mind the consumer contract

team-harness is a **library other projects build on**. Several surfaces are effectively
public API even though they aren't marked so:

- `TeamHarnessResult` shape and the meaning of a normal return vs. a raised `TeamHarnessError`
  (TH-D3).
- Worker **spawn and lifecycle** behavior — how workers are launched, tracked, and cleaned up
  (TH-D2, TH-D4, TH-D5). `loopy-loop` in particular relies on the persisted worker-session
  manifest and on process cleanup.
- The `config.toml` schema and the agent-template contract.

When you change one of these, treat it as a breaking change unless you can show it isn't:
update `CHANGELOG.md`, and note the consumer impact. Prefer additive changes; when you must
break, say so loudly.

---

`design/` layout: `design/decisions.md` (the log) · `design/designs/` (binding design docs) ·
`design/analysis/` (working notes). `CLAUDE.md` points here for the working agreement and
keeps the dev/architecture/release reference.
