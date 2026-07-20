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
It recognizes either of these hard terminal outcomes:

- a JSON object with `type == "result"`, a failure marker (`is_error == true`
  or `status == "error"`), and an explicit 429 status. The status may be a
  numeric field such as `api_error_status`/`error.code`; Gemini stream-json may
  instead retain the explicit `API Error: 429` marker only in that terminal
  result's `error.message`; or
- a JSON object with `type == "rate_limit_event"` whose nested
  `rate_limit_info.status` or `overageStatus` equals `"rejected"`, unless a
  successful terminal result follows it in the same stream.

The scanner processes JSONL incrementally and ignores invalid or truncated
lines. Rejected events are provisional because a worker CLI can report one,
retry internally, and finish successfully. A successful terminal result clears
that evidence; additionally, an exit-zero worker never trips the circuit. A
final failing 429 often omits the reset time, so it retains the latest reset
from the preceding rejected event. Arbitrary output text, generic API failures,
overloads, authentication errors, and task failures do not open this circuit.

Opening stdout can fail transiently while a watcher and another status tool are
synchronizing. `tools/agent_tools.py::_sync_finished_rate_limits` sets
`AgentState.rate_limit_checked` only after the file scan returns successfully.
An `OSError` is fail-open for that call but leaves the worker retry-eligible on
the next spawn, wait, status, list, or availability synchronization.

## State and key

`RateLimitCircuitBreaker` lives inside the per-run agent-tool bindings built by
`tools/agent_tools.py`. Its active map is keyed by agent-template name (called
`family` in the record). The template family is the blocking scope: when
`claude` is account-rate-limited, selecting another Claude model generally does
not move the work to a different provider account. The model that actually
reached the failed worker remains audit metadata obtained from
`agents/spawner.py`'s effective-model value.

The provider's Unix `resetsAt` becomes the circuit expiry. If it is absent or
invalid, the candidate expiry is
`tripped_at + rate_limit_default_cooldown_s`. Providers may encode Unix time in
seconds or milliseconds; both are normalized to a UTC datetime. A retrip uses
`max(current_family_expiry, candidate_expiry)`, so a later bare 429 can refresh
the evidence but cannot replace a multi-day provider reset with the fallback
cooldown. At or after expiry, an availability check or spawn removes the active
entry and the spawn is allowed as a probe. State is intentionally run-local: a
new harness run does not inherit an old process's in-memory health assumptions.

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

The result is intentionally polymorphic for compatibility: a successful spawn
is the bare id, while only a rate-limit short-circuit is the JSON object above.
Non-LLM callers should pass the returned string to the exported
`team_harness.parse_rate_limited_spawn_result`; it returns the validated object
only when `spawned` is exactly `false`, `status` is `rate_limited`, and the
required family/reset fields are present. It returns `None` for an agent id,
unrelated JSON, and existing `ERROR:` strings.

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
expiry, and each later observed trip appends its effective, never-shortened
family interval. Existing fields—including per-turn `usage.prompt_tokens` and
`usage.completion_tokens`—are untouched.

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
