# Grok Bot claim execution context

## Status

Approved on 2026-08-24.

## Problem

The native Grok Bot adapter exposes role-scoped queue operations, but every current queue projection omits `base_ref`, `ownership_paths`, `instructions`, and `verification_commands`. Supervised canaries work because an operator repeats that context in the Bot conversation. A scheduled worker can claim a job but cannot determine the bounded work it was assigned.

The queue must hand the complete bounded task to the authenticated worker that successfully claimed it. List, status, operator, and CLI surfaces must remain redacted.

## Decision

Return a whitelisted `execution_context` object only from a successful worker MCP `grokbot_queue_claim` call.

The existing worker tool inventory remains unchanged. `grokbot_queue_list`, `grokbot_queue_status`, and every CLI mutation continue returning safe projections. Operator instances still cannot discover or call worker tools.

The execution context has this exact shape:

```json
{
  "label": "Bounded task label",
  "role": "repository-scout",
  "repository": "escoffier-labs/brigade",
  "base_ref": "main",
  "ownership_paths": ["src/brigade/grokbot_mcp.py"],
  "instructions": "Perform the assigned bounded task.",
  "verification_commands": ["pytest -q tests/test_grokbot_mcp.py"],
  "artifact": {"kind": "report"},
  "timeout_seconds": 900
}
```

The returned claim result is the existing safe projection plus one `execution_context` member. It does not include the idempotency key hash, queue paths, bearer references, environment values, or record internals.

## Architecture

`grokbot_jobs.claim()` remains the safe CLI operation. Add an internal worker operation named `claim_execution_context()` that uses the same queue lock and claim transition as `claim()` but returns the projection plus the whitelisted context.

Both operations share one locked claim helper. They must not independently implement claim state transitions. Context is derived from the already validated immutable job specification while the queue lock is held.

`GrokbotAdapter` calls `claim_execution_context()` only for `grokbot_queue_claim`. The adapter already fixes the target, worker role, bot identity, and lease duration from deployment configuration. The caller may provide only `job_id` and `lease_id`.

## Security boundaries

- Only a successful role-matching worker claim receives context.
- A retry with the same bot and lease may return the same context.
- A conflicting lease, expired lease, terminal job, cross-role job, malformed request, or caller-selected authority returns the existing generic adapter error.
- List, status, operator, and CLI responses never include execution context.
- The existing envelope validator remains authoritative for sizes, allowed keys, repository syntax, refs, owned paths, commands, roles, artifact kinds, and timeouts.
- Task instructions are untrusted work data. They cannot alter adapter policy, role identity, target selection, lease duration, approval boundaries, or tool inventory.
- Queue producers must not place credentials in labels, instructions, commands, or other scalar values. Secret-value heuristics are outside this slice because they would add a separate detection policy with false-positive and false-negative behavior.

## Failure behavior

Context disclosure and claim mutation are one atomic operation. If validation or claim fails, no context is returned. If response serialization fails after the record is claimed, an idempotent retry with the same lease returns the same context. A different lease cannot take over the job.

No new queue state, schema migration, endpoint, credential, dependency, or service is introduced.

## Tests

- Watch a new internal claim-context test fail before implementation.
- Assert the exact context whitelist after a successful claim.
- Assert an idempotent same-lease retry returns the same context.
- Assert conflicting and expired leases disclose no context.
- Assert worker MCP claim returns context for its role.
- Assert cross-role claim is rejected before context disclosure.
- Assert list and status remain redacted.
- Assert CLI claim remains redacted.
- Assert the operator inventory and worker inventories remain exactly 4 and 8 tools.

## Documentation

Update `docs/grokbot-mcp.md` and `docs/technical-guide.md` to distinguish safe projections from the authenticated post-claim execution handoff. State that unattended routines should claim first, use only the returned context, renew before lease expiry, and stop on context or role mismatch.

## Rollout and acceptance

1. Merge and deploy the updated Brigade package without changing public routes, credentials, or tool inventories.
2. Re-run local and edge canaries for all three roles.
3. Enqueue a bounded Repository Scout job without repeating its instructions in chat.
4. Have the Bot claim it, read the returned execution context, renew once, and complete it with a report artifact.
5. Repeat with one bounded Implementation Worker job that produces a draft PR and cannot merge or deploy.
6. Save the proven workflows as skills, then create staggered routines with at most one claimed job per run.

Acceptance requires both Bots to finish from the claim capsule alone. Manual task instructions in the Bot conversation do not count.

## Alternatives rejected

### Separate context tool

A lease-bound `grokbot_queue_context` tool would make disclosure explicit, but it adds a ninth worker tool, another network round trip, another retry state, and a migration change for every exact-inventory canary. The claim already establishes the disclosure boundary.

### Self-contained labels

Labels are limited to 160 characters and omit base refs, owned paths, commands, and artifact instructions. They cannot carry the worker contract safely.

### Signed task artifact

A separate artifact service or signed download would add storage, URL expiry, credential, and cleanup concerns. The existing authenticated role-scoped MCP connection already reaches the queue authority.
