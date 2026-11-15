# Live-scope retrieval evaluation

Issue #2376 follows the automatic-memory decision in #2591 on 2026-11-15.
The public fixture gate remains available for CI. Real lexical scopes get a
separate, proposal-bound gate, without copying private queries into fixtures.

## Automatic judgments

Searches and uses are append-only hub records, bound to the authenticated node
credential, an explicit interactive session id and the hub clock. The hub records
its own search results and mints the search id. The client sends a card identity,
content hash and session context on open. The hub binds that use to its latest
matching search id in the same session within ten minutes. The evaluator reads
judgments from the hub and never reads local `history.json`. Clients cannot
supply search result identities or gold sets.
Missing session kind or identity, worker and scheduled sessions, run jobs and
scanner jobs earn no labels. Both sides share this filter. Harnesses pass
`session_id` and an affirmative `session_kind` in hook payloads. CLI searches use `BRIGADE_SESSION_ID` and
`BRIGADE_SESSION_KIND`. Missing context holds the gate rather than guessing
whether a headless process is interactive.

The judged set needs 20 normalized query strings, three distinct sessions and
three UTC days. No card may be gold for more than 20 percent of those queries.
Twenty queries cap one query's hit-rate contribution at five percentage points.
Three sessions and days prevent one sitting or one day's batch from qualifying.
The card cap requires at least five independently used cards. These are
conservative cold-start floors, not statistical independence or truth claims.
Distinct query means lowercasing and collapsing whitespace. Punctuation and
paraphrases remain different strings and may express the same information need.

Labels expire after 14 days. A later revision, retirement, correction, trusted supersede
or contrary or negative evidence on a card removes the old label. Missing gold
ids are dropped and empty queries are discarded before checking every floor.
The hub recomputes the derived-set digest and count at record and apply, using
the report's frozen history watermark. Newly invalidated or expired labels hold
an old report. Reports carry only digests and aggregates, never raw queries.

Budgets fail closed with `live evaluation budget exceeded`: 10,000 corpus log
events, 2,000 cards, 16 MiB aggregate response bytes, 2,500 hub requests and 60
seconds per evaluation. Hub history has a separate 10,000-row, 16 MiB total
storage ceiling and a 500-row derivation ceiling. Reaching a ceiling requires
operator maintenance, never automatic truncation into a passing sample.

Hub storage prevents workspace file forgery. Session kind is still asserted by
the authenticated client. A compromised process holding the interactive node
credential can impersonate that client. A separately isolated harness credential
issuer would be needed to prove human presence. The diversity rules bound the
reproduced single-card attack, but use remains a relevance proxy. Claim changes
still require the signed evidence and canary gates from #2591. This gate alone
cannot establish that a frequently opened card is true.

## Measurement and binding

The harness reads the live scope through the hub log and exact card revisions.
The `live.binding.corpus_identity` hashes sorted card ids and normalized content hashes.
A live hub raw-lexical replay must match production BM25 over that snapshot.
The same BM25 adapter evaluates the proposal's exact after-text and membership.
Hit rate, MRR, precision at five and recall at five must each remain at least
the before value. The report binds the proposal id and target hashes, the query
set digest, the live corpus identity and the existing index and policy bindings.
The hub rechecks the thresholds and target binding inside the apply transaction.

The #2374 budget remains a corpus-staleness budget, not a metric-loss allowance:
at most `min(5, floor(min(snapshot_count, live_count) / 50))` unrelated changes,
with no changed proposal targets. Metric loss has zero allowance. Hybrid scopes
hold closed because a lexical simulation cannot establish dense or lineage
behavior. Public R0 and R1 evaluation continue unchanged.

## Verification

Cover extra live cards, automatic query derivation, duplicate and unused
queries, insufficient history, private storage and report redaction. Mutate a
candidate to remove useful terms and prove the measured gate refuses it. Exercise
the Rust record and apply boundary against forged passing metrics and a changed
proposal target. Failed evaluation never writes card heads.

## Invocation

After use history accumulates, an automated caller can measure and record a
pending proposal with an explicit evaluation credential:

```text
python -m brigade.memory_retrieval_eval --live-target <workspace> --hub-url <url> --hub-token-file <credential-file> --hub-scope <scope> --proposal-id <proposal-id> --record
```

The credential needs read access to the corpus and review access to candidate
text. Recording retains the existing admin-attestation boundary. This command
never applies the proposal. The existing auto-apply transaction consumes the
record and rechecks every other gate. Existing policies pinned to a public
fixture digest continue to hold live reports. This change does not enable
scheduling or alter the auto-apply default.
