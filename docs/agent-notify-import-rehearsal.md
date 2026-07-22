# agent-notify import rehearsal

Tracked rehearsal for Brigade issue #431. This document is a durable summary; the full commit map and machine-readable evidence belong in the #431 comment, not in-tree.

- Method: `git filter-repo --to-subdirectory-filter stations/notify --tag-rename :agent-notify/` on a fresh public clone, followed by a non-FF merge of the rewritten tip into a temporary Brigade worktree based on `origin/main`.
- No-rewrite invariant: the standalone `agent-notify` repository was never modified. The worktree, remote, and rewritten clone were temporary and removed. Two renamed tags (`agent-notify/v0.1.0`, `agent-notify/v0.1.1`) leaked into this local Brigade clone through linked-worktree shared refs, were never pushed to origin, and were deleted after review.
- Source pin: `9035424897cc3efb32ad06a9cfb42f5739fc4a32` (public `origin/main`, 42 reachable commits).
- Destination: `stations/notify/` in a temporary Brigade worktree.
- Rewritten tip: `7c97909cea4b33a62c0e4411826bf40d6f99b974`.
- Merge commit (rehearsal-only, non-reproducible): `3827d4793fcc53d52f48e63295c9aebbef4cab59`.
- Commit count: 42 before, 42 after, 42 entries in the old-to-new map.
- Tag results: `v0.1.0` and `v0.1.1` are annotated tags; after rename they are `agent-notify/v0.1.0` and `agent-notify/v0.1.1`, preserving tagger, message, and peel commit mapping.
- Verification summary:
  - Map is bijective; every old and new SHA appears exactly once.
  - Parents, author/committer names, emails, timestamps, timezones, subjects, and bodies match for all 42 mapped commits.
  - Tree equality holds for every mapped commit and for the final tip after stripping `stations/notify/`.
  - `git shortlog -sne` matches for the imported history.
  - `git log --follow stations/notify/go.mod` shows 2 commits across the rename.
  - `go test ./...` from the relocated module: first attempt failed (run `20260722-103147-work-verify-dfd559`, exit 1) because the test harness reads the invoking user's default config path and an existing user config with an env-incomplete channel made four tests exit 2. Re-run with an isolated environment passed (run `20260722-103226-work-verify-b0a49a`, exit 0). No code changed between the two runs.
  - `go vet ./...` from the relocated module passes (run `20260722-103242-work-verify-11a7e0`, exit 0).
  - The repository gate `./scripts/verify` passes (run `20260722-103252-work-verify-c670ab`, exit 0).
- Go receipt caveat: the Go verify receipts wrapped a temporary wrapper script in a deleted temp directory, so those receipts do not attest the module path or environment used. Phase 2 must run the relocated module's Go checks from the tracked tree with explicit config isolation (isolated `HOME` or explicit `--config`).
- Durable finding: public source tip `9035424` has non-hermetic CLI tests because they read the invoking user's default config file. Fix in the source repo or during import before CI runs the relocated module.
- Context: #431, #366, #352, #379, #381.

Phase 2 owns the real import. No `stations/notify` tree or import lands in this branch.
