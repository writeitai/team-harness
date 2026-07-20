# Worker rate-limit circuit breaker

## Status and purpose

This is the binding design for the run-scoped worker rate-limit circuit
(TH-D10). It prevents a coordinator from repeatedly launching a provider family
that has already reported a hard quota rejection. It does not retry workers,
change a worker's model, or decide which alternate family should do the task.

For example, Claude Code may finish with these two JSONL records:

```json
{"type":"rate_limit_event","rate_limit_info":{"status":"rejected","resetsAt":1784811600,"overageStatus":"rejected"}}
{"type":"result","is_error":true,"api_error_status":429}
```

Before this design, the failed agent remained visible, but nothing stopped the
next coordinator turn from launching another Claude process. The provider then
rejected that process for the same account-wide reason.

## Detection boundary

`agents/rate_limits.py` reads the stdout file that `agents/spawner.py` already
captures for every worker. Detection runs only after the worker is terminal.
It recognizes either of these explicit signals:

- a JSON object with `type == "result"` and `api_error_status == 429`; or
- a JSON object with `type == "rate_limit_event"` whose nested
  `rate_limit_info.status` or `overageStatus` equals `"rejected"`.

The scanner processes JSONL incrementally, ignores invalid or truncated lines,
and lets the last valid rate-limit signal win. A final 429 result often omits
the reset time, so it retains `resetsAt` from the preceding rejected event.
Text matches, generic API failures, overloads, authentication errors, and task
failures do not open this circuit.

## State and key

`RateLimitCircuitBreaker` lives inside the per-run agent-tool bindings built by
`tools/agent_tools.py`. It indexes evidence by agent-template name (called
`family` in the record) plus the effective model when known. The template family
is the blocking scope: when `claude` is account-rate-limited, selecting another
Claude model generally does not move the work to a different provider account.
The model that actually reached the failed worker is obtained from
`agents/spawner.py`'s effective-model audit value.

The provider's Unix `resetsAt` becomes the circuit expiry. If it is absent or
invalid, the expiry is `tripped_at + rate_limit_default_cooldown_s`. At or after
expiry, an availability check or spawn removes the active entry and the spawn
is allowed as a probe. State is intentionally run-local: a new harness run does
not inherit an old process's in-memory health assumptions.

## Spawn and coordinator contract

Before `spawn_agent` creates an agent id or writes an assignment, it synchronizes
newly terminal workers and checks the requested family. An open circuit returns
a JSON string with this shape:

```json
{
  "spawned": false,
  "status": "rate_limited",
  "family": "claude",
  "model": "claude-fable-5",
  "tripped_at": "2026-07-23T12:58:20+00:00",
  "resets_at": "2026-07-23T13:00:00+00:00",
  "reason": "worker result reported api_error_status=429",
  "requested_model": "claude-fable-5",
  "message": "Agent family 'claude' is rate-limited until ...",
  "available_families": ["codex", "gemini"]
}
```

No subprocess, assignment file, `AgentState`, or `run.json.agents` entry is
created for that rejected request. This preserves the existing spawn success
contract: successful calls still return only an `agent_<id>` string.

The additive `agent_availability` coordinator tool returns the breaker enabled
flag, available family names, active trip records, and one status record per
allowed family. `list_agents` remains an array of processes that were actually
spawned, preserving existing callers.

## Durable audit contract

`tracking/models.py` adds this top-level field to `RunRecord`; therefore every
new `run.json` contains it, including runs with no rate limit:

```json
{
  "rate_limited_families": [
    {
      "family": "claude",
      "model": "claude-fable-5",
      "tripped_at": "2026-07-23T12:58:20Z",
      "resets_at": "2026-07-23T13:00:00Z",
      "reason": "worker result reported api_error_status=429"
    }
  ]
}
```

Entries are audit history, not only the currently active map. They remain after
expiry, and a later independent trip can append another interval. Existing
fields—including per-turn `usage.prompt_tokens` and `usage.completion_tokens`—
are untouched.

## Configuration and compatibility

Two `[coordinator]` keys control the feature:

```toml
rate_limit_circuit_breaker = true
rate_limit_default_cooldown_s = 900
```

The cooldown must be positive. Setting `rate_limit_circuit_breaker = false`
skips detection, persistence, and spawn blocking; `agent_availability` reports
every allowed family as available. This restores the pre-TH-D10 behavior.

The change is additive to `run.json` and the coordinator tool set. It does not
change one-shot subprocess behavior (TH-D2), the meaning of a normal harness
return (TH-D3), worker-session manifests, or existing usage fields consumed by
loopy-loop.
