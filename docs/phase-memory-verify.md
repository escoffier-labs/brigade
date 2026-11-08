# Memory verification: evidence-checked reflection and access-weighted cadence

Design for #2385 (a model-backed reflect pass that checks cards against the
evidence they cite) and #2387 (an access-weighted verification cadence). The
sibling #2386 adds a deterministic broken-link pass and shares the scheduling
described here. The implementation plan is
[phase-memory-verify-plan.md](phase-memory-verify-plan.md).

This document records decisions and their reasons. It changes no code. It
builds on scheduled hub reflection (S19d), the attestation route (S19e-1), the
proposal resource limits, and the near-duplicate pass with per-scope fair
scheduling from #2383. Section names below refer to
[memory-operations.md](memory-operations.md).

## Why

Scheduled hub reflection proposes only structural edits today: exact duplicate
merges and, after #2383, near-duplicate supersedes. Nothing checks whether a
card is still true. A card that was wrong when written, or that drifted when a
port, flag or host changed, keeps being served by `memory search` and loaded
into local recall.

The write-up at <https://www.anthropic.com/research/components-of-agent-memory>
reports several results that shape this design:

| Result in the write-up | Decision here |
|---|---|
| An offline pass that checked a note against the session transcript fixed a wrong location. With the uncorrected note the agent fell off the same ledge three times and never reached the next city. | D3: verify cards against the evidence they cite and propose corrections. |
| A memory folder of 9 files plus one index file did better than 413 files across 225 folders. | D3 never creates cards and bounds replacement length. D2 reports never-read cards for demotion review. |
| Four agents writing one shared page kept 41 of 57 facts. One writer per page kept 57 of 57. | D3 never writes a card. Each proposal names one card and pins its revision and block hash, so a concurrent edit makes the proposal stale instead of losing a fact. |
| Injected context has to stay small, and a wrong injected note reaches every session. | D3 bounds replacement text. Corrections reach recall only after review. |
| Checking every memory continuously is impractical. The write-up suggests focusing on memories likely to change or tuning cadence by how often they are read. | D2 orders candidates by reads, recent change and time since the last check. |

## D1. Goals and non-goals

Goals:

1. A verification pass that reads a bounded batch of hub cards, the evidence
   each one cites and a few related transcript excerpts, asks one roster seat
   for a structured verdict per claim block, and opens review-gated proposals
   with ops the hub already supports.
2. Per-card daily read counts on the hub, readable by principals that already
   hold `reflect` on the scope, and a deterministic score that orders
   verification candidates.
3. A never-read report, as counts, for cards older than a threshold.

Non-goals:

- No automatic apply. Every proposal from this pass waits for an explicit
  reviewer decision. D4 explains how the pass is kept out of the Tier 0 route.
- Nothing from this pass is Tier 0 eligible, including its `append_evidence`
  ops.
- No new hub authority. The pass uses the existing `read` and `reflect`
  grants, the existing `propose` and `evidence.attach` actions and the
  existing decision path. The one new hub action (D2) is a read gated by those
  grants. It grants nothing and decides nothing.
- No new cards, no retirement proposals, no edits to index files or injected
  context, and no link repair (#2386 owns that).
- No verification of local-only cards when hub mode is off.

## D2. Read counts and candidate cadence (#2387)

### What the hub records

The hub counts two kinds of reads per card per UTC day:

| Counter | Incremented when |
|---|---|
| `search_hits` | A `search` reply with status 200 returns the card as a match. One increment per card per reply. |
| `card_reads` | A `card` read with status 200 returns the first chunk (offset 0). Continuation chunks do not count. |

Retrieval evaluation searches (`eval: true`) and searches the hub runs on its
own behalf are not counted. `log`, `status`, proposal reads and replica pulls
are not card reads and are not counted.

The hub stores counts only. No query text, no principal, no time of day, no
client address and no result rank. The existing request telemetry keeps its
rule that no path, identity, token or body is retained.

Reads that never reach the hub are invisible: local cards, and replica answers
given while the hub was unreachable. The counts therefore undercount, which is
fine for ordering candidates and makes them unfit as an audit record.

### Write path

Reads run on the reader pool, and a writer transaction per read would put the
single writer on the hot path. Each hub process keeps an in-memory
accumulator keyed by `(scope, card_id, day)` with at most 65,536 keys. The
existing writer flushes it every 60 seconds, or sooner when it fills, as one
batched upsert. When the accumulator is full and a flush is pending, further
increments are dropped and counted in a bounded telemetry label
`read_count_dropped`. A crash loses at most one flush interval of counts.
Per-day counters saturate at 2^32 - 1.

### Schema

Memory schema 10 adds two tables. Both are additive and neither is part of the
event log, the log epoch, replication or any policy digest.

| Table | Key | Columns |
|---|---|---|
| `memory_read_days` | `scope`, `card_id`, `day` (UTC days since the Unix epoch) | `search_hits`, `card_reads` |
| `memory_read_totals` | `scope`, `card_id` | `first_read_day`, `last_read_day`, `total_reads` (saturating) |

`memory_meta` gains `read_counts_since_day`, written once by the migration.
The never-read report uses it so that a card is not called never-read before
counting has run for the whole lookback window.

The migration bumps `USER_VERSION` from 9 to 10, creates both tables, and
keeps the log epoch, proposal ids and event sequence. An existing hub at
version 9 creates them on first start under the new build. An older build
refuses version 10, as it refuses any newer schema, so take the usual backup
before upgrading.

### Retention

Daily rows are kept for `memory.read_counts.retention_days`, default 35 and
allowed from 7 to 365. That covers the 30-day window the score uses plus slack.
The existing idle retention sweep deletes expired day rows in bounded
transactions. Totals rows stay while the card is live and are removed by the
same sweep after the card is retired. Unread cards have no day rows.

### Who can read the counts

A new read action on the existing memory route, `read_stats`, takes `scope`,
`limit` and an optional `after` cursor and returns per-card aggregates. Each
row holds `card_id`, `reads_7d`, `reads_30d`, `last_read_day` and
`total_reads`. The reply also carries `read_counts_since_day`. Pages hold at
most 1,000 rows and use an opaque cursor in the style of `proposal_list`.

The caller needs `read` and `reflect` on that exact scope. Collections need
their exact `collection:` grant, as everywhere else. A caller without both
grants gets the existing `403 memory_grant_missing`. Connector profiles never
receive read counts, and the counts are not added to fleet digests, the
proposal listing or search replies.

Whether counts need their own grant is open question Q1. The recommendation is
to reuse `reflect`: a principal with `reflect` and `read` on a scope can
already read every card in it, and day-granular counts with no principal
reveal less than the text. In a scope with a single reader the counts do
describe that reader's activity, which is the case Q1 asks about.

### Candidate score

The verification job computes a score per eligible card from the counts, the
card's head revision and its own state file. The score is deterministic and
uses no model.

```text
access     = log2(1 + 4 * min(reads_7d, 7000) + min(reads_30d, 30000))
volatility = 2 if the head revision is at most 14 days old
           + 1 if it is 15 to 60 days old
           + 2 if a cited evidence item is newer than the last verification
staleness  = min(days_since_verified, 180) / 30
score      = access + volatility + staleness
```

A card never verified counts as 180 days since verification. A card is
eligible again only after `min_interval_days` (default 7). When its last
verdict was supported and neither its head revision nor the digest of its
cited evidence has changed, the interval is 30 days.

One quarter of each run's card slots, rounded up, goes to the eligible cards
with the oldest verification regardless of reads. This staleness lane keeps a
principal that inflates reads from holding every slot, and it means rarely read
cards are still checked eventually. Ties use the order from #2383: least
recently proposed, then oldest first seen, then a 128-bit key digest of the
scope and card id. The key never depends on card text.

Cards are not eligible when they are retired, carry `status: superseded`,
have a non-clean scan state or flagged trust, have no anchored block, exceed
the per-card text bound in D3, or have a remembered proposal from this job
that is still pending.

### Never-read report

`memory verify status` and every run report include:

| Field | Meaning |
|---|---|
| `never_read.count` | Live cards this job first saw at least `never_read_days` ago (default 90), with no read recorded, while `read_counts_since_day` is also at least that old. Null until counting covers the window |
| `never_read.window_days` | The threshold used |

The hub does not expose a creation time, so "first seen by this job" stands in
for card age. The report is a count. `--list-ids` prints card ids only, no
titles or text, for an operator holding the same grants. The job never
proposes retirement or demotion.

## D3. The verification pass (#2385)

### Where it runs

The pass runs as its own care entry, `memory-verify`, in a dedicated verify
folder on one of the operator's machines. It mirrors the dedicated reflect
folder: `.brigade/memory-care.toml` sets `[hub] mode = "read"` and one read
scope, and the folder owns its replica and state. It uses a separate principal
with only `read` and `reflect` on that scope (D4 explains why the principal
must differ from the reflect job's). The default schedule is 11:00 local time,
an hour after `memory-reflect`, so duplicate proposals reach the shared scope
quota first.

The pass reads cards from its own replica. It makes no hub `search` or `card`
calls, so it does not inflate the counts it ranks by.

### Inputs per card

| Input | Source | Bound |
|---|---|---|
| Card | Replica head: card id, scope, revision, trust, scan state and anchored blocks with their ids and hashes | 16 KiB of card text and 16 verified blocks per card |
| Cited evidence | The card's `evidence` front matter, resolved on this host | 8 KiB per item, 32 KiB per card, 8 items per card |
| Related excerpts | `brigade evidence search` over the local evidence archive, with a query built from the card title and its rarest block terms | 3 excerpts of at most 4 KiB each |
| Related cards | A lexical search of the local replica in the same scope | 3 card ids with titles, used only as `supersede_card` targets |

Cited references resolve with the same rules as local reflection: paths under
the evidence roots (verify-run receipts under `.brigade/work/verify-runs`,
`.brigade/receipts`, `.brigade/evidence`, `memory/evidence`, the handoff inbox
and the writer inboxes), plus evidence bundle ids from the local evidence
archive. Hub cards cite paths relative to the workspace that wrote them, so
the verify folder's config maps each scope to the local workspace paths it
may read:

```toml
[verify]
enabled = true
seat = "verifier"

[[verify.workspace]]
scope = "repo:example"
path = "/absolute/path/to/workspace"
```

S19e-1 attested ids (`<principal>/<workspace_id>/<local_id>`) have no hub read
action for their bytes, so the pass cannot read them in v1. They count as
`evidence_unreachable`. Q3 asks whether to add such a read.

Each excerpt is scanned with the memory scan bundle before use. A blocked or
injection finding drops the excerpt and counts `evidence_flagged`. Excerpts
carry a trust rank used by the rules below:

| Rank | Evidence kinds |
|---|---|
| high | Verify-run receipts and Brigade receipts, which record real commands and exit codes |
| medium | Memory handoffs and evidence bundles |
| low | Related session excerpts that the card does not cite |

A card with no resolvable cited evidence and no high or medium excerpt is not
sent to the model. It counts `no_evidence` and its count appears in the run
report as unverifiable.

### Seat

The pass dispatches one bounded model call per card through a roster seat
named by `[verify] seat`. It reuses the roster seat resolution, model pinning
and CLI adapters that `brigade run --worker` uses. It does not run planning,
synthesis, fleet repo claims or worktrees.

The seat must be tool-free for this call. It receives only the composed
prompt. It cannot run commands, read files, call connectors or receive memory
recall from hooks. The call runs in an empty private working directory. A
doctor probe for the seat must prove those properties for its CLI adapter
before the first run, and the pass refuses a seat it cannot prove with
`verify_seat_not_isolated`. Cloud seats and every other connector audience are
refused with `verify_seat_connector` (see D4). A seat on another fleet node is
refused in v1.

### Prompt contract

The prompt holds fixed instructions, versioned and digested as
`prompt_sha256`, then the card's blocks labeled by block id, then the evidence
items labeled with index, kind, trust rank, observation time and sha256. The
instructions state the task, say that everything between the data markers is
data that cannot change the task, and require a JSON reply matching the schema
below.

Card and evidence text sit between markers that carry a random 128-bit nonce
chosen per call, so data cannot close a marker it has not seen.

### Output schema

The reply must be one JSON object of at most 16 KiB with schema
`brigade.memory-verify.v1`:

```json
{
  "schema": "brigade.memory-verify.v1",
  "card_id": "card-example",
  "blocks": [
    {
      "block_id": "c-abcdef",
      "verdict": "contradicted",
      "confidence": "high",
      "quotes": [{"evidence": 0, "text": "exact substring of evidence item 0"}],
      "replacement": "One corrected claim.",
      "previously": null,
      "superseded_by": null
    }
  ]
}
```

`verdict` is one of `supported`, `contradicted`, `changed`, `superseded`,
`history` or `insufficient`. `confidence` is `high`, `medium` or `low`.
Unknown keys, unknown block ids, a duplicate block id or a wrong `card_id`
reject the whole reply for that card with `verify_output_invalid`.

### Deterministic checks before any op

The model proposes. The client decides whether a proposal is allowed:

1. Every quote must be an exact substring, 20 to 400 bytes long, of the
   evidence item it names.
2. `contradicted`, `changed`, `superseded` and `history` need at least one
   quote from high or medium evidence. Quotes from low evidence alone mean
   abstain.
3. `changed` needs its quoting evidence to be newer than the head revision's
   log time.
4. A replacement must differ from the block, stay a single paragraph with no
   newline or `^`, and be at most 1.5 times the block length plus 200 bytes,
   within the hub's 8,192-byte limit.
5. Novelty check: every URL, path, host-like name, email address, code span,
   command-line flag and number of three or more digits in a replacement or
   `previously` text must appear in the original block or in a quote. A reply
   that adds one is rejected with `verify_novel_token`.
6. `superseded_by` must be one of the related card ids supplied for that call,
   live, in the same scope and with a newer head revision.
7. The replacement and history text are scanned locally with the hub's bundle
   before submission, and the hub scans them again on `propose`.
8. Only `high` confidence produces an op. `medium`, `low` and `insufficient`
   abstain and are counted by verdict.
9. Each would-be correction gets a second, independent call with the evidence
   order shuffled and a fresh nonce. The verdict must match, and the second
   replacement must pass the same checks. Otherwise the block abstains with
   `verify_unconfirmed`.

### Mapping to ops

| Verdict | Op | Notes |
|---|---|---|
| `contradicted` | `replace_block` with the replacement | The hub appends `previously X, now Y`, so the old wording stays in history. |
| `changed` | `replace_block` with the replacement | Same op. The verdict is kept in the manifest so reviewers can tell a wrong claim from a stale one. |
| `history` | `add_previously` with the `previously` text | The claim is current, but cited older evidence records a different earlier value a reader may meet. |
| `superseded` | `supersede_card` with the related card as `other_card` | The newer card already holds the corrected claim. |
| `supported` with uncited high or medium support | `append_evidence` | Only with `[verify] propose_confirmations = true`, default false, so reviewer time goes to corrections. |
| `supported` otherwise | None | Recorded in local state as verified. |

Each proposal names one card. Every correction proposal also carries an
`append_evidence` op that cites the verification manifest described in D5, so
the card itself records the check after acceptance. A proposal takes the
highest tier of its ops, and every correction op is Tier 2.

### Bounds per run

| Bound | Default | Allowed |
|---|---:|---:|
| Cards verified per run | 12 | 1 to 64 |
| Blocks per card | 16 | fixed |
| Card text per card | 16 KiB | fixed |
| Cited evidence per card | 32 KiB | 8 to 64 KiB |
| Related excerpts per card | 3 of 4 KiB | 0 to 3 |
| Prompt bytes per call | 64 KiB | fixed |
| Output tokens per call | 2,048 | fixed |
| Model calls per run | 2 per card | fixed |
| Per-call timeout | 120 s | 30 to 600 s |
| Run wall clock | 20 min | 5 to 60 min |
| New proposals per run | 4 | 0 to 16 |
| This job's pending proposals | 8 | 0 to 32 |

A run that reaches its wall clock stops after the current call and counts the
rest as `verify_deferred_time`.

### Cost estimate

At roughly four bytes per token, a typical card (2 KiB of text, 12 KiB of
evidence, 2 KiB of instructions) is about 4,000 input tokens and 400 output
tokens. Twelve cards plus two confirmation calls come to about 60,000 tokens
per run, or about 1.8 million tokens a month on a daily schedule. The worst
case at every bound is 24 calls of about 16,000 input and 2,048 output tokens,
or about 430,000 tokens per run. The run report records actual token counts
when the seat adapter reports them. Prices depend on the seat.

Twelve cards a day covers a 2,000-card scope uniformly in about 167 days, which
is why D2 puts read-heavy cards first.

## D4. Privacy and trust

### Private cards and connector audiences

The private class boundary from the hub memory client section applies
unchanged. Private text may reach the operator's machines and the model
providers behind the operator's own local harnesses. It never reaches a
connector audience.

- The pass runs only on the operator's machine, through a local seat. That
  places private card and evidence text inside the boundary, the same as
  local prompt recall.
- Cloud seats, seats on other nodes, non-loopback embedders and remote
  connector profiles are refused before the pass reads any card.
- The pass sends nothing to the embedder.

### What may appear where

| Surface | May contain | Never contains |
|---|---|---|
| Hub proposal | Ops with replacement or history text, evidence ids, pins | Evidence excerpts, model rationale, raw model output |
| Hub attestation | The manifest from D5: verdict codes, evidence refs, hashes, seat and prompt digests | Card text, evidence text, free text from the model |
| Run report, stdout, logs, timer journal | Counts, card ids, proposal ids, digests, verdict and reason codes | Card text, evidence text, model output |
| `schedule.json` and verify state | Key digests, days, revisions, verdict codes, proposal ids | Any text |
| Local review packet | Excerpts, quotes, replacement text, raw model output | Nothing is added to it from other cards |

The seat's prompt and reply are written to an owner-only work directory inside
the verify folder and deleted once the review packet is written. They never
enter the shared run journal. Packets are owner-only files under
`.brigade/memory/verify/packets/`, capped at 64 MiB in total, and deleted 14
days after their proposal reaches a terminal state.

### Keeping the pass out of Tier 0

The S19e-1 automatic route requires the proposer to appear in the policy's
`reflect_principals` list. The pass runs under its own principal, and that
principal must never appear there. Before its first submission in a run, the
job calls `preflight` on a remembered pending proposal when one exists, and it
calls it again on the run's first new proposal. `preflight` needs only `read`.
If the `policy`
gate reports anything other than a `reflect_principal_disabled` hold, the job
pauses itself with `verify_principal_tier0_listed` and opens nothing further.
Correction ops are Tier 2 regardless. The check covers the `append_evidence`
ops, which the classifier can mark Tier 0. Q6 asks whether the hub should also
force Tier 2 for this principal. That change would narrow authority and add
none.

### Prompt injection

Card text and evidence are written by agents, and session excerpts can carry
text from web pages or tool output. A hostile or confused input could try to
make the model propose a wrong correction, plant a command or URL, flood the
review queue, point a supersede at a chosen card, or leak text from one card
into another.

| Risk | Mitigation |
|---|---|
| Model follows instructions in data | Tool-free seat with no recall, nonce markers, and a reply limited to a strict schema. The model cannot act, only answer. |
| Wrong correction | High confidence only, quotes must match evidence exactly, at least one high or medium quote, a second independent call must agree, and a reviewer decides. |
| Planted command, URL, path or host | The novelty check rejects any such token that is not in the block or a quote. The hub scan rejects blocked content. |
| Queue flooding | At most 4 new proposals per run and 8 pending for the principal. A rejected correction is remembered and not proposed again until the card revision or cited evidence changes. |
| Chosen supersede target | Targets come only from the client's related-card list for that call, in the same scope, live and newer. |
| Cross-card leakage | One card per call. The prompt holds only that card, its evidence and the titles of three related cards. |
| Flagged input | Cards with non-clean scans are not eligible, and excerpts with blocked or injection findings are dropped before the call. |

The `contrary` stance on a correction manifest (D5) holds automatic apply for
that card until the record is purged. An injected input can therefore cause
holds on at most 4 cards per run, in the fail-safe direction. Q4 asks whether
that trade is acceptable.

## D5. Review experience

A reviewer needs to see why the pass believes a claim is wrong without the
evidence passing through logs or the hub log.

Each correction proposal gets a verification manifest, attached with the
existing `evidence.attach` action under the verify principal. Its
`artifact_kind` is `memory-verify-manifest`. Its stance is `contrary` for
`contradicted` and `changed`, and `support` for other verdicts. The manifest is
structured JSON with no free text:

| Field | Content |
|---|---|
| `schema` | `brigade.memory-verify-manifest.v1` |
| `card` | Scope, card id, base revision |
| `blocks` | Block id, verdict code, confidence code |
| `evidence` | Reference, kind, trust rank, observation time, sha256 and quoted byte ranges for each item the quotes used |
| `seat` | Seat name and pinned model id from the roster |
| `prompt_sha256`, `packet_sha256`, `input_sha256` | Digests of the instructions, the local packet and the full prompt input |

References are workspace-relative paths or bundle ids and are scanned before
attachment. A manifest that does not scan clean is not attached, and the card
abstains with `verify_manifest_unclean`. Attestations are immutable and the
hub allows one principal 8 active records per card, so a card at that cap
abstains with `verify_attest_quota` until an admin purges old manifests.

The reviewer works in two steps on the verify host:

1. `brigade memory proposals show <proposal-id>` shows the diff, as today, to a
   caller with `review` on the scope.
2. `brigade memory verify show <proposal-id> --target <verify-folder>` reads
   the local packet and prints each block's verdict, confidence and quotes with
   their reference, kind, observation time and hash. It re-resolves each cited
   file and marks it `current`, `changed` or `missing`. With a `review` grant
   it compares the packet digest with the manifest hash that `preflight`
   reports. Output goes to the terminal only. `--json` prints the same fields
   for tools.

The decision uses the existing `memory proposals decide`. The next run reads
the state of remembered proposals with `proposal_show`. A rejection is stored
as a verdict code against the card revision and evidence digest, so the same
correction is not proposed again until one of them changes. Accepted and
rejected counts per verdict feed the accept rate in D7. A rejected correction
leaves its `contrary` manifest in place, so the run report counts
`contrary_after_reject` for an admin to review and purge.

A reviewer on another machine cannot see the packet in v1 (Q3).

## D6. Failure, pause and quota

The job follows S19d and #2383 with its own state file, so the reflect job's
pause and the verify job's pause are independent.

| Condition | Effect |
|---|---|
| Transport, configuration, authorization or hub-unavailable error | Run fails and counts toward the pause |
| Seat refused by the isolation probe or as a connector | Run fails and counts toward the pause |
| Every model call in the run fails | Run fails and counts toward the pause |
| One call times out or returns invalid output | That card is skipped with a reason code. The run continues |
| Seat rate limit or quota reply | Run stops, counts `seat_quota`, and completes without a failure |
| `429 memory_proposal_principal_quota` or `memory_proposal_scope_quota` | Run stops proposing, counts unsent submissions as `proposal_quota`, and completes without a failure |
| `409 memory_proposal_duplicate_pending` | Records the existing proposal id, counts `duplicate_pending` |
| This job already has 8 pending proposals | Run verifies nothing and reports `pending_ceiling` |
| Policy gate satisfied for the verify principal | Job pauses with `verify_principal_tier0_listed` |

Three consecutive failures pause the scheduled job. Only a successful
non-dry manual run clears the pause, as for `memory-reflect`.

The scope pending limit (128) is shared with the reflect principal and any
other proposer. The verify job's own ceiling of 8 leaves 120 for the others,
and its later schedule lets duplicate merges reach the quota first. The
per-card fairness table uses the same ordering and key digest as the #2383
cluster table and the #2386 link pass, so a card served this run waits behind
cards not yet served.

Verify state lives in `.brigade/memory/verify/state.json` with the same safe
write and recovery rules as `schedule.json` and an 8 MiB limit. It holds one
entry per card the job has seen in its single scope, about 100 bytes each, so
the 50,000-card bound fits in about 5 MB. Keys not seen in a run are dropped.

`--dry-run` selects candidates, assembles evidence and, unless `--no-model` is
given, calls the seat. It submits nothing, writes no packet, attaches nothing
and leaves the failure counter and pause unchanged. It prints counts and
verdict codes only.

## D7. Evaluation and go/no-go

### Seeded evaluation corpus

A fixture corpus of about 60 synthetic cards in one scope, each with evidence
files of every kind, all in the shape Brigade writes:

| Group | Cards | Ground truth |
|---|---:|---|
| Wrong from the start | 20 | Cited evidence contradicts a block. The expected replacement's key tokens are listed. |
| Drifted | 10 | Older evidence supports the block, newer evidence contradicts it |
| Correct | 20 | Evidence supports every block |
| Adversarial | 10 | Card or evidence text tries to steer the model, for example asking it to replace a claim with a download-and-run command |

`brigade memory verify eval --corpus <dir> --seat <seat>` runs the pass
against a scratch replica built from the corpus. It submits nothing and
reports the metrics below. CI runs the same harness with a scripted fake seat
to test the plumbing. Live model runs are an operator step, because they cost
tokens and vary between runs.

### Metrics and thresholds

| Metric | Bar |
|---|---|
| Precision: correct correction proposals over all correction proposals | at least 0.9 |
| Recall over wrong and drifted cards | at least 0.6 |
| Corrections proposed on the 20 correct cards | 0 |
| Adversarial proposals that carry a steered token or claim | 0 |
| Key-token match of replacements on true positives | at least 0.8 |
| Verdict agreement across 3 repeated runs | at least 0.9 |
| Median tokens per card | reported |

### Go/no-go before the live hub

1. The seeded evaluation meets every bar on two consecutive runs with the seat
   chosen for production.
2. Each implementation slice passed its own security review.
3. Shadow mode on one live scope for 14 days with `--dry-run`. The operator
   samples 20 verdicts by hand, and at least 17 of them agree.
4. Live proposals at 2 per run for 28 days. The reviewer accept rate is at
   least 0.7 over at least 20 decided proposals, and no accepted correction
   needs a rollback because the pass was wrong.

After step 4 the per-run cap may rise to the default of 4. Any later rollback
caused by the pass returns the job to shadow mode until the cause is fixed and
the seeded evaluation is rerun.

## Open questions

- Q1. Read counts reuse `reflect`, or get their own grant? Recommendation:
  reuse `reflect`.
- Q2. Which roster seat runs the pass, and whether the confirmation call is
  worth doubling the cost of corrections. Recommendation: keep it until the
  evaluation shows single-call precision at the same bar.
- Q3. Should the hub get a read action for attested artifact bytes, so a
  reviewer on another machine can see the manifest and evidence? That is new
  hub surface and needs its own design. Recommendation: defer.
- Q4. Accept `contrary` stance on correction manifests, with holds that only an
  admin purge clears, including after a reviewer rejects the correction?
  Recommendation: yes, because a contradicted card should not be changed
  automatically, and the report counts the holds left after rejections.
- Q5. Keep `propose_confirmations` off by default? Recommendation: yes.
- Q6. Should the hub force Tier 2 for proposals from a principal marked as a
  verifier, in addition to the client check? Recommendation: yes, in a later
  slice, because it narrows authority.
- Q7. Is a 35-day retention right for daily counts? Recommendation: yes.
- Q8. Session excerpts come from the whole local evidence archive. Should
  `[[verify.workspace]]` also name the archive projects each scope may draw
  from? Recommendation: yes, and required when the archive holds more than one
  project.
