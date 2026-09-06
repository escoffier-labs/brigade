# Post-commit linkage statement

A run's evidence is bound to a final Git tree (`tree_fingerprint` in
`run.json`, plus the subjects of Test Result attestations, approvals, and the
agent-change index). A commit is produced later and its identity changes under
amend, rebase, squash, cherry-pick, and merge while its tree may or may not.
The `Brigade-Run` and `Brigade-Receipt` trailers are text inside the commit
message and prove no tree equivalence. The linkage statement is a signed
in-toto statement that binds one commit to one run's attested tree after the
commit exists, together with a verifier that recomputes the tree relationship
locally.

No forge chronology is claimed: the statement records the result of a local
git object comparison, not the order in which commits were created or pushed,
not who authored them, and not whether any review occurred.

## Commands

- `brigade receipts export commit-linkage --target <dir> --run-id <run-id> --commit <sha> [--key <path>] [--principal <name>] [--out <path>] [--force] [--json]`
- `brigade receipts verify-commit-linkage <envelope-or-run-dir> --target <dir> [--commit <sha>] [--json]`

With a run directory, `--commit` selects among `linkage/*.json`; when exactly
one linkage envelope exists, `--commit` is optional. `--commit` accepts a full
SHA only (40 or 64 lowercase hex characters matching the repository's object
format). The internal `git rev-parse` and `git rev-list` calls do not take a
trailing `--` before the SHA because those commands would treat it as a path
separator, and the hex validation of every SHA before use makes this safe.

## Statement envelope

Predicate type `https://brigade.dev/attestation/commit-linkage/v1`,
`schemaVersion: 1`. Subject: exactly one entry
`{"name": "git:commit", "digest": {"gitCommit": <full-sha>}}`. Signed with
`brigade.sshsig-dsse.v1` using the workspace attestation key, and written to
`<run-dir>/linkage/<commit-sha>.json`.

## Predicate fields

- `run`: `{id, journalChainHead}` as in the agent-change index.
- `attestedTree`: `{gitTree}` — the run's terminal `tree_fingerprint` from
  `run.json`.
- `commitTree`: `{gitTree}` — `git rev-parse <sha>^{tree}`.
- `commitParents`: `[{"gitCommit": ...}]` in order, or `{"status": "unknown"}` at a
  shallow-repository boundary where the parents are cut off.
- `commitKind`: `root`, `linear`, `merge`, or `unknown` when the parents are
  unknown.
- `comparison`: rule, exclusion list, normalization base, and normalized tree.
- `equivalence`: `exact`, `normalized`, or `none`.
- `git`: `{objectFormat, shallow}` from the local repository.
- `baseline`: run baseline commit and its relationship to the commit's first
  parent. `baselineRelation` is one of `same-as-parent`, `ancestor-of-parent`,
  `unrelated`, or `unknown`; any missing information (including a root commit
  with a recorded baseline, or a shallow boundary where ancestry cannot be
  determined) is recorded as `unknown`, never guessed.
- `references`: the agent-change index envelope reference, when present.
- `trailers`: the `Brigade-Run` and `Brigade-Receipt` trailers from the commit
  message, with a flag for whether the run id matches and whether the receipt
  digest resolves to the local run receipt.
- `forgeEvidence`: `{status: "not-included"}`.
- `emittedAt`, `nonce`, `policy`, `project`, `signerIndependence`.

## The normalization rule

`localio.tree_fingerprint` excludes a fixed set of evidence paths when taking
the run fingerprint. To compare a commit tree against that fingerprint, the
excluded paths must be reset to the state that existed when the fingerprint was
taken. The statement records the base used and how it was chosen.

The normalization base can come from one of four sources:

1. `receipt-head`: when the run receipt records `tree_fingerprint_head`, the
   HEAD commit that was checked out when the fingerprint was taken. This is the
   strongest source because it proves the exact state used to reset excluded
   evidence paths, even when the linked commit's first parent is not the
   baseline.
2. `run-baseline`: when the run's `baseline_commit` equals the linked commit's
   first parent. This faithfully reproduces the HEAD that was checked out when
   the run fingerprint was taken, but is only available on linear commits that
   start from the recorded baseline.
3. `first-parent-assumed`: when the first parent is used but neither the
   receipt head nor the run baseline is available or matches. The verifier still
   records the comparison, but reports `NOT-EQUIVALENT` because an assumed base
   can produce false negatives.
4. `unavailable`: the linked commit is a root commit and has no parent. The
   normalized tree cannot be computed.

The normalized tree is computed with a temporary `GIT_INDEX_FILE` by reading
the commit tree, resetting the exclusion paths to the base tree, and writing
the result.

## Verifier axes

`verify-commit-linkage` reports observations, never a single boolean. The axes
are:

- `envelope`: `syntax`, `signature`, `trust`, `freshness`, `policy`, `project`.
- `commitAvailable`: `present` or `missing`.
- `objectFormat`: `match` or `mismatch`.
- `attestedTreeObject`: `present` or `missing` (the tree object may have been
  pruned by `git gc`).
- `commitTree`: `match`, `mismatch`, or `unavailable`.
- `normalizedTree`: `match`, `mismatch`, or `unavailable`.
- `ruleDrift`: `true` when the statement's exclusion list differs from the
  verifier's; the verifier still evaluates under the statement's list.
- `equivalence`: `confirmed`, `contradicted`, or `unavailable`.
- `runBinding`: `bound`, `conflicted`, or `unavailable`.
- `indexBinding`: `bound`, `conflicted`, or `absent`.
- `baselineRelation`: `confirmed`, `contradicted`, or `unavailable`.

Overall `status`:

- `LINKED-EXACT`: the commit tree equals the attested tree.
- `LINKED-NORMALIZED`: the normalized commit tree equals the attested tree, the
  exclusion rule has not drifted, and the normalization base is provable: either
  its source is `run-baseline`, the base commit equals the local `run.json`
  `baseline_commit`, and the base commit equals the recomputed first parent; or
  its source is `receipt-head` and the base commit equals the local `run.json`
  `tree_fingerprint_head`.
- `NOT-EQUIVALENT`: trees do not match, or normalized equivalence was based on
  an assumed base, or the rule drifted.
- `INVALID`: envelope signature or trust failure, policy/project mismatch,
  object-format mismatch, or `--commit` format mismatch.
- `UNVERIFIABLE`: missing policy, malformed envelope, or missing git objects.

## Exit codes

`export commit-linkage` exits `0` on `exact` or `normalized`, `3` on `none`,
and `1` or `2` on errors.

`verify-commit-linkage` exits `0` only for `LINKED-EXACT` or
`LINKED-NORMALIZED`, and `1` otherwise.

## Limitations

- Trailers are commit-message text and are spoofable. They are recorded for
  information only and never feed `status`.
- The linkage is signed with the shared workspace attestation key, which can
  sign a linkage for any commit that shares the attested tree.
- No forge chronology, branch protection state, or review evidence is included.
- The attested tree object may be unreachable and can be pruned by `git gc`.
  A missing object does not change the digest comparison; it only means the
  object cannot be probed directly.
