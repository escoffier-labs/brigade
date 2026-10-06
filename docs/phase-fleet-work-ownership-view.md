# Fleet work ownership read view

Refs #1595. This first slice adds `brigade fleet work ownership show <id>`
with optional `--json`, composing the existing Worklore item and ownership
GET projections. It introduces no new authority or persistence.

## Existing capabilities

Worklore already has optimistic item versions, append-only ownership events,
fenced checkpoint/report/handoff/release metadata, and authenticated item and
ownership GET clients. Ownership revisions and generations fence Hub metadata;
they do not establish filesystem exclusivity or process liveness. OpenClaw
remains the canonical memory authority.

## Contract and bounded plan

1. Add CLI tests for GET-only transport, recognized projections, independent
   revisions/observations, absent metadata, malformed replies, and safe refusals.
   Run them through Brigade and observe the missing command fail.
2. Add a standalone CLI module and minimal registration in `fleet.py`. Extract
   the existing Worklore parser block into `fleet_work.py`, passing the original
   dispatch handlers explicitly to preserve flags, defaults and monkeypatch
   seams while leaving room under the unchanged module-size ceiling. Read the
   item first, then ownership, using existing client settings and authentication.
   Select bounded known fields, validate recognized values, and omit unrelated
   item bodies, links, events, credentials, holder hashes/nonces and unknown fields.
3. Regenerate the command inventory, add release notes, then run the focused
   gate through Brigade with CLI help, inventory, module-size and existing
   Worklore CLI/parser tests. Commit
   the candidate for accountable root integration and review.

Output retains item version, ownership revision/generation, checkpoint source
revision, report sequence and source observation time separately. Each GET has
its own local completion timestamp. `consistency: separate-reads` explicitly
permits intervening writes; there is no atomic combined revision. Report
freshness is `unassessed`: this read has no provider liveness or expiry proof.
Absent checkpoint/report metadata is `unobserved`; unowned is a real ownership
state, distinct from a missing item. Liveness is always `unknown` and conflict
checking is always `not-performed`. Reported evidence remains unverified and
provider lifecycle remains unobserved.

Failures exit 1, with fixed JSON on stderr under `--json` and plain safe text
otherwise. `not-found` means missing; stale revision/generation refusals mean
stale; transport/Hub refusals mean unavailable. Invalid recognized projections
are refused without reflecting remote messages, field names or field values.
No partial success is emitted when either read fails. All output strings use
the existing private-data and terminal-control validation.

## Remaining #1595 criteria

Path arbitration, eligibility/scheduling, dependency reconciliation, actual
handoff orchestration and controlled three-owner acceptance remain open.
There are no ownership writes, provider calls, queues, schemas, migrations,
new runtime dependencies or deployment changes in this slice. Expansion needs
an explicitly authorized follow-on contract, not inferred operator authority.
