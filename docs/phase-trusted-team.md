# Trusted-team collaboration

Status: implementation direction and trusted-team policy approved on 2026-10-28.
The first slice is confirmed Hub-claim admission. Human membership, sharing and
recovery authorization remain to be implemented under the approved boundary.

## Goal

Let named people and their agents work on shared project context and hand work
over with its execution and verification evidence intact. Keep responsibility,
credentials and execution ownership separate.

The initial product boundary is a trusted team using one Hub. This does not
provide isolation between mutually untrusted tenants or operating-system users.
Project access requires explicit membership. Personal knowledge stays private
and local until its owner explicitly shares it. Recovery and force-release are
restricted to team administrators. Current claim and read-only handoff features
do not yet enforce these membership, sharing or recovery permissions.

Existing Fleet Hub, Worklore, journals, receipts and mesh messaging remain the
foundation. No new runtime dependency, service, storage backend or deployment is
part of this work.

## Evidence

The 2026-10-28 disposable four-client experiment against `15402b155` observed
one winner in a CLI claim race, successor-only Worklore writes after handoff,
message replay without duplication and rejected revoked credentials. It also
observed overlapping `repo_claim` blocks during a real Hub outage without grants,
fleet-peer force release, fleet-wide Worklore reads and per-node revocation.
These are distinct contracts, not evidence that a complete teammate experience
already exists. The outage observation was a library probe, not an end-to-end
duplicate worker launch.

Relevant source contracts are `src/brigade/fleet_client.py`,
`src/brigade/worklore_ownership.py`, `docs/worklore-ownership.md` and
`docs/phase-agent-mesh.md`. The mesh plan already owns teammate messaging,
invitation, grants and cryptographic identity. Do not introduce a competing
invitation system here.

OpenClaw's [team setup](https://docs.openclaw.ai/start/teams) and
[multi-user model](https://docs.openclaw.ai/concepts/multi-user) inform the
collaboration goal. Their session assignment tracks responsibility; Brigade's
execution claims retain their separate fencing contract. The episode 10
[prepared-review demonstration](https://www.youtube.com/watch?v=EPkF9Y0bZ1Q&t=1560)
suggests a useful future work-item view, not a new automatic-merge authority.

## Sequencing and decision bases

1. Confirmed claim admission. Basis: observed overlapping outage entry plus the
   operator's instruction to settle shared-work outage behavior before storage.
2. Named people linked to existing principals. Basis: the requested human and
   fleet collaboration goal. A display label is not authentication, and existing
   read-only owner charters are not membership authorization.
3. Project membership, recovery roles and explicit shared context. The operator
   approved explicit project membership, personal knowledge kept private until
   shared, and recovery or force-release restricted to team administrators on
   2026-10-28. Apply this boundary across the existing routes and projections,
   rather than limiting enforcement to a new view.
4. Prepared handoffs and a shared work view. Reuse Worklore item revisions,
   ownership generations, checkpoints, run links and receipts. Do not copy
   transcripts or credential-bearing metadata into a second task store.

Each later subsystem needs its own source-grounded plan and verification. The
first slice must be usable independently and must not claim to finish the later
membership, sharing or handoff user experience.

## First slice: required Fleet claim

Caller usage:

```python
with fleet_client.repo_claim(target, base_path=workspace, require_claim=True) as claim:
    # Entry implies a currently confirmed Hub claim.
    dispatch_existing_work()
```

The CLI form is `run TASK --require-fleet-claim`. It uses the same
run path, local lock and claim protocol as an ordinary run. It adds no project
configuration file or environment variable.

### Admission

- `repo_claim` gains the keyword-only `require_claim: bool = False`.
- The default preserves existing local behavior. Explicit required mode refuses
  entry without a confirmed grant: no configured Hub, configuration read error,
  missing usable node identity, unavailable Hub, repeated ownerless refusal, or
  a grant whose confirmed deadline has expired.
- Auth refusals, held claims, quotas, protocol fences and invalid targets retain
  their existing terminal refusal behavior.
- Add `FleetClaimRequiredError` under the existing `FleetClaimHeldError` family
  for the previously degradable admission outcomes. Error text names the fixed
  refusal reason, without raw configuration, credential values or network error
  details.
- Preserve the bounded initial retry and holder-scoped orphan cleanup when an
  acquire response is ambiguous. Refusing admission does not prove that the Hub
  never accepted the request. Keep existing cleanup decisions for terminal
  refusals: authentication refusal starts no retry or background release, and
  held claims keep their existing refusal path. Required admission does not
  expand cleanup authority or promise immediate removal of every uncertain
  remote claim; existing TTL and operator recovery remain relevant.
- Required mode enforces the existing abort-on-loss policy even when the solo
  continuation option is selected. Active credential rejection and unclassified
  heartbeat refusals also request cancellation in required mode. Credential
  detail is delivered once through `on_credential_failure`, without duplicate
  `on_claim_lost` publication. The existing bounded abort applies when the
  credential callback blocks or is absent. Default behavior stays unchanged.
  Required mode does not grant stronger physical process termination guarantees
  than existing cancellation provides.

### CLI and detached execution

- The `--require-fleet-claim` flag is mutually exclusive with
  `--no-fleet-claim` and is forwarded to detached child argv.
- The flag reaches `repo_claim(require_claim=True)` before agent execution.
  A refusal exits 2 through the existing claim error path, releases the local
  lock, and leaves a failed terminal run receipt when artifacts are enabled.
- The default CLI path remains source-compatible with existing context-manager
  adapters: do not pass a new keyword when the flag is absent.
- Model admission, budgets, worktree handling and active-run cancellation remain
  their existing gates. A confirmed repo claim does not replace any of them.

## Acceptance and limits

Use real temporary Hub servers for grant and outage boundaries. Prove no guarded
entry in required mode during a real transport outage, and retain a default-mode
control that records the documented local fallback. Prove confirmed grants allow
entry, revoked credentials remain refused, ownerless refusals do not admit work,
and required mode cannot opt into continue-on-loss.

At the CLI boundary, prove required mode reaches admission, refusal starts no
agent and leaves a terminal failure receipt, incompatible flags fail parsing,
and detached children retain the flag. The existing solo path remains covered.

No release, production migration or fleet policy activation is authorized by
this design. Dolt remains an optional history sink and a future structured
knowledge experiment.

## Next slice: prepared context and people attribution

Prepare a read-only work view from one database snapshot. Include the item
version, ownership revision and generation, checkpoint, bounded run references
and reported evidence. A prepared view never accepts ownership or authorizes a
run. Receipt references remain unresolved until their owning verifier checks
the exact source and run binding.

An administrator-managed person directory can supply current registered node
responsibility. Bind that metadata to an opaque node enrollment identifier,
not a token digest or a reusable node name. Revocation or re-enrollment must
invalidate the association until an administrator explicitly binds it again.
Reuse the mesh plan's person identifiers and future invitation integration.

Existing Worklore events lack node enrollment epochs. Their historical human
attribution must remain unrecorded. Show current registered responsibility
separately without relabeling old actors, owners or reports. A display name
grants no read, execution, recovery or membership authority.

Project ACL implementation must cover existing Worklore routes, policy reads,
dashboard views and event projections together. Check membership inside the
same transaction as a protected write and its replay checks. A new restricted
view cannot establish isolation while older routes remain fleet-readable.
Coordinate schema and HTTP changes with the existing mesh work before editing.
