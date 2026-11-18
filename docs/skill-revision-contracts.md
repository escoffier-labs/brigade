# Skill revision contracts and exact fleet approval

`brigade skills inbox add SOURCE --behavioral-contract-json JSON` accepts a
version 1 behavioral contract. Its fields are:

- `reason` and `addressed_failure`: nonempty text, at most 4,000 characters each.
- `change_category`: `behavior`, `bugfix`, `clarification`, `safety`, or
  `compatibility`.
- `baseline_fingerprint`: the current registry package identity, or `null` for
  a skill absent from that registry.
- `expected_observable_behavior`: 1 to 100 objects with unique `id` values and
  nonempty `description` values, each at most 4,000 characters.
- `schema_version`: integer `1`.

For example, a new skill can declare:

```json
{
  "schema_version": 1,
  "reason": "Report failed verification accurately",
  "addressed_failure": "A command timeout was reported as passing",
  "change_category": "behavior",
  "baseline_fingerprint": null,
  "expected_observable_behavior": [
    {"id": "timeout", "description": "Report a timeout as incomplete"}
  ]
}
```

Show and diff expose the contract. Proposals without one display `incomplete`.
When no explicit contract is supplied, a contract embedded in source `skill.json`
is validated and reviewed as the proposal contract, including its baseline.
Brigade does not infer a failure or expected behavior. Acceptance preserves the
contract in registry `skill.json`. Semantic metadata defaults and the contract
are finalized before recording candidate identity, so acceptance preserves the
reviewed package identity. A separate contract digest detects changed or removed
proposal contracts. Acceptance refuses changed candidate or baseline
identities, including helper and semantic metadata changes. `--force` does not
bypass those checks. Legacy proposals without recorded package identity require
a new proposal before acceptance.

Package identity composes the existing collected-file fingerprint with semantic
root metadata and nested metadata file digests. Root metadata formatting and
managed `imported_at`, `updated_at`, `created_at`, `fingerprint`, and `source`
fields do not change identity. The source selector is bound separately.

## Outcome regression proposals

`brigade outcome reconcile --proposals` files a proposal for a Brigade-bundled skill with a verified regression (see [Outcome scoring](outcome-scoring.md#regression-proposals)). It carries a complete contract filled only from observed evidence:

- `reason` names the artifact, the ratchet reason code (`regression_hold` or `regression_rollback`), the decision reason and the helped and hurt counts.
- `addressed_failure` names the failing receipt run ids and failing check ids.
- `change_category` is `bugfix`.
- `baseline_fingerprint` is the registry package identity at filing, or `null` when the skill is absent from the registry.
- `expected_observable_behavior` has one assertion per failing check read back from the hurt receipts (`<run id>:<check id>`, with the recorded command), at most five receipts and five checks each. A receipt that is gone yields an assertion that says `command unavailable`; a readable receipt whose failing command has no check id cites that command instead (`<run id>:command-<n>`). Counts, run ids and assertions all come from the same receipt set, deduplicated by evidence unit exactly as the scorecard counts it; Brigade never invents a command.

The proposal is filed with `dedupe_key` `outcome:<artifact_id>:<reason_code>:<content fingerprint>`, the provenance `brigade://outcome-regression/<skill_id>`, and `requires_revision: true`. Its staged text is the current skill, unchanged, and it records that candidate as `unrevised_identity` and the body fingerprint (which ignores `skill.json`) as `unrevised_fingerprint`. `inbox accept` refuses a `requires_revision` proposal whose files still match `unrevised_fingerprint`, or that lacks the unrevised fields (exit 2, `--force` included). It also refuses any candidate whose files match the `unrevised_fingerprint` of an open `requires_revision` proposal for the same skill, and `inbox add` refuses to file one, so re-filing the same unrevised files under a different contract cannot get past the guard. Accepting would otherwise change nothing while still muting the dedupe key. To act on the request, file a revised candidate with `brigade skills inbox add` and a contract, then reject the review request. `inbox show` prints `requires_revision: yes` for it.

## `brigade init` never overwrites an edited SKILL.md

`brigade init`, `brigade reconfigure` and `--force` write a bundled `SKILL.md`
(the wired `brigade-work` and `ultra-work-scout` skills in each harness skills
directory, and the workspace-depth `skills/*/SKILL.md` files) only when the
destination is missing, or holds a pristine copy of a past release (below).
`--force`, including the force that reconfigure passes
implicitly, still replaces other template files but never a `SKILL.md`. For an
existing destination:

| Destination | Result |
| --- | --- |
| Missing | Written, and recorded in `.brigade/skills/wired.json`. |
| Symlink, or a symlinked parent directory | Kept. Init prints `kept (symlinked): <path>` and writes and proposes nothing. |
| Byte-identical regular file | Kept. No write and no proposal. |
| Regular file equal to a text a past release shipped for that skill (newline-folded) | Refreshed to the current bundled text. Init prints `refreshed (pristine): <path>`, records it in `wired.json` like a fresh write, leaves no `.bak`, and marks a pending inbox proposal it filed for that file `superseded` (reason `refreshed to current bundled text`). |
| Any other differing regular file | Kept untouched. Init files one inbox proposal with the bundled text and prints `kept (edited)` or `kept (pristine-stale)` with the proposal id. |

SKILL.md wiring walks, reads and creates the destination relative to a held
descriptor for the target root (`brigade.dirfd`), one component at a time, so a
skill installed as a link (the shape in #2677), a linked parent directory or a
Windows junction is never reached through it, and a parent swapped mid-run
cannot redirect the write. The read is non-blocking and requires a regular
file, so a FIFO at the destination cannot hang init. A race (a component
vanishing or changing type) resolves to a kept outcome, never to a write. This
guarantee covers SKILL.md wiring only; the other template files keep their own
write path. Line endings do not count as an edit: bundled text is written as LF
and a CRLF copy compares equal to it.

A new `SKILL.md` is published whole or not at all. The text is written and
fsynced to a temporary sibling (`.SKILL.md.<random>.tmp`) under the same held
directory, then moved to its name with `dirfd.replace_children(replace=False)`,
the module's portable no-replace publish (a hard link then unlink on POSIX, a
rename with replacement disabled on Windows; `dirfd.link_child` is POSIX-only,
so it is not used). It never replaces an existing name: a name taken during the
publish is the lost-race kept outcome. A failed or interrupted write (a full
disk, an I/O error, Ctrl-C, a killed process) leaves no `SKILL.md`, and the temp
file is removed, so the next run writes the bundled text instead of treating a
truncated file as `edited`.

An error that is not a race (permission denied, a read-only or full disk while
creating a directory or the temp file, an I/O error while reading) does not
abort init. That skill is a `kept (not-a-regular-file)` outcome with a warning
naming the path and errno, it is not recorded, and the remaining skills, the
`wired.json` write and the rest of init still run. `wired.json` is written in a
`finally`, so files already placed are recorded even when a later step fails.

The proposal is filed with a `dedupe_key` of
`init:<skill_id>:<destination path>:<sha256 of the proposed files>`. `inbox_add`
accepts the key on any caller: when a proposal in any status already carries it,
the existing id is returned, with the same exit code the first filing gave (0
when its lint passed), and nothing new is written, so a repeated init does not
pile up proposals. The destination path is part of the key, so the same text
proposed for two installed copies is two proposals. A proposal's `source` is
`brigade://bundled-skills/<skill_id>@<brigade version>`, not a temporary path.
The proposal carries a behavioral contract only if the bundled `skill.json`
ships one. Brigade does not infer a failure for it.

### Refreshing a pristine copy

`src/brigade/templates/shipped-file-history.json` records the digest of every
text Brigade shipped for the wired skills (`brigade-work`, `ultra-work-scout`,
`note`) and the care runbooks, one entry per distinct text with the first
release tag that carried it. `scripts/shipped_file_history.py --write` builds it
from every `v*` tag plus the working tree, and `--check` (part of
`scripts/verify` and `scripts/verify-focused`) fails when a template changed
without regenerating it, when an entry claims a tag that did not ship it, or when
an entry recorded on the base ref was dropped. An untagged row is valid only when
it is the current working-tree text or the base ref already recorded it; an
untagged row for a text a release shipped must be promoted with `--write`. Tag
provenance is what keeps a forged row from vouching for an edited file, so
without local `v*` tags, or under strict mode without the base ref, the check
fails under `--require-provenance` (used by CI, which fetches the tags and the
base branch, and by `RELEASE.md`) and whenever `GITHUB_ACTIONS` is set; an
unparsable manifest on the base ref is always a failure, and only an explicit
local run without tags or a base ref warns and passes. `RELEASE.md` regenerates it before the version
bump.

At runtime, init refreshes an existing `SKILL.md` only when its newline-folded
digest is in that record for the skill **and** the text it is about to write is a
recorded text too. Anything else, including a missing, linked, oversized or
malformed record, keeps today's behavior exactly. The copy is re-read under a
fresh no-follow descriptor and must still be the same bytes right before the
publish; a copy that changed, vanished or became a link is kept and reported
(`kept (changed-during-refresh)`, `kept (symlinked)`), with no write and no
proposal. The publish is the same fsynced temp-then-rename path a new file uses,
now allowed to replace; the instant between the re-read and the rename is the one
window left. A publish that fails (permission, disk) is a warning, the copy
stays, and its proposal stays pending. Proposals are withdrawn only after a
successful publish. A superseded proposal no longer counts for `dedupe_key`
matching, so if the operator edits the refreshed file the bundled text is
proposed afresh.

`.brigade/skills/wired.json` records, per file init wrote, the harness, skill
id, `sha256` of the installed bytes, `brigade_version` and `template_sha256`.
Init uses it only to label a differing copy: a copy whose bytes still match the
recorded `sha256` is `pristine-stale` (the bundled template moved on), anything
else is `edited`. The label never authorizes a write; a refresh needs a
shipped-history match (below). The file is read
and written under the skills mutation lock, so concurrent inits keep each
other's records, and an unreadable file is copied to
`wired.json.corrupt-<digest>` before it is replaced. A failed record write is a
warning. `brigade skills install` and `brigade skills sync` keep their own
semantics and are unchanged. Review a filed proposal with
`brigade skills inbox diff <id>`. When the installed `brigade-work` copy is kept
and `brigade doctor` reports `stale skill`, doctor adds a pointer to
`brigade skills inbox list` if init filed a proposal for that file. The
classification itself is unchanged.

`brigade reconfigure --prune` still removes a pruned harness directory whole.
Before it does, it prints a warning naming each `SKILL.md` under it that differs
from the bundled text, so an operator edit is not deleted silently.

## Fleet report and review compatibility

Report schema 2 carries a closed, versioned `binding` object per copy. Schema 1
reports remain readable, with no mutation approval. Missing receipt provenance
also leaves binding absent. Paths, package text, prompts, and credentials do not
enter the report. Workspace identity is a SHA-256 digest of the resolved local
workspace path. Node identity comes from the authenticated report owner.

Bindings include skill, harness, verified source selector, installed and target
whole-package identities, a separate source-package identity, and exact rollback
snapshot identity. Target identity covers the rendered package bytes used by
installation. Contract and
evaluation digests may be null. Null means absent or pending evidence, with no
claim about evaluation success or promotion eligibility.

The browser submits a displayed copy-set token. The Hub compares it with the
current server view before saving explicit per-copy approvals. A new node or
harness, changed binding, or legacy unbound draft produces `review-stale` and
withholds executable commands. Saved defaults do not authorize new copies.
Legacy drafts remain inspectable.

Fleet commands include `--expected-binding-json`. Install, uninstall, and
rollback compare the binding with current anchored package observations before
mutation. Install checks the collected source bytes used for rendering and the
installed bytes used for rollback capture. Rollback checks the collected exact
snapshot it restores. Ordinary local invocations retain their existing behavior
when the optional precondition is absent. Filesystem operations retain existing
no-follow protections. Supported skill mutations share a workspace lock,
including ordinary imports, inbox acceptance and projection operations. This
serializes cooperative writers. It does not exclude an unrelated process that
writes installed files without taking the lock.

## Evaluation integration

The proposal's `candidate_identity` and `baseline_identity` identify the
comparison inputs. The contract's assertion IDs name observable checks. Parent
pilot code can use `skills_cmd.registry._package_identity` on the same collected
package bytes and retain the proposal contract alongside its traces.

Registry metadata may carry `evaluation_evidence_sha256` after the evaluation
owner validates the evidence. The report binds that digest when present. It
does not validate evaluator provenance or convert an audit receipt into scoring
evidence. The paired pilot and registered verifier retain those responsibilities.
