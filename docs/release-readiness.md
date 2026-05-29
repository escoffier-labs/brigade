# Release Readiness

`brigade release` is a local publish gate. It collects the receipts and health checks Brigade already knows how to read, then writes a reviewable release-readiness receipt without pushing, tagging, creating a release, or mutating remotes.

## Commands

```bash
brigade release plan
brigade release doctor
brigade release run
brigade release runs
brigade release show <run-id>
```

`plan` gathers local evidence and reports readiness without running publish checks or writing a receipt.

`doctor` runs local publish checks such as content-guard when available, then reports blockers and warnings without writing a receipt.

`run` performs the same readiness checks as `doctor` and writes a receipt under:

```text
.brigade/release/runs/
```

`runs` and `show` inspect those receipts.

## Evidence

Release readiness includes:

- latest work closeout
- latest work verification receipt
- latest code review closeout
- latest scanner sweep review state
- latest security health and evidence state
- handoff draft and ingest health
- git branch, dirty tracked files, upstream, ahead count, and behind count
- README, CHANGELOG, and ROADMAP touch warnings when user-facing code changed
- content-guard tip scan when available
- introduced-content scan when a base ref is provided and content-guard is available

## Blockers And Warnings

Blockers include missing or blocked work closeout, missing or failed verification, unclosed review runs, unresolved code-review findings, unresolved scanner sweep issues, open security issues, stale or invalid handoff draft state, content-guard failures, and dirty tracked files.

Warnings include missing content-guard and docs/changelog/roadmap touch expectations. Warnings do not make the receipt blocked by themselves.

## Boundary

Release readiness is local and explicit. Brigade does not run `git push`, create tags, create GitHub releases, write pull request comments, mutate GitHub, approve content-guard findings, store secrets, start daemons, or install schedulers.
