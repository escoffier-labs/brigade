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

On real scopes only Tier 0 proposals whose every target keeps an equal
ranking digest can pass the retrieval gate without measurement. The ranking
digest comes from the same function the indexer uses. It covers scope, card id,
live status, normalized tokens, normalized literal text, title, summary, tags,
trust, lineage links and the `automated` value as it will be after the apply. It
leaves out revision, head sequence, content hash and raw text. Changed document
membership always holds. Hybrid scopes hold.

Search indexes `ranking_text`, not the stored card. It drops the single-line
`evidence:` front-matter line, by the same key rule the proposal writer uses, and
the ` ^c-xxxxxx` anchor suffixes. Both are still stored and shown, and neither
is searchable. So only an op list made entirely of `append_evidence` and
`anchor_blocks` is admitted, and each must leave `ranking_text` and every other
ranking input equal. An apply rewrites the card's writer class to the applier's,
so a target whose `automated` value would flip holds. A mixed op list, a stale
base revision hold. Removing a document changes BM25 document counts and
average length, so a duplicate merge is admitted under a bounded drift instead
of a digest: the keeper keeps every search token of the retired card, trust and
`automated` match, no live card links to the retired id, and the keeper changes
only by the aliases and evidence the merge writes. See
[memory operations](memory-operations.md) for the reason codes. Cosmetic `replace_block` is Tier 1 and stays
outside this authorizer.

The one ranking input the digest cannot see is lineage recency, because every
apply gives the target a new revision. A trusted target that declares
`superseded_by`, `supersedes`, `merged_from` or `aliases` links, or that another
live card in its scope names, is classified Tier 2 on real scopes. Its
automatic attempt ends as a pending proposal held with
`lineage_requires_review`. The reflect client leaves that proposal open for a
reviewer, with no withdrawal and no retry. The gate repeats the check as
`retrieval_eval_lineage_exposed` for a link added after tiering, and for a
target that is an end of a trusted edge in the published index. The naming-card
scan prefilters on the four lineage key names, because `Links::parse` decodes
values with `literal_eval` and a key cannot be escaped, while an id can be.

The digest covers ranking and literal search only. Push and connector search
also read `writer` and `writer_class`, and an apply writes the applier's onto
the head. A target with `agent-unreviewed` trust whose head writer differs from
the applier's principal or class holds with `retrieval_eval_writer_changed`.
The automatic path denies such a target earlier, with `card_trust_denied`, so
this hold is the backstop for a later change to that check. The literal scan
spends its byte budget on the text it searches, so an admitted append cannot
push later matching cards past it.

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
