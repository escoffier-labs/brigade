# Memory verification implementation plan

Design: [phase-memory-verify.md](phase-memory-verify.md). Issues: #2385
(model-backed verification pass) and #2387 (access-weighted cadence). Sibling:
#2386 (deterministic link repair), which shares the fairness helper from
slice 3.

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

## Integration baseline

- #2383 (near-duplicate pass and per-scope fair scheduling) must be merged
  before slice 3, which extracts its ordering into a shared helper.
- Memory schema is at version 9 on main. Slice 1 takes version 10. If another
  change takes 10 first, slice 1 rebases onto the next number.
- No slice enables the pass on a live hub. Enabling follows the go/no-go steps
  in D7 after slice 9.

## Dependency order

```text
1 -> 2 -> 3 -> 4 -> 6 -> 7 -> 8 -> 9
               \-> 5 ---/
10 is optional and depends on 7
```

Slices 4 and 5 can proceed in parallel after slice 3.

## Slice 1. Hub read counters

Design: D2 (what the hub records, write path, schema, retention).

Changes:

- `engines/hub/src/memory/schema.rs`: version 10 migration adding
  `memory_read_days` and `memory_read_totals`, and `read_counts_since_day` in
  `memory_meta`. Log epoch, proposal ids and event sequence are unchanged.
- A new `engines/hub/src/memory/read_counts.rs`: the bounded accumulator
  (65,536 keys), the 60-second batched flush through the existing writer,
  saturation, and the drop counter.
- The `card` read and `search` paths in `engines/hub/src/memory/mod.rs` report
  hits to the accumulator after a 200 reply. `eval: true` searches and the
  hub's own searches do not.
- The idle retention sweep removes expired day rows and the totals rows of
  retired cards in bounded transactions.
- `memory_config.rs`: `memory.read_counts.retention_days`, default 35, from 7
  to 365. Unknown keys fail configuration as today.
- `engines/hub/src/telemetry.rs`: the bounded `read_count_dropped` label.

Tests (`engines/hub/src/memory/read_counts_tests.rs`):

- A version 9 database migrates to 10 with the same log epoch, sequence and
  proposal ids. A second start is a no-op.
- A search reply increments `search_hits` once per returned card. A card read
  at offset 0 increments `card_reads`, and a continuation chunk does not.
- `eval: true` searches, `log`, `status` and proposal reads leave counts
  unchanged. Refused reads (403, 404) count nothing.
- The schema holds no principal, query or time-of-day column.
- A full accumulator drops increments and raises the telemetry label. A flush
  writes one transaction.
- The sweep deletes day rows past retention and totals rows of retired cards,
  and stays within its row bound per transaction.
- Retention outside 7 to 365 fails configuration.

Security review focus: nothing identifying is stored, the read path cannot
stall the writer, and the accumulator bound holds under a read flood.

## Slice 2. `read_stats` read action and client

Depends on slice 1. Design: D2 (who can read the counts).

Changes:

- `engines/hub/src/memory/mod.rs`: route `read_stats` through the existing
  read dispatch. It requires `read` and `reflect` on the exact scope, pages at
  most 1,000 rows, and uses an authenticated cursor bound to the principal and
  scope.
- `src/brigade/memory_hub_client.py`: a `read_stats` client call that refuses
  admin credentials, as reflect does.
- Remote connector profiles and fleet digests do not gain the field.

Tests:

- `read` alone, `reflect` alone and a grant on another scope each return
  `403 memory_grant_missing`. Both grants return rows. Collections need their
  exact scope.
- An altered, expired or other-principal cursor is refused.
- Rows hold only the documented numeric fields and card ids.
- The client test uses the existing `FakeHub` in
  `tests/test_memory_hub_client.py`.
- A connector profile test confirms no tool returns read counts.

Security review focus: the cursor, grant checks, and whether counts open any
existence oracle beyond what `read` already gives.

## Slice 3. Verify folder, state, scoring and never-read report

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
- `[verify]` and `[[verify.workspace]]` parsing in `.brigade/memory-care.toml`.
  The workspace file still cannot name a hub URL or credential path.
- Commands: `memory verify status` and `memory verify run --dry-run --no-model`,
  which prints selected card ids, scores and skip counts with no text.

Tests (`tests/test_memory_verify_select.py`, `tests/test_memory_verify_state.py`):

- Score values for fixed inputs, including caps and a never-verified card.
- The staleness lane takes a quarter of the slots, rounded up, even when
  other cards have far higher read counts.
- The minimum interval and the 30-day interval for unchanged supported cards.
- Ties follow the shared helper, and the near-duplicate tests still pass.
- Never-read stays null until counting covers the window, then counts only
  cards first seen before the window with no reads.
- State files over the limit, malformed or unsafe fail closed with a fixed
  reason and no text.
- No output path prints card text.

Security review focus: state file safety and the absence of text in every
output.

## Slice 4. Evidence assembly

Depends on slice 3. Design: D3 (inputs per card).

Changes:

- A new `src/brigade/memory_verify_evidence.py`. It resolves cited references
  with the resolver and evidence roots in
  `src/brigade/memory_reflect_candidates.py`, mapped through
  `[[verify.workspace]]`, plus evidence bundles from the local archive.
- Related excerpts through `brigade evidence search` with a deterministic
  query, limited to the archive projects the workspace entry names.
- Per-item, per-card and per-run byte bounds, trust ranks, and scans of every
  excerpt with the memory scan bundle.
- Reason codes: `evidence_unreachable`, `evidence_flagged`, `no_evidence`.

Tests (`tests/test_memory_verify_evidence.py`):

- Each evidence kind resolves to the right trust rank.
- A path outside the evidence roots, a symlink, or `..` is not read.
- S19e-1 attested ids count as unreachable.
- Bounds cut at the documented sizes and never split a UTF-8 sequence.
- A flagged excerpt is dropped, and a card left with no high or medium
  evidence becomes `no_evidence`.
- The query depends only on the card title and block terms.

Security review focus: path handling, private evidence staying on the host,
and scan coverage of every byte that reaches a prompt.

## Slice 5. Isolated seat dispatch

Depends on slice 3. Design: D3 (seat) and D4 (private cards and connector
audiences).

Changes:

- A new `src/brigade/memory_verify_seat.py` that reuses roster seat
  resolution, model pinning and CLI adapters from the `brigade run --worker`
  path, without planning, synthesis, fleet claims or worktrees.
- An isolation probe per CLI adapter, run by `memory verify doctor`, that
  proves the call has no tools, no recall and an empty private working
  directory. Seats it cannot prove are refused with `verify_seat_not_isolated`.
- Refusal of cloud seats and other connector audiences with
  `verify_seat_connector`, and of seats on other nodes in v1.
- Prompt and reply files in an owner-only work directory under the verify
  folder, deleted after use. Nothing goes to the shared run journal.

Tests (`tests/test_memory_verify_seat.py`):

- A scripted fake seat returns fixed replies and timeouts.
- Cloud and remote seats are refused before any prompt is written.
- Work directory and file modes are owner-only, and the files are gone after
  the call, including after a timeout.
- A seat rate-limit reply maps to `seat_quota`, and an auth failure maps to a
  run failure.
- No run journal entry or report holds prompt or reply text.

Security review focus: the isolation probe itself, the connector boundary, and
cleanup on every exit path.

## Slice 6. Prompt contract, output validation and op mapping

Depends on slices 4 and 5. Design: D3 (prompt contract, output schema,
deterministic checks, mapping to ops) and D4 (prompt injection).

Changes:

- A new `src/brigade/memory_verify_contract.py`: the versioned instructions and
  their digest, nonce markers, and the `brigade.memory-verify.v1` validator.
- Deterministic checks: exact quotes, trust rank rules, the newer-evidence rule
  for `changed`, replacement bounds, the novelty check, supersede targets, the
  local scan, the confidence floor and the confirmation call.
- Mapping from verdicts to `replace_block`, `add_previously`,
  `supersede_card` and `append_evidence`, one card per proposal.

Tests (`tests/test_memory_verify_contract.py`):

- Every verdict maps to the documented op, or to none.
- Replies with unknown keys, unknown blocks, a wrong card id or over 16 KiB are
  rejected whole.
- A quote that is not an exact substring, or only low-trust quotes, abstains.
- A replacement that adds a URL, path, host, email, code span, flag or long
  number absent from the block and quotes is rejected.
- A confirmation call that disagrees abstains with `verify_unconfirmed`.
- The adversarial fixtures from slice 9 produce no op through the fake seat.
- A marker in card text cannot match the per-call nonce.

Security review focus: the novelty check and quote rules against crafted
inputs. This slice gets the deepest review of the set.

## Slice 7. Submission, manifests, failure and the care entry

Depends on slice 6. Design: D4 (keeping the pass out of Tier 0), D5
(manifest) and D6.

Changes:

- A new `src/brigade/memory_verify_run.py`: `memory verify run`, which reuses
  the S19d request id ledger, refusal storage and quota handling from
  `src/brigade/memory_reflect_hub.py`.
- Manifest attachment with `evidence.attach`, `contrary` or `support` stance,
  and the manifest scan.
- Owner-only review packets with the 64 MiB cap and 14-day expiry.
- The Tier 0 guard: `preflight` on a remembered proposal before the first
  submission when one exists, and on the first new proposal of each run. A
  satisfied policy gate pauses the job with `verify_principal_tier0_listed`.
- The pending ceiling via `proposal_show` on remembered proposals.
- The `memory-verify` care entry in `src/brigade/care_schedules.py` and
  `src/brigade/care_launchers.py`, at 11:00 by default, refusing admin
  credentials and the reflect job's folder.

Tests (`tests/test_memory_verify_run.py`, with a `FakeHub` subclass in the style
of `tests/test_memory_reflect_hub.py`):

- A dry run makes no hub write, writes no packet and leaves the pause counter.
- A real run submits pinned proposals with one card each and a manifest
  `append_evidence` op.
- Three failures pause scheduled calls, and a manual success clears them.
- Principal and scope quota replies stop proposing without a failure.
- A duplicate pending reply records the existing id.
- A preflight showing a satisfied policy gate pauses the job.
- The pending ceiling stops new proposals.
- Installing into the reflect job's folder is refused.

Security review focus: what reaches the hub, packet permissions and expiry, and
the Tier 0 guard.

## Slice 8. Review command and decision feedback

Depends on slice 7. Design: D5.

Changes:

- `memory verify show <proposal-id>`: reads the packet, re-resolves cited
  files as `current`, `changed` or `missing`, and with a `review` grant checks
  the packet digest against the manifest hash from `preflight`. Terminal and
  `--json` output only.
- The next run reads remembered proposal states, stores rejections against
  card revision and evidence digest, and keeps accept and reject counts per
  verdict.

Tests (`tests/test_memory_verify_show.py`):

- Output fields match the packet. A tampered packet fails the digest check.
- A changed or missing evidence file is marked.
- A rejected correction is not proposed again until the revision or evidence
  changes.
- No log or state file gains text.

Security review focus: the digest check and the absence of text outside the
terminal.

## Slice 9. Evaluation harness and seeded corpus

Depends on slice 8. Design: D7.

Changes:

- `tests/fixtures/memory_verify_eval/`: about 60 synthetic cards with evidence
  in the four groups and a ground-truth file. All hosts, paths and addresses
  use reserved example values.
- `memory verify eval --corpus <dir> --seat <seat>`: builds a scratch replica,
  runs the pass with no submissions, and reports the D7 metrics as JSON.
- A CI test that runs the harness with the scripted fake seat.
- An operator section in [memory-operations.md](memory-operations.md) with
  the shadow and go/no-go steps.

Tests (`tests/test_memory_verify_eval.py`):

- The fake seat run reports the expected metrics exactly.
- The harness never contacts a hub and never writes outside its scratch folder.
- The corpus passes the content guard with no baseline additions.

Security review focus: the corpus holds no real private data, and the harness
cannot be pointed at a live hub.

## Slice 10. Hub-side verifier tier floor (optional)

Depends on slice 7 and the answer to Q6.

Changes: a policy list of verifier principals whose proposals the classifier
always marks Tier 2, checked at submit, preflight and apply. The list narrows
authority and grants nothing.

Tests: a verifier's `append_evidence` proposal stays Tier 2 under a policy
that lists `append_evidence` in `tier0_op_classes` and the verifier in
`reflect_principals`. Policy digests change when the list changes.

Security review focus: that the floor cannot be bypassed through policy
staleness or replay.

## Rollout

1. Merge slices 1 and 2. Upgrade every hub instance to schema 10 before
   granting the verify principal anything.
2. Merge slices 3 to 9. Run the seeded evaluation with the production seat
   twice.
3. Create the verify folder and principal with `read` and `reflect` on one
   scope. Confirm the principal is absent from `reflect_principals`.
4. Run shadow mode for 14 days, then live at 2 proposals per run for 28 days,
   as D7 describes. Raise the cap only after the bar is met.
