# Live-scope retrieval evaluation

Issue #2376 follows the automatic-memory decision in #2591 on 2026-11-15.
The public fixture gate remains available for CI. Real lexical scopes get a
separate, proposal-bound gate, without copying private queries into fixtures.

## Automatic judgments

Successful explicit hub searches retain their query and scoped result identities
in owner-only state under `~/.brigade/private-retrieval-eval/`, outside the
workspace. An interactive card open within ten minutes marks the latest matching
search as used. Returned results and automatic recall alone earn no judgment.
The join requires the same scope and card identity. Repeated normalized queries
count once. Existing read totals from #2449 cannot reconstruct query relevance,
and prompt hashes cannot reconstruct query text, so neither becomes a label.

Derivation requires 20 distinct used queries. This limits a single query's
contribution to aggregate hit rate to five percentage points. It is a cold-start
floor, not a claim of statistical significance. Insufficient history holds the
gate with counts and a fixed reason. Recording and derivation require no manual
query authoring. Search history is capped at 500 entries. The derived query set
is saved privately with 0700 directories and 0600 files. Public reports contain
aggregate metrics and opaque identities only.

## Measurement and binding

The harness reads the live scope through the hub log and exact card revisions.
Its corpus identity hashes sorted card ids and normalized content hashes.
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
