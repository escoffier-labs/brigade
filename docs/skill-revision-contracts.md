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

## `brigade init` never overwrites a wired SKILL.md

`brigade init`, `brigade reconfigure` and `--force` write a bundled `SKILL.md`
(the wired `brigade-work` and `ultra-work-scout` skills in each harness skills
directory, and the workspace-depth `skills/*/SKILL.md` files) only when the
destination is missing. `--force`, including the force that reconfigure passes
implicitly, still replaces other template files but never a `SKILL.md`. For an
existing destination:

| Destination | Result |
| --- | --- |
| Missing | Written, and recorded in `.brigade/skills/wired.json`. |
| Symlink, or a symlinked parent directory | Kept. Init prints `kept (symlinked): <path>` and writes and proposes nothing. |
| Byte-identical regular file | Kept. No write and no proposal. |
| Differing regular file | Kept untouched. Init files one inbox proposal with the bundled text and prints `kept (edited)` or `kept (pristine-stale)` with the proposal id. |

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

`.brigade/skills/wired.json` records, per file init wrote, the harness, skill
id, `sha256` of the installed bytes, `brigade_version` and `template_sha256`.
Init uses it only to label a differing copy: a copy whose bytes still match the
recorded `sha256` is `pristine-stale` (the bundled template moved on), anything
else is `edited`. Nothing refreshes a file in place from it. The file is read
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
