# Live-scope retrieval evaluation

Issue #2376 follows the automatic-memory decision in #2591 on 2026-11-15.
The public fixture gate remains available for CI. Real lexical scopes get a
projection-invariance gate computed by the hub, without copying private queries
into fixtures. Live labels never authorize automatic apply.

## Automatic judgments

Searches and uses are append-only hub records, bound to the authenticated node
credential, an explicit interactive session id and the hub clock. The hub records
its own search results and mints the search id. The client sends a card identity,
content hash and session context on open. The hub binds that use to its latest
matching search id in the same session within ten minutes. The evaluator reads
judgments from the hub and never reads local `history.json`. Clients cannot
supply search result identities or gold sets.
Missing session kind or identity, worker and scheduled sessions, run jobs and
scanner jobs earn no labels. Both sides share this filter. Wired harnesses must pass
`session_id` and an affirmative `session_kind` in hook payloads. CLI searches use
`BRIGADE_SESSION_ID` and `BRIGADE_SESSION_KIND`. Missing context collects no labels. A headless process has no independently
attested interactive status.

Any surviving label may veto. The former 20-query, three-session, three-day
and 20 percent card-share floors are removed. They did not establish independent
relevance because a node-credential holder can manufacture every session and
label. No amount of live history can authorize a proposal.

Labels expire after 14 days. A later revision, retirement, correction, trusted supersede
or contrary or negative evidence on a card removes the old label. Missing gold
ids are dropped and empty queries are discarded before comparing individual gold-card ranks.
The hub recomputes the derived-set digest and count at record and apply, using
the report's frozen history watermark. Newly invalidated or expired labels hold
an old report. Reports carry only digests and aggregates, never raw queries.

Budgets fail closed with `live evaluation budget exceeded`: 10,000 corpus log
events, 2,000 cards, 16 MiB aggregate response bytes, 2,500 hub requests and 60
seconds per evaluation. Hub history has a separate 10,000-row, 16 MiB total
storage ceiling and a 500-row derivation ceiling. Reaching a ceiling requires
admin maintenance, never automatic truncation into a passing sample.
Each principal is limited to 1,000 rows and 2 MiB, each credential to 500 rows
and 1 MiB. Both are also limited to 120 rows per minute. These share the global
ceiling. The admin-only `retrieval_history_rotate` action deletes at most 500
rows per call older than 14 days plus the 24-hour report lifetime and ten-minute
join window. It preserves recent frozen evidence and never reuses sequence ids.
Rotation is explicit, audited, and available through the authenticated writer.

Hub storage prevents workspace file forgery. Session kind is asserted by the
authenticated client. Harnesses do not currently supply this automatically.
Label collection requires matching explicit session context on searches and
opens. Missing wiring collects no labels and cannot authorize anything.
Residual: a node-credential holder can stall automation with veto labels.
That fails closed and is visible in the preflight hold and private history.

## Projection invariance and vetoes

On real scopes only Tier 0 proposals with equal before and after projection
hashes on every target can pass the retrieval gate without measurement. The hub
uses the indexer's projection function, including indexed tokens, searchable
fields, scope, identity, live status, trust, lineage, automated origin and
revision recency. Changed document membership always holds. Hybrid scopes hold.

No current Tier 0 op class is unconditionally invariant. `append_evidence`
indexes the evidence metadata as part of the full card text and advances
revision recency, so ordinary evidence appends hold. `merge_exact_duplicates`
never qualifies in this PR, even for equal fingerprints: removing a document
changes BM25 document counts and average length. Only an unchanged full
projection passes the hash check. Cosmetic `replace_block` is Tier 1 and stays
outside this authorizer. Future op classes must prove all projection inputs stay
equal. This PR does not change the indexer's text or ranking semantics to make
an operation qualify.

Live evaluation is diagnostic and a per-query veto only. Every surviving gold
card must rank at least as well after the proposal. A gain on another query or
another gold card cannot offset a regression. Aggregate metrics remain visible
but never authorize. The hub replays the current bounded history against the
proposal under the apply transaction, including labels added after measurement.
Expiry, revision invalidation, retirement, supersedes and negative evidence still
remove labels. Invalid or over-budget history holds closed. The public fixture
scope `repo:brigade/retrieval-eval-fixture` keeps the existing R0 and R1
behavior for tests and CI. Fixture reports cannot authorize any other scope.

## Migration order

PR #2606 owns schema version 14. This PR owns version 15 and keeps its history
DDL self-contained for databases at versions 13 and 14. Until #2606 merges,
version 14 is reserved here without its DDL. After it merges this PR must be
rebased and rechecked with the actual sequential 13 to 14 to 15 upgrade,
including both `memory_verify_evals` and `memory_retrieval_history`.

## Verification

Cover extra live cards, automatic query derivation, duplicate and unused
queries, insufficient history, private storage and report redaction. Mutate a
candidate to remove useful terms and prove the measured gate refuses it. Exercise
the Rust record and apply boundary against forged passing metrics and a changed
proposal target. Failed evaluation never writes card heads.

## Invocation

With explicitly wired session context, an automated caller can measure and record a
pending proposal with an explicit evaluation credential:

```text
python -m brigade.memory_retrieval_eval --live-target <workspace> --hub-url <url> --hub-token-file <credential-file> --hub-scope <scope> --proposal-id <proposal-id> --record
```

The credential needs read access to the corpus and review access to candidate
text. Recording retains the existing admin-attestation boundary. This command
never applies the proposal. The existing auto-apply transaction independently checks projection invariance
and live vetoes alongside every other gate. A passing report grants no authority. Existing policies pinned to a public
fixture digest continue to hold live reports. This change does not enable
scheduling or alter the auto-apply default.
