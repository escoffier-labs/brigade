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

Revision 2 (2026-11-08) answers the first security review and two design
review threads: a hub-enforced verifier floor (D4), per-scope evidence
isolation and two-item corroboration (D3), manifests that hold nothing before
acceptance and a reviewer hash read (D5), and writer budgets for the read
counters (D2). The open questions became decisions at the end.

Revision 3 (2026-11-08) answers the second security review: the verifier flag
belongs to the principal and every credential it holds (D4), a manifest binds
only to a proposal for the exact card, scope and revision it checked (D5), and
non-reviewers cannot see how many manifests a proposal binds (D5). It also
requires the two seats to agree on the exact resulting op, not only the
verdict (D3, check 9).

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
   each one cites and a few related transcript excerpts, asks a roster seat for
   a structured verdict per claim block, and opens review-gated proposals with
   ops the hub already supports.
2. Per-card daily read counts on the hub, readable under a dedicated audited
   grant, and a deterministic score that orders verification candidates.
3. A never-read report, as counts, for cards older than a threshold.

Non-goals:

- No automatic apply. A human reviewer's explicit decision is the only way a
  proposal from this pass changes a card. The client checks in D3 reduce
  reviewer load. They do not establish that a correction is true.
- Nothing from this pass is Tier 0 eligible, including its `append_evidence`
  ops. The hub enforces this (D4).
- No new write or decision authority. The new hub surface is one read right
  (`read_stats`), a verifier credential attribute and a proposal-bound
  attestation state. Each of them reads data or narrows what a credential may
  do.
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

Reads of local cards, and replica answers given while the hub was down, never
reach the hub. The counts undercount, which is fine for ordering candidates.

### Write path and budgets

Reads run on the reader pool, and a writer transaction per read would put the
single writer on the hot path. Each hub process keeps an in-memory accumulator
keyed by `(scope, card_id, day)`. Every limit below has a configuration key
under `memory.read_counts`, and unknown keys fail configuration as today.

| Budget | Default | Effect when reached |
|---|---:|---|
| Accumulator keys | 65,536 | New keys are dropped and counted as `read_count_dropped` |
| Increments one principal may add per card per day | 64 | Further reads are served but not counted |
| Increments one principal may add per day | 20,000 | Further reads are served but not counted |
| Rows per flush transaction | 2,048 | The rest stay in the accumulator for the next flush |
| SQLite execution time per flush transaction | 20 ms | The transaction stops at a row boundary, commits, and resumes later |
| `memory_read_days` rows | 1,000,000 | New day rows are not inserted. Existing rows still update |
| `memory_read_totals` rows | 250,000 | New totals rows are not inserted. Existing rows still update |
| Rows per retention sweep transaction | 2,000 | The sweep resumes on the next idle tick |
| SQLite execution time per sweep transaction | 20 ms | Same |

The per-principal limits live only in memory, reset at the UTC day boundary,
and stop one credential from flooding the writer or steering the score.

Flushing has low priority. The writer flushes only when its queue is empty, or
when the accumulator is over three quarters full. A flush takes the oldest
keys first, commits each bounded transaction on its own, and leaves the rest
for the next tick, so a flush interrupted by load or restart resumes without
redoing committed rows. The target interval is 60 seconds. A crash loses the
counts not yet flushed. Per-day counters saturate at 2^32 - 1. Each budget
that bites raises a bounded telemetry label.

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

The migration bumps `USER_VERSION` from 9 to 10 and keeps the log epoch,
proposal ids and event sequence. An older build refuses version 10, as it
refuses any newer schema, so take the usual backup before upgrading.

### Retention

Daily rows are kept for `memory.read_counts.retention_days`, default 35 and
allowed from 7 to 365. That covers the 30-day window the score uses plus slack.
The idle retention sweep deletes the oldest expired day rows first within the
sweep budgets above, and removes totals rows after their card is retired.
Unread cards have no day rows.

### Who can read the counts

A new read action on the existing memory route, `read_stats`, takes `scope`,
`limit` and an optional `after` cursor and returns per-card aggregates. Each
row holds `card_id`, `reads_7d`, `reads_30d`, `last_read_day` and
`total_reads`. The reply also carries `read_counts_since_day`. Pages hold at
most 1,000 rows and use an opaque cursor in the style of `proposal_list`.

The caller needs `read` and a new, dedicated `read_stats` right on that exact
scope. `reflect` alone does not open the counts. Collections need their exact
`collection:` grant, as everywhere else. A caller missing either right gets
the existing `403 memory_grant_missing`. One principal may make one
`read_stats` request per second and 600 per day. Over that the hub answers
`429 memory_read_stats_rate_limited`.

Every `read_stats` request writes a row to the hub's audit table with the
principal, scope, time, rows returned and status. The memory event log is not
used, so the record does not replicate to card readers. Connector profiles,
fleet digests, proposal listings and search replies never carry counts.

Privacy note for single-reader scopes: counts carry no principal, but in a
scope that one principal reads, they describe that principal's activity per
card per day. Grant `read_stats` on such a scope only to a principal that the
reader would accept seeing it, and treat the audit table as the record of who
looked.

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
with the oldest verification regardless of reads. This staleness lane keeps
inflated reads from holding every slot, and it means rarely read cards are
still checked eventually. Ties use the order from #2383: least recently
proposed, then oldest first seen, then a 128-bit key digest of the scope and
card id. The key never depends on card text.

Cards are not eligible when they are retired, carry `status: superseded`,
have a non-clean scan state or flagged trust, have no anchored block, exceed
the per-card text bound in D3, or have a remembered proposal from this job
that is still pending.

### Never-read report

`memory verify status` and every run report include `never_read.count`: live
cards this job first saw at least `never_read_days` ago (default 90) with no
read recorded. It stays null until `read_counts_since_day` is also that old.
The hub does not expose a creation time, so "first seen by this job" stands in
for card age. `--list-ids` prints card ids only, no
titles or text, for an operator holding the same grants. The job never
proposes retirement or demotion.

## D3. The verification pass (#2385)

### Where it runs

The pass runs as its own care entry, `memory-verify`, in a dedicated verify
folder on one of the operator's machines. It mirrors the dedicated reflect
folder: `.brigade/memory-care.toml` sets `[hub] mode = "read"` and one read
scope, and the folder owns its replica and state. It uses its own principal,
enrolled as a verifier (D4), with `read`, `reflect` and `read_stats` on that
scope. The default schedule is 11:00 local time, an hour after
`memory-reflect`, so duplicate proposals reach the shared scope quota first.

It reads cards from its own replica and makes no hub `search` or `card`
calls, so it does not inflate the counts it ranks by.

### Inputs per card

| Input | Source | Bound |
|---|---|---|
| Card | Replica head: card id, scope, revision, trust, scan state and anchored blocks with their ids and hashes | 16 KiB of card text and 16 verified blocks per card |
| Cited evidence | The card's `evidence` front matter, resolved only inside the roots bound to the card's scope | 8 KiB per item, 32 KiB per card, 8 items per card |
| Related excerpts | `brigade evidence search`, limited to the archive projects listed for the scope, with a query built from the card title and its rarest block terms | 3 excerpts of at most 4 KiB each |
| Related cards | Not passed in v1 | `superseded` is deferred (check 6), so no related card ids or titles reach the prompt |

### Per-scope evidence isolation

Hub cards cite paths relative to the workspace that wrote them. The verify
folder's config binds each scope to the workspace roots and evidence archive
projects it owns. Nothing outside those bindings is read for that scope.

```toml
[verify]
enabled = true
propose_seat = "verify-propose"
confirm_seat = "verify-confirm"

[[verify.workspace]]
scope = "repo:example"
path = "/absolute/path/to/workspace"
archive_projects = ["example"]
# Optional. Remote-tracking refs the commit resolver may prove reachability
# from. Without it, the single ref refs/remotes/origin/HEAD names.
commit_refs = ["refs/remotes/origin/main"]
```

The rules:

- A workspace root belongs to exactly one scope. A root bound to two scopes,
  or nested inside another bound root, fails configuration.
- `archive_projects` must be listed explicitly for each workspace entry. An
  empty list means the scope gets no excerpts and no evidence bundles.
- A cited path resolves only under the scope's roots and only under the
  evidence roots local reflection uses: `.brigade/work/verify-runs`,
  `.brigade/receipts`, `.brigade/evidence`, `memory/evidence`, the handoff
  inbox and the writer inboxes.
- An evidence bundle id counts only when the archive records it under one of
  the scope's listed projects.
- Ownership that is absent or ambiguous makes the item `evidence_unreachable`
  and it is never read. That covers a path found under no root, a path found
  under two roots of the same scope, a bundle under an unlisted project or
  under more than one project, and every S19e-1 attested id, whose bytes have
  no hub read action.
- A scope with no `[[verify.workspace]]` entry is not verified.

Each item that passes ownership is scanned with the memory scan bundle before
use. A blocked or injection finding drops it from the prompt, counts
`evidence_flagged`, and is recorded in the review packet with its rule ids.

### Evidence trust and independence

| Rank | Evidence kinds |
|---|---|
| authenticated | The authenticated fields of a Brigade receipt (below) |
| medium | Memory handoffs, evidence bundles, and the output fields of receipts |
| low | Related session excerpts that the card does not cite |

A receipt is machine-authenticated when its bytes match the digest that the
local evidence archive recorded when it imported the receipt. Its
authenticated fields are the ones Brigade itself writes: argv, exit status,
run status, tree fingerprint, start and end time, commit, and manifest id.
Captured stdout, stderr, summaries and notes are command output. The client
presents them as a separate item at medium rank, because a command can print
anything. A receipt that the archive has not imported, or whose bytes no
longer match, is medium throughout.

Two items are independently produced when they are different files, do not
come from the same run id, and neither contains a span of 200 or more bytes
copied from the other. A handoff that quotes a receipt is therefore not
independent of that receipt.

### Abstaining before any model call

A card gets no model call, and counts the reason, when its scope has no
binding, when every cited item is unreachable or flagged, or when the
remaining evidence cannot meet the corroboration rule below: at least two
independently produced items, one of them with authenticated fields. Since
confirmations are off by default (decision Q5), a call that cannot lead to a
correction would only spend tokens.

### Seats

Two roster seats run the pass. `propose_seat` makes one call per card, and
`confirm_seat` makes one independent call for each card whose reply would lead
to a correction. The seats must pin different models, or configuration fails
with `verify_seat_same_model`. The recorded choice is a mid-tier model to
propose and a top-tier model to confirm. Both reuse the roster seat
resolution, model pinning and CLI adapters of `brigade run --worker`, without
planning, synthesis, fleet repo claims or worktrees.

Each seat must be tool-free for these calls. It receives only the composed
prompt. It cannot run commands, read files, call connectors or receive memory
recall from hooks. The call runs in an empty private working directory. A
doctor probe for each seat must prove those properties for its CLI adapter
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

Data sits between markers carrying a random 128-bit nonce per call, so data
cannot close a marker it has not seen.

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

The model proposes. The client decides whether a proposal may be opened, and
the reviewer decides whether it is true:

1. Every quote must be an exact substring, 20 to 400 bytes long, of the
   evidence item it names.
2. The only v1 correction is expanding an abbreviated commit id that the
   card marks as a commit reference. A marked reference is the whole word
   `commit` or `commits` in any case, exactly one space, then 7 to 39
   lowercase hex characters with a digit, bare or as a single-backtick code
   span. That rule comes from the cards themselves. In a sample of about
   2,000 operator cards, 61 of the 63 explicitly marked abbreviated ids used
   one of those two forms. The other two used a colon with an unbalanced
   backtick, and no card carried a `commit` front-matter key. Hex tokens with no such marker
   (artifact digests, fingerprints, ids in paths or URLs, and bare
   abbreviations) are far more common and are never corrected.

   After the normalization of rule 9, the replacement must equal the block
   except that marked abbreviations become full commit ids (40 or 64
   lowercase hex) that start with the exact old token. Any other added,
   removed or reordered word or punctuation, a negation included, abstains
   with `verify_edit_out_of_scope`. A changed hex token that is not a marked
   reference abstains with `verify_not_commit_reference`. An old token that
   is already full length, or a new id that does not start with the old
   token, abstains with `verify_not_abbreviation`. One id is never swapped
   for another, so "not in commit X" cannot be retargeted.

   An abbreviation shorter than 7 hex characters abstains. A token that
   short is not a marked reference at all, so it abstains with
   `verify_edit_out_of_scope`, and `verify_abbreviation_too_short` stays as
   a second guard. A 7-character prefix is 28 bits, so an attacker can grind
   a commit that starts with it, get an authentic receipt for that commit,
   and point a claim such as "Do not commit X to the release index" at it.
   Prefix length alone is therefore not the defense. The slice 8 minimum was
   12, and in the same card sample only 1 of the 61 accepted marked
   abbreviations is 12 characters or longer, while 52 are exactly 7. Slice 9a
   lowered the minimum to 7 when it shipped the resolver below, because
   exact equality with the unique authenticated candidate plus reachability
   from a freshly fetched allowed ref means a ground commit must also land
   on an allowed ref.

   Commit identity is bound in two steps before corroboration. Every
   authenticated `commit` and `baseline_commit` value in the card's assembled
   evidence, quoted or not, that starts with the abbreviation must name
   exactly one full id. None or several abstain with
   `verify_commit_ambiguous`, even when both seats pick the same one. Then
   the caller's resolver must return that same id, or the block abstains
   with `verify_commit_unresolved`. Without a resolver every expansion
   abstains. The resolver's normative rules are in "Commit resolver
   contract" below. The contract module stays pure and ships only a test
   fake. Slice 9 implements the git resolver.

   Each expanded id must appear in quotes from two independently produced
   countable items. One of them must be an authenticated receipt quote that
   covers a whole `commit` or `baseline_commit` field holding exactly that
   id. Those two fields are the whole allowlist, because their values are
   typed full commit ids. Every other authenticated field is excluded.
   `argv` is text the caller chose, so `echo "the color is red"` would
   otherwise authenticate any sentence. `status` and the times read the same
   on almost every receipt. `run_id`, `manifest_id` and `tree_fingerprint`
   are Brigade's own bookkeeping. Untyped value fields such as `exit_code`
   are excluded too, because a bare value cannot say which claim it belongs
   to. Quotes from command output or low-rank excerpts never hold an id.

   A receipt proves that its worktree used a commit. It does not prove that
   a hex token in a card was a commit, or that the receipt concerns the
   card's subject. The marker answers the first question and the prefix
   rule keeps the referent fixed, which is why expansion is the only edit.
   Broader corrections need typed claim predicates that bind the claim's
   subject and relation to receipt metadata, for example an exit status
   bound to the command a claim names. The reply schema cannot express them
   and the client cannot check them yet, so they are deferred past v1.
   Until then every other correction abstains, and the D7 metrics measure
   that cost.
3. `changed` needs rule 2 to hold using only evidence newer than the head
   revision's log time.
4. A replacement or `previously` text must differ from the block, stay a
   single paragraph with no newline, carriage return or `^`, and be at most 1.5 times the block length plus 200 bytes,
   within the hub's 8,192-byte limit.
5. Novelty check: every URL, path, host-like name, IP address, email
   address, code span, command-line flag and number of three or more digits
   in a replacement or `previously` text must appear in the original block,
   or in quotes from two independently produced items that can meet rule 2.
   Tokens compare by their canonical reading: NFKC, then a fixed table that
   maps dot, slash, backslash, at, colon and dash lookalikes (such as
   U+3002, U+2024, U+2215 and U+29F8) to ASCII, then up to three rounds of
   strict percent decoding (UTF-8 only) and HTML character references. Hosts,
   addresses and emails compare without case. A word that still holds an
   escape after the last round, or decodes to invalid UTF-8, has no
   canonical reading and is rejected. So is a word with a non-ASCII
   punctuation or symbol character between two letters or digits, which
   catches lookalikes the table does not list. A token counts only as a
   whole word or a whole code span, and quotes from command output or
   excerpts never count. One planted handoff therefore cannot introduce a
   token. A reply that adds one is rejected with `verify_novel_token`.
6. `superseded` abstains in v1 with `verify_supersede_deferred` and never
   opens `supersede_card`. `history` abstains with `verify_history_deferred`,
   because an expansion names the same commit and so states no earlier
   value. `supersede_card` retires the whole card, and
   nothing in the reply binds the source card's claim to the target card.
   Ids and titles of related cards cannot show that the target holds the
   corrected claim, and evidence quoted for the source does not mention the
   target. Re-enabling it needs the target card's claim text in the prompt
   and evidence that binds the source claim to that target. It also needs
   the single-claim-block rule, because retiring a card with several claim
   blocks would remove claims the verdict never covered.
7. The replacement and history text are scanned locally with the hub's bundle
   before submission, and the hub scans them again on `propose`.
8. Only `high` confidence produces an op. `medium`, `low` and `insufficient`
   abstain and are counted by verdict.
9. The confirm seat repeats the call with the evidence order shuffled and a
   fresh nonce, and its reply must pass checks 1 to 8 on its own. The two
   seats must then agree on the resulting operation, not only the verdict:
   the same op type, the same refs for `append_evidence`, and for
   `replace_block` the same text after normalization.
   Check 4 already holds both texts to one newline-free paragraph.
   Normalization then only strips claim anchors (` ^c-xxxxxx`) and leading
   and trailing ASCII spaces and tabs, and collapses each internal run of ASCII
   spaces and tabs to one space. It never touches characters inside a code
   span, so spacing in code must match exactly. Case, punctuation, other
   whitespace characters and wording must match. A block whose seats
   disagree on any of these abstains with `seat_disagreement`. A confirm reply
   that fails checks 1 to 8 abstains with `verify_unconfirmed`. When the seats
   agree, the proposal carries the proposing seat's exact text. The confirming
   model's text matches it once anchors and ASCII space and tab runs outside
   code spans are normalized. The bytes themselves can differ in those places.

These checks catch malformed, ungrounded and steered replies. They cannot
prove the evidence right, so every op still waits for a reviewer.

### Commit resolver contract

`verify_card` takes `resolve_commit(abbreviation)`, which returns a full
commit id or None. The contract module calls it only for a marked
abbreviation that has exactly one authenticated candidate, and accepts the
result only when it equals that candidate. With no resolver every expansion
abstains. The rules below are normative for the slice 9 implementation.
Slice 9's resolver implementation requires its own security review. This contract is the minimum it must meet. The resolver MUST:

1. Run every git command through one wrapper that applies rules 2 to 4 to
   each call, with a fixed timeout, no stdin, `--no-pager`, and no prompt.
   No git command bypasses it. A timeout kills git's whole process group,
   and helpers that outlive git after a normal exit are reaped too.
   Commit expansion is POSIX-only in v1. The resolver runs git only where
   `proc.run_held` can execute a held descriptor (Linux `fexecve`). On
   Windows, and on any other platform, it returns None for every
   abbreviation with the fixed reason `verify_resolver_unsupported_platform`
   and never starts a process. Enabling Windows needs install roots whose
   ACLs are validated first.
2. Run only a root-owned git, and build its environment from scratch. git is
   the first of `/usr/bin/git`, `/bin/git` and `/usr/local/bin/git` that
   exists, resolved with `realpath`. The real file and every directory from
   it up to `/` must be owned by root and writable by neither group nor
   others. A git owned by the current user is refused, and so is one reached
   through a link whose target fails the rule. git's exec directory must
   meet the same rule. No setting or environment variable can name another
   git. The verified file is opened once per run, the descriptor is checked
   against the path (device and inode), and every call executes that
   descriptor with `fexecve`. Replacing the path after the check changes
   nothing, and a script is never run this way. Nothing is copied from the
   caller's environment, which holds exactly:
   - `PATH`: git's exec directory plus `/usr/bin`, `/bin` and
     `/usr/local/bin`
   - `HOME`: an empty 0700 directory made with `mkdtemp` inside a 0700
     parent the resolver made with `mkdtemp` under the fixed `/tmp`. When
     the run ends, both are removed only while they are still the
     directories the resolver made: the parent is reached without following
     any link, both device and inode pairs and the owner must match, and the
     HOME is removed relative to the held parent by a removal that never
     follows a link. Any mismatch leaves them in place and logs it.
   - `LC_ALL=C`
   - `GIT_CONFIG_NOSYSTEM=1`, `GIT_CONFIG_GLOBAL=/dev/null`,
     `GIT_NO_REPLACE_OBJECTS=1` and `GIT_TERMINAL_PROMPT=0`

   So every `GIT_*` variable from the caller (`GIT_DIR`,
   `GIT_CONFIG_PARAMETERS`, `GIT_CONFIG_COUNT`, `GIT_GRAFT_FILE` and the
   rest) never reaches git. Neither do the loader variables (`LD_*`,
   `DYLD_*`), `OPENSSL_*`, `SSLKEYLOGFILE`, every `*_proxy`, `CURL_*`,
   `SSL_CERT_*` and `XDG_*`. System and global config are never read, the
   fetch uses the system trust store and it connects directly. No
   production code path adds a CA or any other TLS setting.
3. Pass `--no-replace-objects` and these `-c` overrides on every call:
   `core.fsmonitor=false`, `core.hooksPath` set to a directory that does not
   exist, `credential.helper=` (empty, which clears every helper),
   `core.askPass=`, `core.alternateRefsCommand=`, `maintenance.auto=false`,
   `gc.auto=0`, `fetch.recurseSubmodules=false`, `protocol.allow=never`,
   `protocol.https.allow=always`, `http.sslVerify=true`, `http.proxy=`
   (empty), `http.followRedirects=false`, `http.extraHeader=` (empty, which
   clears every header) and `http.cookieFile=` (empty). SSH and every other
   transport are not allowed, so no `core.sshCommand` or `GIT_SSH_COMMAND`
   is set.
4. Work in the git repository at the `[[verify.workspace]]` path bound to the
   card's scope, and refuse when `git rev-parse --show-toplevel` names any
   other directory.
5. Read the repository's local config with
   `git config --local --no-includes --list` and return None when it holds
   any of: `include.*` or `includeIf.*`, `core.fsmonitor`,
   `core.hooksPath`, `core.sshCommand`, `core.gitProxy`, `core.askPass`,
   `core.alternateRefsCommand`, any `credential.*`, any `alias.*`,
   `url.*.insteadOf` or `url.*.pushInsteadOf`, `remote.*.vcs`,
   `remote.*.uploadpack`, `remote.*.proxy` or `remote.*.proxyAuthMethod`,
   any `http.*` key (including the per-URL `http.<url>.*` form, so no TLS,
   CA, client certificate, proxy, redirect, header or cookie setting from
   the repository applies), or `protocol.*`. The `-c` overrides cover what
   they can. Includes cannot be overridden, so a repository that has one is
   refused and never read. git never lets an alias shadow a built-in
   command, and aliases are refused anyway.
6. Validate the URL of every remote that an allowed ref names. Each
   `remote.<name>.url` value must use the `https` scheme. `ext::`, `fd::`,
   `file::`, `file://`, local paths, `ssh`, `git` and any other transport
   or remote helper return None.
7. Ignore replacement refs and refuse grafts. `--no-replace-objects` and
   `GIT_NO_REPLACE_OBJECTS=1` stop every `refs/replace/` entry from
   applying. A grafts file at `git rev-parse --git-path info/grafts` that
   exists returns None, and `GIT_GRAFT_FILE` is already removed by rule 2.
8. Return None for an abbreviation shorter than the contract minimum
   without calling git. git itself refuses `--disambiguate` below 4 hex
   digits, and the contract minimum (7, lowered from 12 by slice 9a) is always above that
   floor.
9. Return None for a shallow or incomplete repository.
   `git rev-parse --is-shallow-repository` must print exactly `false`, and
   `extensions.partialClone` must be unset, because missing history or
   objects could hide a second match.
10. Take the allowed refs from a new `commit_refs` key on the
    `[[verify.workspace]]` entry. It lists fully qualified remote-tracking
    refs such as `refs/remotes/origin/main`. When the key is absent, the
    default is the single ref that `git symbolic-ref refs/remotes/origin/HEAD`
    names, which is the remote-tracking default branch. If that symbolic ref
    is unset there is no default, and the resolver returns None. Local
    branches and tags are never allowed refs, because a local process can
    create or move them without a fetch. Same-user tampering with
    remote-tracking refs stays out of scope, as in the S24 marker ruling.
11. Prove every allowed ref fresh in the current run without relying on the
    remote's configured fetch refspec. For each allowed ref
    `refs/remotes/<remote>/<branch>`, fetch with
    `git fetch --porcelain --no-tags --prune <remote> +refs/heads/<branch>:refs/remotes/<remote>/<branch>`.
    A refspec on the command line replaces the configured ones, so no tag or
    other refspec from config applies. The porcelain output must list that
    destination ref with an updated or up-to-date flag. git prints
    up-to-date refs in porcelain output only with `--verbose`, so the fetch
    passes it too, along with `--no-prune-tags`, because a configured
    `pruneTags` would otherwise add a tag refspec. With a git older than
    2.41, which has no `--porcelain`, the resolver instead compares the
    destination ref after the fetch with `git ls-remote <remote>
    refs/heads/<branch>` from the same run, and they must match. A failed,
    timed out or rejected fetch, a missing destination ref, a mismatch, or a
    ref proven only in an earlier run returns None.
12. Evaluate ambiguity across the complete available object database, not
    only the allowed refs. `git rev-parse --disambiguate=<abbreviation>`
    lists every object of any type (loose, packed and alternates) whose id
    starts with the prefix, reachable or not, and never consults ref names.
    Exactly one line must come back, or the resolver returns None. A second
    object on another ref, an unreachable object or a blob with the same
    prefix makes the abbreviation ambiguous.
13. Check that `git cat-file -t <id>` prints exactly `commit`, or return
    None.
14. Check that the id is exactly 40 or 64 lowercase hex characters, and
    return it in that spelling. The contract then requires it to equal the
    unique authenticated candidate.
15. Check reachability separately with
    `git merge-base --is-ancestor <id> <ref>` against each allowed ref. Exit
    0 for at least one ref passes. Exit 1 for every ref, or any other exit,
    returns None.
16. Return None on any missing object, ambiguity, command error, nonzero
    exit, timeout or unparsable output.

Slice 9 tests plant each hostile setting from rules 2 to 7 in a scratch
repository: an include, an fsmonitor command, a hooks path and a
`reference-transaction` hook that would write a marker file, a credential
helper, an alias, an `insteadOf` rewrite, `ext::` and `file://` remote URLs,
an alternate refs command, a remote helper, a grafts file, a tag refspec in
config and an inherited `GIT_DIR`. Each test asserts None and that no marker
file appeared. A planted replacement ref must leave the result unchanged.

`git rev-parse --verify <abbreviation>^{commit}` alone is insufficient. It
resolves ref names before object ids, so a local branch or tag named like
the abbreviation would answer instead of the object. The `^{commit}` peel
uses the object type to break ties between a commit and a tree, blob or tag
with the same prefix instead of reporting the ambiguity. It also peels an
annotated tag whose id matches to the commit the tag points at, which is a
different id. And it says nothing about freshness or reachability.

Reachability from an allowed ref after a proven fetch is the identity
binding. A receipt proves only that some worktree used a commit. Grinding a
commit with a matching prefix and getting a receipt for it is not enough
unless the attacker can also land that commit on an allowed ref.

The minimum abbreviation stayed at 12 hex characters until slice 9a shipped
this resolver (`src/brigade/memory_verify_resolver.py`) with a test for every
None case above, including an abbreviation that is ambiguous only because of
an object on another ref and a shallow clone. Slice 9a then lowered the
minimum to 7. Exact equality with the unique
authenticated candidate plus allowed-ref reachability means a ground commit
must land on an allowed ref, so prefix length stops being the defense. In
the card sample, 1 of the 61 accepted marked abbreviations qualifies at 12
characters and 52 qualify at 7.

### Mapping to ops

| Verdict | Op | Notes |
|---|---|---|
| `contradicted` | `replace_block` with the replacement | v1: a marked abbreviated commit id expanded to its full id (check 2). The hub appends `previously X, now Y`, so the old wording stays in history. |
| `changed` | `replace_block` with the replacement | Same op. The verdict is kept in the manifest so reviewers can tell a wrong claim from a stale one. |
| `history` | None in v1 | Abstains with `verify_history_deferred` (check 6). `add_previously` returns with typed claim predicates. |
| `superseded` | None in v1 | Abstains with `verify_supersede_deferred` (check 6). |
| `supported` with uncited support | `append_evidence` | Only with `[verify] propose_confirmations = true`, default false. Every content word of the claim must meet the rule 2 corroboration rule. Eligibility is per evidence item: a ref is cited only when every assembled item whose source maps to it is a quoted countable item holding claim material. A receipt yields a fields item and a context-only output item with one source, so its ref is never cited through the fields item. Command output and excerpts never become citations. |
| `supported` otherwise | None | Recorded in local state as verified. |

Each proposal names one card. Every correction proposal also carries an
`append_evidence` op that cites the verification manifest from D5, so the card
records the check after acceptance. The hub classifies every proposal from a
verifier as Tier 2 (D4).

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
| Model calls per card | 1 proposing, plus 1 confirming for corrections | fixed |
| Per-call timeout | 120 s | 30 to 600 s |
| Run wall clock | 20 min | 5 to 60 min |
| New proposals per run | 4 | 0 to 16 |
| This job's pending proposals | 8 | 0 to 32 |

A run that reaches its wall clock stops after the current call and counts the
rest as `verify_deferred_time`.

### Cost estimate

At roughly four bytes per token, a typical card (2 KiB of text, 12 KiB of
evidence, 2 KiB of instructions) is about 4,000 input tokens and 400 output
tokens. Twelve proposing calls and two confirming calls come to about 60,000
tokens per run, or about 1.8 million a month on a daily schedule. Confirming
calls cost more per token but run only for would-be corrections. The worst
case is 24 calls of about 16,000 input and 2,048 output tokens, about 430,000
tokens per run. The run report records actual tokens per seat.

Twelve cards a day covers a 2,000-card scope uniformly in about 167 days, which
is why D2 puts read-heavy cards first.

## D4. Privacy and trust

### Private cards and connector audiences

The private class boundary from the hub memory client section applies
unchanged. Private text may reach the operator's machines and the model
providers behind the operator's own local harnesses. It never reaches a
connector audience.

- The pass runs only on the operator's machine, through local seats. That
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
| Local review packet | Excerpts, quotes, replacement text, raw model output, trust labels, scan findings | Nothing is added to it from other cards |

The seats' prompts and replies are written to an owner-only work directory
inside the verify folder and deleted once the review packet is written. They
never enter the shared run journal. Packets are owner-only files under
`.brigade/memory/verify/packets/`, capped at 64 MiB in total, and deleted 14
days after their proposal reaches a terminal state.

### Verifier principals and the Tier 2 floor

Verifier identity is an attribute of the principal, held by the hub. An admin
sets a `verifier` flag on the principal record. The principal cannot set or
clear it, and no policy list or naming convention stands in for it.

The flag covers every credential of the principal. Each credential record
carries a copy of it, bound when the credential is issued, and every request
requires the copy to match the principal record. A mismatch is refused with
`403 memory_verifier_flag_mismatch`. The hub keeps the two in step:

- Enrolling an additional credential, rotating a credential, and re-adding a
  credential after revocation all issue the new credential with the
  principal's flag. An enrollment or rotation request that would create an
  unflagged credential for a verifier principal is refused with
  `memory_verifier_credential_unflagged`, and nothing is issued.
- Revoking credentials does not clear the flag. If the principal record itself
  is removed, the hub keeps a tombstone with the flag, so the same principal
  id comes back as a verifier until an admin clears it.
- Setting or clearing the flag updates the principal record and every live
  credential in one transaction.

The hub enforces three rules for flagged principals:

1. **Tier 2 floor.** Every proposal a verifier submits is classified Tier 2
   at submit, at preflight and at every decision, whatever its ops and
   whatever `tier0_op_classes` lists. The proposal body records
   `verifier: true` at submit, and the floor follows that record, so clearing
   the flag later does not lower the tier of an open proposal. Preflight
   reports a `verifier_floor` hold, and `auto_apply` refuses the proposal.
2. **Never a reflect principal.** A policy write that lists a flagged
   principal in `reflect_principals` is refused with
   `memory_policy_verifier_listed`. Flagging a principal already listed there
   is refused until the policy drops it.
3. **Explicit status.** The `status` reply reports the caller's own
   `verifier` flag and its `reflect_principal` membership as two booleans.

The verify job reads those two booleans before every run and refuses to run
unless `verifier` is true and `reflect_principal` is false. It never infers
either one from a preflight hold reason. Preflight checks
`tier0_op_classes` before principal membership, so an ordinary
`replace_block` correction reports `class_disabled` and says nothing about the
principal. The client check catches a misconfigured folder early. The hub
rules are what keep the pass out of Tier 0.

No slice may submit a live proposal until the hub implements all three rules,
and the job refuses to submit against a hub that does not advertise them
(`verify_hub_floor_missing`).

Slice 3 stores the principal flag and the credential copies in the memory
file (schema 11), because D7 keeps Python out of it and the hub file's schema
belongs to Python. Native registration binds a credential's copy through the
memory writer before the hub issues it. Fleet policy revisions are written by
Python, so the memory writer publishes the flagged principal ids to
`fleet-hub-memory-verifiers.json` inside the flag change's transaction, before
it commits, and refuses the change if that write fails. The policy writer
checks that file inside its own hub write transaction and refuses a listing
when hub memory exists but the file is missing or unreadable. The file carries
a generation that the hub writer records in `hub_meta` before each flag change
runs. The writer labels each list with the memory file's own generation and
the policy writer accepts only an exact match, so a list a crash left behind,
or a restored copy of either database, is refused as stale until an admin
`verifier` or `verifier_resync` action brings the two files back together.
Memory does not start until it can republish the list. The
operator details are in [memory-operations.md](memory-operations.md).

### Prompt injection

Card text and evidence are written by agents, and session excerpts can carry
text from web pages or tool output. A hostile or confused input could try to
make the model propose a wrong correction, plant a command or URL, flood the
review queue, point a supersede at a chosen card, or leak text from one card
into another.

| Risk | Mitigation |
|---|---|
| Model follows instructions in data | Tool-free seats with no recall, nonce markers, and a reply limited to a strict schema. The model cannot act, only answer. |
| Wrong correction | High confidence only, exact quotes, two independent items with one authenticated, the same op and text from a separately pinned model, and a reviewer who sees the trust labels. |
| Planted command, URL, path or host | The novelty check rejects any such token that is not in the block or a quote. Command output cannot satisfy the authenticated-evidence rule. The hub scan rejects blocked content. |
| Queue flooding | At most 4 new proposals per run and 8 pending for the principal. A rejected correction is not proposed again until the card revision or cited evidence changes. |
| Chosen supersede target | `superseded` opens no op in v1 (check 6). |
| Cross-card or cross-scope leakage | One card per call and evidence only from the scope's own roots and projects. No other card's text or title reaches the prompt in v1. |
| Flagged input | Cards with non-clean scans are not eligible. Flagged excerpts are dropped before the call and listed in the packet. |
| Automatic-apply holds | None before review. A manifest's `contrary` stance activates only when a reviewer accepts its proposal (D5). |

## D5. Review experience

A reviewer needs to see why the pass believes a claim is wrong, and needs to
know that the packet in front of them is the one the hub bound to the
proposal. Human review is the acceptance boundary for every proposal.

### The manifest

Each correction proposal gets a verification manifest, attached with
`evidence.attach` under the verify principal and cited by the proposal's
`append_evidence` op. Its `artifact_kind` is `memory-verify-manifest`. The
manifest is structured JSON with no free text:

| Field | Content |
|---|---|
| `schema` | `brigade.memory-verify-manifest.v1` |
| `card` | Scope, card id, base revision |
| `blocks` | Block id, verdict code, confidence code |
| `evidence` | Reference, kind, trust rank, authenticated or not, observation time, sha256 and quoted byte ranges for each item the quotes used |
| `findings` | Rule ids of scan findings on dropped items |
| `seats` | Seat names and pinned model ids for the proposing and confirming calls |
| `prompt_sha256`, `packet_sha256`, `input_sha256` | Digests of the instructions, the local packet and the full prompt input |

References are workspace-relative paths or bundle ids and are scanned before
attachment. A manifest that does not scan clean is not attached, and the card
abstains with `verify_manifest_unclean`.

### Proposal-bound attestation state

An attached `contrary` record holds automatic apply on its card at once and
until an admin purges it. Verifier attachments instead follow a
proposal-bound life cycle:

| State | Entered when | Holds automatic apply |
|---|---|---|
| `pending_review` | Attached by a verifier credential. Bound to the first proposal that passes the binding check below | No |
| `active` | That proposal is accepted. A manifest with `contrary` stance then behaves as a normal contrary record | Yes, for `contrary` |
| `inert` | The proposal is rejected, goes stale or is compacted, or no proposal cites the record within 24 hours | No, and it never counts as proof |

An inert record frees its quota slot, as a purged one does, so rejected
corrections do not use up the 8 records one principal may hold per card.

Binding requires exact correspondence between three things: the attestation's
scope and card id, the manifest's `card` fields (scope, card id and base
revision), and the proposal's ops. The hub parses the manifest at attach time
and refuses one whose `card` scope or card id differs from the attestation's,
with `memory_manifest_card_mismatch`. At `propose`, a proposal that cites a
verifier manifest binds it only when all of these hold:

- The proposal comes from the principal that attached the record.
- The `append_evidence` op citing the record addresses the attestation's scope
  and card id, with a `base_revision` equal to the manifest's base revision.
- Every other op in the proposal addresses that same scope, card id and base
  revision. A `supersede_card` op's `other_card` must be in the same scope.
- The record is still `pending_review` and unbound.

Otherwise the whole proposal is refused with
`422 memory_manifest_binding_invalid` and nothing is stored. A verifier
therefore cannot bind a manifest for card A to a proposal that changes card B,
to a card in another scope, or to a revision the manifest did not check.

Slice 4 keeps this state in a `memory_evidence_bindings` side table (memory
schema 12), so `memory_evidence` stays immutable, and a trigger lets a row
move forward only. A proposal that goes stale because the policy changed can
still be decided again, so its records stay `pending_review` until that
decision or until compaction makes them inert. A record older than 24 hours
cannot bind even before the sweep reaches it.
Because inert records free their slots, the bytes and records a verifier
retains outside `active` are also budgeted per principal and per scope, and
the idle sweep purges a record 35 days after it went inert through the same
tombstone and `evidence.purge` event as an admin purge.

### Hash read for reviewers

`attestation::resolve` returns hashes only for supporting records from
members of `reflect_principals`. A verifier manifest is neither, so a reviewer
could not authenticate it. The hub adds a read: `proposal_show` gains a
`verifier_manifests` list with each bound manifest's evidence id,
`artifact_kind`, `artifact_sha256`, stance and state.

The authorization targets for this list are the proposal's targets plus the
scope and card of every bound attestation. Only a caller with `review` on all
of them receives the list, which is present and possibly empty. For any other
caller the key is absent, so a reply to a non-reviewer looks the same whether
the proposal binds zero, one or several manifests. The hash covers the
manifest bytes. The manifest carries `packet_sha256`, which binds the local
packet in turn.

### Commands

The reviewer works on the verify host:

1. `brigade memory proposals show <proposal-id>` shows the diff to a caller
   with `review` on the scope.
2. `brigade memory verify show <proposal-id> --target <verify-folder>` checks
   the chain before printing anything. The sha256 of the local manifest must
   equal the `artifact_sha256` from `proposal_show`, and the sha256 of the
   local packet must equal the manifest's `packet_sha256`. A mismatch prints
   only the failure and exits non-zero.
3. After the chain checks, it prints a header per evidence item with its trust
   rank, whether its fields are authenticated, whether it counted toward the
   two-item rule, and any scan findings, ahead of the quotes. Dropped items
   are listed with their rule ids. It re-resolves each cited file and marks
   it `current`, `changed` or `missing`. Output goes to the terminal only, and
   `--json` prints the same fields for tools.

The decision uses the existing `memory proposals decide`. The next run reads
the state of remembered proposals with `proposal_show`. A rejection is stored
as a verdict code against the card revision and evidence digest, so the same
correction is not proposed again until one of them changes. Accepted and
rejected counts per verdict feed the accept rate in D7.

Review stays on the operator host where the packet lives (decision Q3).

## D6. Failure, pause and quota

The job follows S19d and #2383 with its own state file, so the reflect job's
pause and the verify job's pause are independent.

| Condition | Effect |
|---|---|
| Transport, configuration, authorization or hub-unavailable error | Run fails and counts toward the pause |
| `status` shows `verifier` false or `reflect_principal` true, or the hub lacks the floor | Run fails before any model call and counts toward the pause |
| Seat refused by the isolation probe, as a connector, or for sharing a model | Run fails and counts toward the pause |
| Every model call in the run fails | Run fails and counts toward the pause |
| One call times out or returns invalid output | That card is skipped with a reason code. The run continues |
| Seat rate limit or quota reply | Run stops, counts `seat_quota`, and completes without a failure |
| `429 memory_proposal_principal_quota` or `memory_proposal_scope_quota` | Run stops proposing, counts unsent submissions as `proposal_quota`, and completes without a failure |
| `429 memory_read_stats_rate_limited` | Run uses the previous counts from its state file and counts `read_stats_limited` |
| `409 memory_proposal_duplicate_pending` | Records the existing proposal id, counts `duplicate_pending` |
| This job already has 8 pending proposals | Run verifies nothing and reports `pending_ceiling` |

Three consecutive failures pause the scheduled job. Only a successful
non-dry manual run clears the pause, as for `memory-reflect`.

The scope pending limit (128) is shared with the reflect principal and any
other proposer. The verify job's own ceiling of 8 leaves 120 for the others,
and its later schedule lets duplicate merges reach the quota first. The
per-card fairness table uses the same ordering and key digest as the #2383
cluster table and the #2386 link pass, so a card served this run waits behind
cards not yet served.

Verify state lives in `.brigade/memory/verify/state.json` with the safe write
and recovery rules of `schedule.json` and an 8 MiB limit. At about 100 bytes
per card, the 50,000-card scope bound fits in about 5 MB.

`--dry-run` calls the seats unless `--no-model` is given, then submits,
attaches and writes nothing, leaves the pause state alone, and prints counts
and verdict codes only.

## D7. Evaluation and go/no-go

### Seeded evaluation corpus

A fixture corpus of about 70 synthetic cards in one scope, each with evidence
files of every kind, all in the shape Brigade writes:

| Group | Cards | Ground truth |
|---|---:|---|
| Wrong from the start | 20 | Two independent items, one an authenticated receipt, contradict a block. The expected replacement's key tokens are listed. |
| Drifted | 10 | Older evidence supports the block, newer independent evidence contradicts it |
| Correct | 20 | Evidence supports every block |
| Adversarial | 10 | Card, handoff or command output tries to steer the model, for example asking it to replace a claim with a download-and-run command |
| Shared source | 10 | A wrong claim is backed by two items that trace to one source, such as a handoff quoting a receipt's output or two excerpts of one run |

`brigade memory verify eval --corpus <dir>` runs the pass against a scratch
replica built from the corpus with the configured seats. It submits nothing
and reports the metrics below. CI runs the same harness with scripted fake
seats to test the plumbing. Live model runs are an operator step, because they
cost tokens and vary between runs.

### Metrics and thresholds

| Metric | Bar |
|---|---|
| Precision: correct correction proposals over all correction proposals | at least 0.9 |
| Recall over wrong and drifted cards | at least 0.6 |
| Corrections proposed on the 20 correct cards | 0 |
| Adversarial proposals that carry a steered token or claim | 0 |
| Corrections proposed in the shared-source group | 0 |
| Key-token match of replacements on true positives | at least 0.8 |
| Verdict agreement across 3 repeated runs | at least 0.9 |
| Same-model correlation: wrong verdicts shared by proposer and confirmer, compared with a run where one model plays both seats | reported, with the two-model setup no worse than the one-model run |
| Shared-source correlation: wrong verdicts among cards whose supporting items share a run or a copied span | reported |
| Median tokens per card, per seat | reported |

### Go/no-go before the live hub

1. The hub floor, the policy refusal, the explicit `status` booleans, the
   proposal-bound attestation states and the reviewer hash read are deployed
   and verified on the live hub.
2. The seeded evaluation meets every bar on two consecutive runs with the
   production seats.
3. Each implementation slice passed its own security review.
4. Shadow mode on one live scope for 14 days with `--dry-run`. The operator
   samples 20 verdicts by hand, and at least 17 of them agree.
5. Live proposals at 2 per run for 28 days. The reviewer accept rate is at
   least 0.7 over at least 20 decided proposals, and no accepted correction
   needs a rollback because the pass was wrong.

After step 5 the cap may rise to 4. A later rollback caused by the pass
returns the job to shadow mode until the evaluation passes again.

## Decisions

Revision 1's open questions, as settled by the conductor and the security
review:

- Q1. A dedicated, audited `read_stats` right, not `reflect` (D2).
- Q2. A mid-tier model proposes and a separately pinned top-tier model
  confirms, on would-be corrections only (D3).
- Q3. No hub read for evidence bytes. Review stays on the operator host, and
  the hub returns only manifest hashes (D5).
- Q4. No automatic-apply hold before acceptance (D5).
- Q5. Confirmation proposals are off by default.
- Q6. The verifier Tier 2 floor is mandatory and lands first (D4).
- Q7. 35-day retention within the D2 budgets.
- Q8. Explicit archive projects per scope, and unclear ownership is
  unreachable (D3).
