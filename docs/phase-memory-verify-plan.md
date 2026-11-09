# Memory verification implementation plan

Design: [phase-memory-verify.md](phase-memory-verify.md). Issues: #2385
(model-backed verification pass) and #2387 (access-weighted cadence). Sibling:
#2386 (deterministic link repair), which shares the fairness helper from
slice 5.

Each slice below is one pull request. A slice lists its dependencies, the
files it touches, the tests it adds, and its security review focus. Every
slice gets its own security review before merge, because each one either
changes the hub, reads private evidence, calls a model, or writes proposals.
Every slice adds a changelog fragment, updates
[memory-operations.md](memory-operations.md) for anything an operator sees,
and regenerates the command inventory when it adds a command:

```bash
BRIGADE_EXTRAS=1 brigade roadmap commands --write
```

Focused checks run through Brigade with the slice's own selectors:

```bash
brigade work verify run --target . --argv-json '["./scripts/verify-focused","<selectors>"]' --capture brigade-work
```

Hub slices also run the hub crate tests for their module through Brigade.
New Python modules stay under the 2,000-line ceiling of the module size
ratchet. Planned commands are written without the `brigade` prefix because
they do not exist yet.

Revision 2 (2026-11-08) moves the hub work ahead of every client slice. The
verifier floor (old slice 10, now slice 3) is mandatory, and a new slice 4
adds proposal-bound attestations and the reviewer hash read. Old slices 3 to
9 become slices 5 to 11 with the same order. Revision 3 makes the verifier
flag principal-level in slice 3, adds exact card binding and a hidden
manifest list to slice 4, and requires op-level seat agreement in slice 8.

## Integration baseline

- #2383 (near-duplicate pass and per-scope fair scheduling) must be merged
  before slice 5, which extracts its ordering into a shared helper.
- Memory schema is at version 9 on main. Slice 1 takes version 10, and slices
  3 and 4 take the next versions in merge order. If another change takes a
  number first, the slice rebases onto the next one.
- No slice submits a live proposal before slices 3 and 4 are merged and
  deployed. Enabling the pass follows the go/no-go steps in D7 after slice 11.

## Dependency order

```text
1 -> 2 -> 5 -> 6 -> 8 -> 9 -> 10 -> 11
          5 -> 7 -> 8
3 -> 4 -> 9
     4 -> 10
```

Slices 3 and 4 can proceed in parallel with slices 1 and 2. Slices 6 and 7
can proceed in parallel after slice 5.

## Slice 1. Hub read counters and budgets

Design: D2 (what the hub records, write path and budgets, schema, retention).

Changes:

- `engines/hub/src/memory/schema.rs`: version 10 migration adding
  `memory_read_days` and `memory_read_totals`, and `read_counts_since_day` in
  `memory_meta`. Log epoch, proposal ids and event sequence are unchanged.
- A new `engines/hub/src/memory/read_counts.rs`: the bounded accumulator, the
  in-memory per-principal increment limits, low-priority flushing in
  transactions bounded by rows and SQLite execution time, resumable from the
  oldest keys, the table row caps, and saturation.
- The `card` read and `search` paths in `engines/hub/src/memory/mod.rs` report
  hits to the accumulator after a 200 reply. `eval: true` searches and the
  hub's own searches do not.
- The idle retention sweep removes expired day rows oldest first and the
  totals rows of retired cards, within its row and time budget, and resumes on
  the next idle tick.
- `memory_config.rs`: the `memory.read_counts` keys with their defaults and
  ranges. Retention is 7 to 365 days, default 35.
- `engines/hub/src/telemetry.rs`: one bounded label per budget.

Tests (`engines/hub/src/memory/read_counts_tests.rs`):

- A version 9 database migrates to 10 with the same log epoch, sequence and
  proposal ids. A second start is a no-op.
- A search reply increments `search_hits` once per returned card. A card read
  at offset 0 increments `card_reads`, and a continuation chunk does not.
- `eval: true` searches, `log`, `status`, proposal reads and refused reads
  leave counts unchanged.
- The schema holds no principal, query or time-of-day column.
- One principal past either daily limit is served but not counted, and the
  limit resets at the day boundary.
- A flush stops at the row cap and at the time budget, commits, and the next
  flush continues without double counting.
- A flush waits while the writer queue is busy, unless the accumulator is over
  three quarters full.
- Both row caps stop new keys while existing rows still update.
- The sweep stays within its budgets and finishes over several ticks.

Security review focus: nothing identifying is stored, and a read flood from
one principal cannot stall the writer or grow the tables past their caps.

## Slice 2. `read_stats` right, audit and client

Depends on slice 1. Design: D2 (who can read the counts).

Changes:

- `engines/hub/src/memory/auth.rs` and the grant validation: the new
  `read_stats` right. `reflect` does not imply it.
- `engines/hub/src/memory/mod.rs`: route `read_stats` through the existing read
  dispatch. It requires `read` and `read_stats` on the exact scope, pages at
  most 1,000 rows, and uses an authenticated cursor bound to the principal and
  scope. A per-principal rate limit returns
  `429 memory_read_stats_rate_limited`.
- An audit row per request in the hub audit table
  (`engines/hub/src/schema/audit.rs`), never in the memory event log.
- `src/brigade/memory_hub_client.py`: a `read_stats` client call that refuses
  admin credentials, as reflect does.
- Remote connector profiles and fleet digests do not gain the field.

Tests:

- `read` alone, `reflect` with `read`, and a grant on another scope each
  return `403 memory_grant_missing`. `read` with `read_stats` returns rows.
  Collections need their exact scope.
- An altered, expired or other-principal cursor is refused.
- The rate limit refuses the request over the limit and recovers after it.
- Every request, refused ones included, writes one audit row, and no memory
  event is appended.
- Rows hold only the documented numeric fields and card ids.
- The client test uses the existing `FakeHub` in
  `tests/test_memory_hub_client.py`.
- A connector profile test confirms no tool returns read counts.

Security review focus: the grant split from `reflect`, the cursor, the audit
record, and the single-reader-scope note in the docs.

## Slice 3. Verifier principals and the Tier 2 floor

No dependency on slices 1 and 2. Design: D4 (verifier principals and the
Tier 2 floor). Mandatory before any slice submits a proposal.

Changes:

- The hub principal record: a `verifier` attribute set or cleared only by an
  admin. The principal cannot change it. Each credential record carries a copy
  bound at issue, and every request refuses a copy that differs from the
  principal record with `403 memory_verifier_flag_mismatch`.
- The enrollment, rotation and re-add paths issue every new credential with
  the principal's flag and refuse a request that would create an unflagged
  credential for a verifier principal with
  `memory_verifier_credential_unflagged`. Revocation keeps the flag, and
  removing the principal leaves a flagged tombstone. Setting or clearing the
  flag updates the principal and all live credentials in one transaction.
- `engines/hub/src/memory/proposal_tiers.rs` and `proposals.rs`: proposals
  from a flagged principal record `verifier: true` at submit and are Tier 2 at
  submit, preflight and every decision, whatever their ops.
- `engines/hub/src/memory/preflight.rs`: a `verifier_floor` hold. The
  `auto_apply` route refuses these proposals.
- Fleet policy validation: a policy that lists a flagged principal in
  `reflect_principals` is refused with `memory_policy_verifier_listed`, and
  flagging a principal that a policy lists is refused.
- The memory `status` reply: the caller's own `verifier` and
  `reflect_principal` booleans, and a capability entry advertising the floor.

Tests:

- A verifier's `append_evidence` proposal stays Tier 2 under a policy that
  lists `append_evidence` in `tier0_op_classes`.
- Clearing the flag after submit leaves the open proposal at Tier 2 through
  preflight and decision.
- `auto_apply` on a verifier proposal is refused and changes nothing.
- Both policy refusals, including a replay of the same policy write.
- `status` reports both booleans correctly for flagged, unflagged and listed
  principals.
- A principal cannot set its own flag through any memory action.
- Rotation: the rotated credential carries the flag, and a rotation request
  asking for an unflagged credential is refused and issues nothing.
- Multiple credentials: a second and third credential enrolled for a verifier
  are all flagged, and a proposal from any of them is Tier 2.
- Revoke and re-add: revoking every credential and re-adding one, and removing
  and re-adding the principal id, both yield a flagged credential.
- A credential whose stored copy differs from the principal record is refused
  on every memory action.
- Clearing the flag updates every live credential at once, and no request in
  between sees a mismatch.

Security review focus: the floor cannot be bypassed through policy staleness,
replay, flag changes, credential rotation or re-enrollment, or a direct
`auto_apply` call.

## Slice 4. Proposal-bound attestations and the reviewer hash read

Depends on slice 3. Design: D5 (proposal-bound attestation state, hash read
for reviewers).

Changes:

- `engines/hub/src/memory/attestation.rs`: attachments from a verifier enter
  `pending_review` and never hold automatic apply or count as proof. Attach
  parses the manifest and refuses a `card` scope or card id that differs from
  the attestation's with `memory_manifest_card_mismatch`.
- `propose` binds a cited manifest only when the proposal is from the
  attaching principal, its citing `append_evidence` op and every other op
  address the attestation's scope and card id at the manifest's base
  revision, any `other_card` is in the same scope, and the record is unbound.
  Otherwise the whole proposal is refused with
  `422 memory_manifest_binding_invalid` and nothing is stored.
- Proposal decisions in `proposals.rs`: accept moves bound records to
  `active`. Reject, stale and compaction move them to `inert`. A sweep moves
  records unbound after 24 hours to `inert`. Inert records free their quota
  slot and never count as proof or as a contrary hold.
- `engines/hub/src/memory/proposal_reads.rs`: `proposal_show` returns a
  `verifier_manifests` list (evidence id, `artifact_kind`, `artifact_sha256`,
  stance and state) only to callers with `review` on the proposal's targets
  and on the scope and card of every bound attestation. For anyone else the
  key is absent.

Tests:

- A verifier's `contrary` manifest does not hold automatic apply on its card
  while the proposal is pending.
- Accept activates the hold. Reject, stale, compaction and the 24-hour unbound
  sweep each make the record inert, free its slot, and leave no hold.
- A record cannot be bound by another principal's proposal or by a second
  proposal.
- Cross-card: a manifest attested for card A cited by a proposal whose ops
  change card B is refused, including when the proposal also touches card A.
- Cross-scope: the same card id in another scope, and an `other_card` in
  another scope, are refused.
- Stale revision: a proposal whose ops use a base revision other than the
  manifest's is refused, and so is a manifest whose `card` fields differ from
  its attestation at attach time.
- An accepted proposal for card B cannot activate a contrary record for card
  A by any sequence of attach and propose calls.
- `proposal_show` returns the list to a reviewer with `review` on all
  targets and attested cards.
- To a caller with `read` only, or with `review` on some targets, replies for
  proposals binding zero, one and many manifests have the same keys and the
  same shape, and differ only in fields a non-reviewer already sees.
- Existing non-verifier attestations behave exactly as before.

Security review focus: state transitions under concurrent decisions, and that
the hash read reveals nothing to non-reviewers.

## Slice 5. Verify folder, state, scoring and never-read report

Depends on slice 2 and #2383. Design: D2 (candidate score, never-read report),
D3 (where it runs) and D6 (state file).

Changes:

- A shared fairness helper extracted from `src/brigade/memory_reflect_near.py`
  (least recently proposed, oldest first seen, key digest). The near-duplicate
  pass switches to it with no change in behavior.
- A new `src/brigade/memory_verify_state.py`: `.brigade/memory/verify/state.json`
  with the safe write, lock and recovery rules of `schedule.json`, an 8 MiB
  limit, and the three-failure pause counter.
- A new `src/brigade/memory_verify_select.py`: eligibility filters, the score,
  the staleness lane, and the never-read report.
- `[verify]` and `[[verify.workspace]]` parsing in `.brigade/memory-care.toml`,
  including the ownership rules: one scope per root, no nested roots, and an
  explicit `archive_projects` list. The workspace file still cannot name a hub
  URL or credential path.
- Commands: `memory verify status` and `memory verify run --dry-run --no-model`,
  which prints selected card ids, scores and skip counts with no text.

Tests (`tests/test_memory_verify_select.py`, `tests/test_memory_verify_state.py`):

- Score values for fixed inputs, including caps and a never-verified card.
- The staleness lane takes a quarter of the slots, rounded up, even when
  other cards have far higher read counts.
- The minimum interval and the 30-day interval for unchanged supported cards.
- Ties follow the shared helper, and the near-duplicate tests still pass.
- Never-read stays null until counting covers the window.
- A root bound to two scopes, a nested root and a missing project list each
  fail configuration.
- State files over the limit, malformed or unsafe fail closed with a fixed
  reason and no text.

Security review focus: state file safety, ownership validation, and the
absence of text in every output.

## Slice 6. Evidence assembly with per-scope isolation

Depends on slice 5. Design: D3 (inputs per card, per-scope evidence isolation,
evidence trust and independence, abstaining before any model call).

Changes:

- A new `src/brigade/memory_verify_evidence.py`. It resolves cited paths only
  under the card scope's roots and the evidence roots from
  `src/brigade/memory_reflect_candidates.py`, and bundles only under the
  scope's listed archive projects.
- Related excerpts through `brigade evidence search`, filtered to the listed
  projects, with a deterministic query.
- Receipt splitting into authenticated fields and command output, with the
  archive digest check.
- The independence test: different files, different run ids, and no copied
  span of 200 or more bytes.
- Byte bounds, trust ranks, scans of every item, and the pre-call abstain
  rule. Reason codes: `evidence_unreachable`, `evidence_flagged`,
  `no_evidence`, `uncorroborated`.

Tests (`tests/test_memory_verify_evidence.py`):

- A path under no root, under two roots of the scope, or under another scope's
  root is unreachable and never opened.
- A bundle under an unlisted or second project is unreachable.
- S19e-1 attested ids are unreachable.
- A receipt whose bytes differ from the archive digest is not authenticated.
  Its stdout is never ranked as authenticated.
- A handoff that copies a receipt span is not independent of that receipt.
- A card without two independent items, one authenticated, gets no model call.
- A flagged item is dropped and recorded with its rule ids.
- Bounds cut at the documented sizes and never split a UTF-8 sequence.

Security review focus: cross-scope isolation, path handling, the
authenticated-field boundary, and scan coverage of every byte that reaches a
prompt.

Slice 6 requirements carried from the slice 5 review:

- Open each bound root as a directory handle (`O_DIRECTORY | O_NOFOLLOW` on
  POSIX) and check the opened handle's `(st_dev, st_ino)` against the identity
  pinned in the parsed workspace. `memory_verify_config.revalidate()` followed
  by a separate open by path is not enough, because the path can change
  between the two.
- Resolve every evidence path relative to that handle, component by
  component, with no-follow opens and a containment check, never through the
  root's path string.
- Hold the handle for as long as evidence under the root is read. That also
  covers inode reuse after a root is removed and recreated.

## Slice 7. Isolated seat dispatch

Depends on slice 5. Design: D3 (seats) and D4 (private cards and connector
audiences).

Changes:

- A new `src/brigade/memory_verify_seat.py` that reuses roster seat
  resolution, model pinning and CLI adapters from the `brigade run --worker`
  path, without planning, synthesis, fleet claims or worktrees.
- `propose_seat` and `confirm_seat`, refused with `verify_seat_same_model`
  when they resolve to one model id.
- An isolation probe per CLI adapter, run by `memory verify doctor`, that
  proves the call has no tools, no recall and an empty private working
  directory. Seats it cannot prove are refused with `verify_seat_not_isolated`.
- Refusal of cloud seats and other connector audiences with
  `verify_seat_connector`, and of seats on other nodes in v1.
- Prompt and reply files in an owner-only work directory under the verify
  folder, deleted after use. Nothing goes to the shared run journal.

Tests (`tests/test_memory_verify_seat.py`):

- Scripted fake seats return fixed replies and timeouts.
- Two seats pinned to one model are refused.
- Cloud and remote seats are refused before any prompt is written.
- Work directory and file modes are owner-only, and the files are gone after
  the call, including after a timeout.
- A seat rate-limit reply maps to `seat_quota`, and an auth failure maps to a
  run failure.
- No run journal entry or report holds prompt or reply text.

Security review focus: the isolation probe itself, the connector boundary, and
cleanup on every exit path.

## Slice 8. Prompt contract, output validation and op mapping

Depends on slices 6 and 7. Design: D3 (prompt contract, output schema,
deterministic checks, mapping to ops) and D4 (prompt injection).

Changes:

- A new `src/brigade/memory_verify_contract.py`: the versioned instructions and
  their digest, nonce markers, and the `brigade.memory-verify.v1` validator.
- Deterministic checks: exact quotes, the two-item corroboration rule with one
  quote inside authenticated fields, the newer-evidence rule for `changed`,
  replacement bounds, the novelty check, supersede targets, the local scan,
  the confidence floor, and agreement between the seats on the resulting op
  (op type, supersede target, and text after anchor and whitespace
  normalization).
- Mapping from verdicts to `replace_block`, `add_previously`,
  `supersede_card` and `append_evidence`, one card per proposal.

Tests (`tests/test_memory_verify_contract.py`):

- Every verdict maps to the documented op, or to none.
- Replies with unknown keys, unknown blocks, a wrong card id or over 16 KiB are
  rejected whole.
- A correction quoting one item, two dependent items, or only command output
  abstains.
- A replacement that adds a URL, path, host, email, code span, flag or long
  number absent from the block and quotes is rejected.
- A confirm seat with a different verdict makes the block abstain with
  `seat_disagreement`, and one whose reply fails the checks abstains with
  `verify_unconfirmed`.
- Both seats say `contradicted` with replacements that differ in wording,
  case or punctuation: the block abstains with `seat_disagreement`.
- Replacements that differ only in claim anchors or in runs of ASCII spaces
  and tabs outside code spans agree, and the proposal carries the proposing
  seat's exact bytes.
- A replacement or `previously` text containing a newline or carriage return
  is rejected by check 4 before agreement is tested.
- Replacements that differ in spacing inside a code span, or in a non-ASCII
  whitespace character, abstain with `seat_disagreement`.
- A `superseded` verdict on a card with two or more claim blocks abstains
  with `supersede_partial` and opens no `supersede_card`, while the same
  verdict on a single-block card can.
- Both seats say `superseded` with different `other_card` targets: the block
  abstains with `seat_disagreement`.
- One seat proposes `replace_block` and the other `add_previously` for the
  same block: the block abstains with `seat_disagreement`.
- The adversarial and shared-source fixtures from slice 11 produce no op
  through the fake seats.
- A marker in card text cannot match the per-call nonce.

Security review focus: the corroboration and novelty rules against crafted
inputs. This slice gets the deepest review of the client set.

## Slice 9. Submission, manifests, failure and the care entry

Depends on slices 3, 4 and 8. Design: D4 (verifier principals), D5 (the
manifest) and D6.

Changes:

- A new `src/brigade/memory_verify_run.py`: `memory verify run`, which reuses
  the S19d request id ledger, refusal storage and quota handling from
  `src/brigade/memory_reflect_hub.py`.
- The pre-run check of the `status` booleans and the floor capability, with no
  inference from preflight hold reasons.
- Manifest attachment with `evidence.attach`, the manifest scan, and the
  `append_evidence` op that binds it to the proposal.
- Owner-only review packets with trust labels and scan findings, the 64 MiB
  cap and 14-day expiry.
- The pending ceiling via `proposal_show` on remembered proposals.
- The `memory-verify` care entry in `src/brigade/care_schedules.py` and
  `src/brigade/care_launchers.py`, at 11:00 by default, refusing admin
  credentials and the reflect job's folder.
- The git commit resolver passed to `verify_card(resolve_commit=...)`,
  following the "Commit resolver contract" in D3 rule by rule, and the
  `commit_refs` key on `[[verify.workspace]]` with its default of the
  remote-tracking default branch.
- After the resolver tests below pass, lower `MIN_ABBREVIATION` in
  `src/brigade/memory_verify_contract.py` from 12 to 7. Exact equality with
  the unique authenticated candidate plus allowed-ref reachability means a
  ground commit must land on an allowed ref. In the card sample, 1 of the 61
  accepted marked abbreviations qualifies at 12 and 52 at 7.

Tests (`tests/test_memory_verify_run.py`, with a `FakeHub` subclass in the style
of `tests/test_memory_reflect_hub.py`):

- A run against a hub without the floor, or with `verifier` false or
  `reflect_principal` true, fails before any model call or hub write.
- A dry run makes no hub write, writes no packet and leaves the pause counter.
- A real run submits pinned proposals with one card each and a manifest
  `append_evidence` op.
- Three failures pause scheduled calls, and a manual success clears them.
- The commit resolver returns None for each case in D3's contract: an
  abbreviation under the minimum, a repository outside the bound workspace, a
  shallow clone, a partial clone, a missing or unset allowed ref, a failed
  fetch, a fetch from an earlier run, no matching object, an abbreviation
  ambiguous only because of an object on another ref, a match that is not a
  commit, a commit not reachable from any allowed ref, and any git error or
  timeout. A branch or tag named like the abbreviation never answers. It
  returns the full id for a unique, fresh, reachable commit.
- Principal and scope quota replies stop proposing without a failure.
- A duplicate pending reply records the existing id.
- The pending ceiling stops new proposals.
- Installing into the reflect job's folder is refused.

Read count snapshots (carried from the slice 5 review): this slice adds the
producer for the cached counts in `.brigade/memory/verify/state.json`. After a
complete `read_stats` pagination of a scope, a non-dry run stores the whole
scope snapshot (per-card `reads` and the scope's `counts_ms` from the program
clock) in one atomic state write. Only then may a later run score a
rate-limited scope from that snapshot, within a bounded age, and never use it
for the never-read report. Slice 5 validates both fields but never reads them.
Same-user tampering with the state file stays out of scope, as with the S24
marker ruling, so the snapshot carries no HMAC.

Security review focus: what reaches the hub, packet permissions and expiry,
and the pre-run credential check.

## Slice 10. Review command and decision feedback

Depends on slices 4 and 9. Design: D5 (commands).

Changes:

- `memory verify show <proposal-id>`: checks the local manifest's sha256
  against the `artifact_sha256` from `proposal_show`, then the packet against
  the manifest's `packet_sha256`, and prints nothing else on a mismatch. On a
  match it prints per-item headers with trust rank, authentication, whether
  the item counted toward corroboration and scan findings, then the quotes,
  and re-resolves cited files as `current`, `changed` or `missing`. Terminal
  and `--json` output only.
- The next run reads remembered proposal states, stores rejections against
  card revision and evidence digest, and keeps accept and reject counts per
  verdict.

Tests (`tests/test_memory_verify_show.py`):

- A tampered manifest or packet fails the chain and prints no evidence.
- A caller without `review` gets no manifest list and the command refuses.
- Headers show trust, authentication and findings ahead of quotes.
- A rejected correction is not proposed again until the revision or evidence
  changes.
- No log or state file gains text.

Security review focus: the hash chain and the absence of text outside the
terminal.

## Slice 11. Evaluation harness and seeded corpus

Depends on slice 10. Design: D7.

Changes:

- `tests/fixtures/memory_verify_eval/`: about 70 synthetic cards with evidence
  in the five groups and a ground-truth file. All hosts, paths and addresses
  use reserved example values.
- `memory verify eval --corpus <dir>`: builds a scratch replica, runs the pass
  with the configured seats and no submissions, and reports the D7 metrics as
  JSON, including a one-model run for the correlation comparison.
- A CI test that runs the harness with scripted fake seats.
- An operator section in [memory-operations.md](memory-operations.md) with
  the shadow and go/no-go steps.

Tests (`tests/test_memory_verify_eval.py`):

- The fake seat run reports the expected metrics exactly.
- The harness never contacts a hub and never writes outside its scratch folder.
- The corpus passes the content guard with no baseline additions.

Security review focus: the corpus holds no real private data, and the harness
cannot be pointed at a live hub.

## Rollout

1. Merge and deploy slices 1 to 4. Upgrade every hub instance before granting
   the verify principal anything.
2. Enroll the verify principal with the `verifier` flag and grant `read`,
   `reflect` and `read_stats` on one scope. Confirm `status` shows `verifier`
   true and `reflect_principal` false.
3. Merge slices 5 to 11. Run the seeded evaluation with the production seats
   twice.
4. Run shadow mode for 14 days, then live at 2 proposals per run for 28 days,
   as D7 describes. Raise the cap only after the bar is met.
