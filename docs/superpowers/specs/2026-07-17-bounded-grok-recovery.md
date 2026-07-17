# Bounded direct-Grok recovery

## Goal

Recover one narrow direct Grok read-only result-integrity failure without turning Brigade into a general provider router: continue the exact Grok session once, then use one explicitly configured Cursor-Grok ACP seat if the continuation also lacks a valid structured final.

## Scope

The policy applies only to `brigade run --worker <seat> --read-only` when the selected seat uses `cli = "grok"`, `transport = "direct"`, and the adapter returns `failure_phase = "output-validation"` with `failure_kind = "malformed-final-output"`. Startup, authentication, settings, network, timeout, permission, nonzero inference, writable, orchestrated-worker, and other output-validation failures do not trigger recovery.

## Considered approaches

1. Retry inside the Grok adapter. This keeps CLI mechanics together but cannot resolve or validate a roster fallback and cannot produce worker-level attempt artifacts cleanly.
2. Retry in the top-level run command. This has artifact context but duplicates transport dispatch and model-setting logic outside its current boundary.
3. Add a bounded recovery policy to `run_transport.dispatch`. This is the selected approach because dispatch already owns the seat, transport, model, reasoning, timeout, cwd, sandbox, and direct-worker mode. The adapter only gains exact-session continuation support and Grok envelope metadata extraction.

## Configuration

A direct Grok seat may declare `invalid_final_fallback = "<seat-name>"`. Roster loading rejects this field on any non-direct-Grok seat and rejects references that do not name an existing worker with `cli = "cursor"`, a `grok-*` model, `transport = "acpx"`, and the reviewed ACPX version. The field remains optional so first-attempt success does not require a fallback; an exhausted continuation without it fails as `grok-fallback-missing`.

## Execution

The first direct attempt keeps the original task, model, reasoning, cwd, sandbox, timeout, and read-only settings. A typed invalid final must include the Grok session identifier. Brigade then invokes `grok --resume <session-id>` once with the same settings and a fixed request to return the final answer through the required schema. A missing session identifier fails as `grok-session-missing`.

If the continuation succeeds, it is selected. If it returns the same typed invalid-final result, Brigade invokes the configured ACP fallback once with the original task. Any other continuation failure stops recovery. The ACP result is terminal whether it succeeds or fails, so the policy cannot recurse.

## Artifacts

`worker-results.json` keeps the selected result in the existing worker fields and adds `attempts` for direct-Grok recovery candidates, including first-attempt success. Each attempt records kind, worker, original task, transport, requested model, reasoning, UTC start and finish, exit code, terminal reason, failure phase and kind, session ID, log references, and `selected`. Raw stdout and stderr from every attempted process are written to distinct worker-attempt logs.

No invalid attempt is selected. Concise schema-valid answers such as `No actionable findings.` remain valid and suppress recovery.

## Verification

Adapter tests cover session metadata extraction and exact `--resume` argv construction. Roster tests cover valid and invalid fallback references. Direct-worker tests cover first-attempt success, continuation recovery, fallback recovery, missing fallback, all attempts invalid, and non-trigger failures. The final gate is `./scripts/verify` through `brigade work verify run`.
