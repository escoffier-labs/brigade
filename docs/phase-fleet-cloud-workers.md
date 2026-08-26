# Fleet Cloud Workers and Honest Command Deck

## Status

Proposed on 2026-08-25 for the beta line. This is not a release-cut plan.

## Problem

The fleet hub currently mixes historical terminal events with live station work.
Exact terminal states such as `run.failed` are excluded, but terminal variants
such as `run.dispatch.failed` survive the live query and render as occupied
FAILED cards. Old non-terminal observations can also remain in station capacity
indefinitely.

Cloud tracking has a separate gap. Codex Cloud dispatches are registered, but
Cursor Cloud only reports whether a key exists and Claude Code cloud sessions
are not a provider. GitHub branches are inferred as orphaned work, so historical
branches can dominate the tracker without proving that a cloud worker is active.
There is no shared subscription-aware admission limit across orchestrators.

## Decision

Use hub-authoritative cloud leases and local provider adapters.

Provider credentials stay on the dispatching machine. Brigade adapters call the
provider's supported CLI or API, normalize the result, and publish only safe
identity, repository, state, timestamp, and artifact metadata through normal
fleet commands. Agents must not call the fleet hub HTTP API directly.

Cloud capacity is separate from physical-machine capacity:

| Pool | Initial active limit | Authority |
|---|---:|---|
| Cursor Cloud | 3 | Cursor subscription and spend controls |
| Codex Cloud | 2 | ChatGPT/Codex subscription pool |
| Claude Code Cloud | 0 | Disabled while Claude Max usage is exhausted |
| All cloud providers | 4 | Brigade fleet policy |
| Rocinante | 10 | Existing physical station configuration |
| Shadowfax | 8 | Existing physical station configuration |
| Gandalf | 4 | Existing physical station configuration |

The provider-specific and global cloud limits must both admit a launch. A
provider authentication, quota, or billing-limit response closes that provider's
circuit until an operator re-enables it or a successful bounded probe clears it.
Brigade does not guess remaining tokens or dollars when the provider does not
publish that information.

## Considered alternatives

### Local-only quotas

Each machine could count its own registry entries before launch. This is small,
but two orchestrators in different repositories could both admit work and exceed
the subscription limit. It does not satisfy the coordination goal.

### Enroll every provider VM as a fleet node

Each short-lived vendor VM could receive a node identity and publish directly.
This gives precise heartbeats but creates secret distribution, revocation, and
identity churn for disposable infrastructure. It is deferred unless a provider
offers a safe workload-identity exchange with the private hub.

## Provider adapters

### Cursor Cloud

Use the public Cloud Agents API v1 with the existing `CURSOR_API_KEY` secret.
List agents through `GET /v1/agents`, then resolve each relevant agent's
`latestRunId` through the run endpoint. Execution state comes from the run, not
the durable agent's ACTIVE or IDLE state. Polling is bounded, paginated, and
best-effort. API errors must not erase the last observation or admit extra work.

Creating a new Cursor agent is a separate mutating operation. The first slice
adds supported status reconciliation and the admission gate, then routes any
existing or future launcher through that gate. Prompt text and API keys are
never written to the registry, events, receipts, or dashboard.

### Codex Cloud

Keep the existing `codex cloud exec/status/list/diff` adapter. Register a lease
before submission, bind the provider task ID immediately after submission, and
release the lease only after a terminal provider observation. Submission errors
and timeouts become terminal safe events without applying a diff.

### Claude Code Cloud

Add `claude-cloud` as a tracked provider with register/adopt/status contracts.
The launcher uses the installed Claude Code cloud surface only after its policy
limit is raised above zero. While the subscription is exhausted, launch fails
closed before invoking Claude. Existing Claude web sessions may be adopted by
safe session ID or branch evidence without storing prompts or transcripts.

The first slice may ship the provider as disabled and unprobed. A live canary is
not required while Claude quota is unavailable, but unit and CLI contract tests
are required.

## Cloud lease contract

A cloud lease contains:

- provider
- provider task or session ID when known
- repo target
- sanitized label and prompt hash
- owner node and conductor
- admitted, renewed, and expiry timestamps
- normalized state and optional safe artifact reference

No API key, bearer, prompt body, transcript, environment secret, or provider
response body is stored.

Admission is atomic at the fleet authority. It checks the provider limit and the
global cloud limit in the same transaction. A launch that cannot acquire a lease
does not call the provider. A lease without a provider task ID has a short
submission TTL. Active leases renew from provider observations. Terminal and
expired leases stop consuming capacity but remain queryable as outcomes.

Browser-launched work is not exempt. An operator may adopt an existing task, but
new browser automation must acquire the same lease before opening the provider
launch flow.

## Command deck projection

The deck separates four concepts:

1. Physical stations show only current non-terminal runs heard within the
   configured active window.
2. Cloud Workers shows active leased work grouped by Cursor, Codex, and Claude,
   with `used/limit` counts and provider circuit state.
3. Needs You contains approvals, collisions, provider errors, and one bounded
   abandoned-run row per run.
4. Recent outcomes contains terminal history and remains bounded.

A canonical terminal-state helper covers exact lifecycle terminals and terminal
families such as `.failed`, `.completed`, `.interrupted`, `.cancelled`,
`.canceled`, `.timed_out`, and `.timeout`. Terminal rows never count as busy.
Non-terminal rows older than `stale_after_seconds` do not count as busy or remain
in station tiles. They may produce one bounded Needs You item. Historical events
remain in SQLite; this change fixes projections, not audit retention.

GitHub branch inference remains recovery evidence only. It never creates an
active cloud worker or consumes a cloud slot without a provider observation or
an unexpired adopted lease.

## Public contracts

- `brigade run cloud status --json` reports all four providers, admission limits,
  active counts, circuit state, and normalized task rows.
- `brigade run cloud register` and `adopt` accept `claude-cloud`.
- Cloud launchers fail with a stable capacity or disabled-provider result before
  provider mutation when admission is denied.
- Fleet status and dashboard JSON/HTML expose Cloud Workers without exposing
  secrets or requiring direct hub API calls by agents.
- Existing registry schema is read compatibly. A migration adds defaults rather
  than discarding entries.

## Test contract

Tests must first demonstrate these failures on the current implementation:

- `run.dispatch.failed` appears as a live station tile.
- an old non-terminal event still consumes station capacity.
- a wired Cursor key does not produce live API task observations.
- Claude cannot be registered or reported.
- two simultaneous admissions can exceed a shared limit.
- inferred historical branches appear in recovery data but never active capacity.

Implementation tests then cover terminal-family normalization, age filtering,
bounded Cursor pagination and error handling, Claude's disabled fail-closed
state, atomic global/provider admission, lease expiry/release, sanitized payloads,
and the Cloud Workers HTML projection. Network calls use fakes. Live canaries are
bounded read-only checks and are not part of the unit suite.

## Rollout

1. Land the schema, admission, and projection changes on the beta line.
2. Deploy the beta checkout to the hub and all three physical nodes.
3. Keep Claude limit at zero.
4. Run read-only Cursor and Codex inventory canaries.
5. Confirm the dashboard has no terminal cards in station capacity and shows
   provider limits even when no cloud work is active.
6. Launch one Cursor Cloud canary through Brigade, observe it in Cloud Workers,
   and confirm terminal release. Do not auto-apply its changes.
7. Enable Claude only after subscription usage returns and a read-only canary
   succeeds.

Rollback restores the prior beta checkout and service. Registry and hub history
remain readable because migrations are additive.

## Non-goals

- Cutting version 0.27.0.
- Deleting historical hub events or cloud branches.
- Estimating provider dollar or token balance without provider authority.
- Auto-merging or auto-applying cloud work.
- Giving cloud VMs permanent node credentials.
