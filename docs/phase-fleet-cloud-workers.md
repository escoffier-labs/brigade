# Fleet Cloud Workers and Honest Command Deck

## Status

Proposed on 2026-08-25 for the beta line. Updated on 2026-08-26 with verified provider URLs and the Jules adapter. This is not a release-cut plan.

## Problem

The fleet hub currently mixes historical terminal events with live station work.
Exact terminal states such as `run.failed` are excluded, but terminal variants
such as `run.dispatch.failed` survive the live query and render as occupied
FAILED cards. Old non-terminal observations can also remain in station capacity
indefinitely.

Cloud tracking has a separate gap. Codex Cloud dispatches are registered, but
Cursor Cloud only reports whether a key exists, Claude Code cloud sessions are not
a provider, Jules sessions are not tracked, and GitHub branches are inferred as
orphaned work. Historical branches can dominate the tracker without proving that
a cloud worker is active. There is no shared subscription-aware admission limit
across orchestrators.

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
| Jules Cloud | 15 | Google AI Pro subscription |
| All hosted cloud providers | 4 | Brigade fleet policy |
| Grok Bot | tracked, not hosted | Local/self-hosted seat policy |
| Rocinante | 10 | Existing physical station configuration |
| Shadowfax | 8 | Existing physical station configuration |
| Gandalf | 4 | Existing physical station configuration |

The provider-specific and global cloud limits must both admit a launch. A
provider authentication, quota, or billing-limit response closes that provider's
circuit until an operator re-enables it or a successful bounded probe clears it.
Brigade does not guess remaining tokens, tasks, or dollars when the provider does
not publish that information.

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

Use the public beta Cloud Agents API v1 with the existing `CURSOR_API_KEY`
secret. The v1 surface separates durable agents from per-prompt runs; execution
state comes from runs, not from the durable agent's ACTIVE or IDLE state.
Official documentation and schema references are at
https://cursor.com/docs/cloud-agent/api/endpoints,
https://cursor.com/docs/api, and
https://cursor.com/docs-static/cloud-agents-openapi.yaml.
The API accepts both Basic and Bearer authentication.

Supported v1 operations for the hosted adapter slice are:

- `POST /v1/agents` and `GET /v1/agents` for agent creation and listing
- `POST /v1/agents/{id}/runs`, `GET /v1/agents/{id}/runs`, and
  `GET /v1/agents/{id}/runs/{runId}` for run creation, listing, and status
- run cancellation
- usage and artifact metadata
- `statusChange` webhooks as a future event signal

List agents with bounded pagination, resolve each relevant agent's
`latestRunId` through the run endpoint, and normalize the run state. Polling is
bounded, paginated, and best-effort. API errors must not erase the last
observation or admit extra work.

Creating a new Cursor agent is a separate mutating operation. The first slice
adds supported status reconciliation plus lease-gated agent and run creation.
Hub admission must precede the `POST /v1/agents` mutation and any run creation.
The returned agent and run IDs are bound to the lease before launch is
acknowledged. Browser-created adoption is
allowed only after the API lists the agent, the repository and owner match, and
a lease can still be admitted. `autoCreatePR` and `auto-merge` remain opt-in and
are off by default. Prompt text and API keys are never written to the registry,
events, receipts, or dashboard.

The installed cursor-agent 2026.08.11 also has a private cloud worker mode, but
that is a separate self-hosted worker surface and is not required for the hosted
adapter slice.

### Codex Cloud

Keep the existing `codex_cloud.py` adapter. The installed codex-cli 0.149.1
has the experimental `codex cloud` subcommand with `exec`, `list`, `status`,
`diff`, and `apply`. The official reference is at
https://developers.openai.com/codex/cli/reference. The adapter is already valid;
this slice only gates it on the hub lease.

Register a lease before `codex cloud exec`, bind the provider task ID
immediately after submission, and release the lease only after a terminal
provider observation. Submission errors and timeouts become terminal safe events
without applying a diff. Inventory is bounded through `codex cloud list
--json` and `codex cloud status TASK_ID`. The adapter never calls `codex cloud
apply` automatically; applying patches remains a human decision.

### Claude Code Cloud

Add `claude-cloud` as a tracked provider with register/adopt/status contracts.
The installed Claude Code 2.1.246 supports `--cloud [description|session_id|url]`,
`--environment` for a self-hosted cloud pool, `--from-pr`, `--background`, and the
machine-readable `claude agents --json --all` inventory. The official docs at
https://code.claude.com/docs/en/cli-reference and
https://code.claude.com/docs/en/claude-code-on-the-web lag this installed
surface, so the contract is based on the installed binary.

The launcher uses the installed Claude Code cloud surface only after its policy
limit is raised above zero. While the subscription is exhausted, launch fails
closed before invoking Claude. Existing Claude web sessions may be adopted by a
safe session ID or branch evidence without storing prompts or transcripts. Any
future launch requires hub admission and a proved durable session ID. Brigade
must not automate claude.ai cookies or undocumented HTTP endpoints.

The first slice inventories `claude agents --json --all` through a bounded
subprocess parser and labels it as installed-CLI observation. It does not call
`claude --cloud` while the provider limit is zero.

### Jules Cloud

Add a Jules Cloud provider adapter using only stdlib `urllib`. Jules is
included with the Google AI Pro subscription at 100 tasks per rolling 24 hours
and 15 concurrent tasks. The official alpha REST base is
https://jules.googleapis.com/v1alpha, documented at
https://jules.google/docs/api/reference/overview/ and
https://developers.google.com/jules/api/reference/rest. API keys are created at
https://jules.google.com/settings and passed in the `X-Goog-Api-Key` header.

Supported operations for the adapter are:

- `GET /v1alpha/sources` and `GET /v1alpha/sources/{sourceId}` to list and get
  connected repositories
- `POST /v1alpha/sessions`, `GET /v1alpha/sessions`, and
  `GET /v1alpha/sessions/{sessionId}` to create, list, and get sessions
- `POST /v1alpha/sessions/{sessionId}:approvePlan` to approve a generated plan
- `GET /v1alpha/sessions/{sessionId}/activities` to poll activities

Hub admission must precede `POST /v1alpha/sessions`. The session ID is bound to
the lease immediately after a successful create. The default is
`requirePlanApproval: true`. `AUTO_CREATE_PR` and applying patches are off unless
explicitly requested. Unknown session states hold capacity until a later poll
proves the session terminal. There is no documented idempotency key, so the
adapter does not blindly retry after an ambiguous create timeout; it surfaces the
uncertainty as a bounded Needs You item and keeps the lease open until the next
poll clarifies the state.

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
global cloud limit in the same transaction. The Fleet Hub Task 2
implementation already includes the Jules provider cap of 15 and the
model/provider policy table; the global hosted cap remains 4. A launch that
cannot acquire a lease does not call the provider. A lease without a provider task
ID has a short submission TTL. Active leases renew from provider observations.
Terminal and expired leases stop consuming capacity but remain queryable as
outcomes.

Hub admission must precede any mutating provider call, including `POST
/v1/agents` and `POST /v1/agents/{id}/runs` for Cursor, `codex cloud exec` for
Codex, `POST /v1alpha/sessions` for Jules, and any future Claude launch.

Browser-launched work is not exempt. An operator may adopt an existing task, but
new browser automation must acquire the same lease before opening the provider
launch flow.

## Command deck projection

The deck separates four concepts:

1. Physical stations show only current non-terminal runs heard within the
   configured active window.
2. Cloud Workers shows active leased work grouped by Cursor, Codex, Claude, and
   Jules, with `used/limit` counts and provider circuit state.
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
active cloud worker or consumes a cloud slot without a provider observation or an
unexpired adopted lease.

## Public contracts

- `brigade run cloud status --json` reports all five providers (Cursor, Codex,
  Claude, Jules, and Grok Bot), admission limits, active counts, circuit state,
  and normalized task rows.
- `brigade run cloud register` and `adopt` accept `claude-cloud` and `jules`.
- Cloud launchers fail with a stable capacity or disabled-provider result before
  provider mutation when admission is denied.
- Fleet status and dashboard JSON/HTML expose Cloud Workers without exposing
  secrets or requiring direct hub API calls by agents.
- Existing registry schema is read compatibly. A migration adds defaults rather
  than discarding entries.

## Test contract

Tests must first demonstrate these failures on the current implementation:

- `run.dispatch.failed` appears as a live station tile.
- An old non-terminal event still consumes station capacity.
- A wired Cursor key does not produce live API task observations.
- Claude cannot be registered or reported.
- Jules cannot be registered or reported.
- Two simultaneous admissions can exceed a shared limit.
- Inferred historical branches appear in recovery data but never active capacity.

Implementation tests then cover terminal-family normalization, age filtering,
bounded Cursor pagination and error handling, Claude's disabled fail-closed
state, Jules stdlib HTTP adapter behavior, no blind retry on ambiguous Jules
create timeouts, atomic global/provider admission, lease expiry/release,
sanitized payloads, and the Cloud Workers HTML projection. Network calls use
fakes. Live canaries are bounded read-only checks and are not part of the unit
suite.

## Rollout

1. Land the schema, admission, and projection changes on the beta line.
2. Deploy the beta checkout to the hub and all three physical nodes.
3. Keep Claude limit at zero.
4. Run read-only Cursor, Codex, and Jules inventory canaries.
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
- Estimating provider dollar, token, or task balance without provider authority.
- Auto-merging or auto-applying cloud work.
- Giving cloud VMs permanent node credentials.
- Implementing the Cursor private cloud worker mode or Claude self-hosted cloud
  pool in the first slice.
