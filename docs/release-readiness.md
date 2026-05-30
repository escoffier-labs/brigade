# Release Readiness

`brigade release` is a local publish gate. It collects the receipts and health checks Brigade already knows how to read, then writes a reviewable release-readiness receipt without pushing, tagging, creating a release, or mutating remotes.

## Commands

```bash
brigade release plan
brigade release doctor
brigade release run
brigade release runs
brigade release show <run-id>
brigade release schema
brigade release ci doctor
brigade release ci import-issues
brigade release smoke plan
brigade release smoke record
brigade release smoke list
brigade release smoke show <receipt-id>
brigade release smoke doctor
brigade release candidate plan
brigade release candidate build
brigade release candidate list
brigade release candidate show <candidate-id>
brigade release candidate audit <candidate-id>
brigade release candidate compare <candidate-id>
brigade release candidate import-issues <candidate-id>
brigade release candidate closeout <candidate-id>
brigade release candidate archive <candidate-id>
```

`plan` gathers local evidence and reports readiness without running publish checks or writing a receipt.

`doctor` runs local publish checks such as content-guard when available, then reports blockers and warnings without writing a receipt.

`run` performs the same readiness checks as `doctor` and writes a receipt under:

```text
.brigade/release/runs/
```

`runs` and `show` inspect those receipts.

`release candidate` commands turn readiness receipts into local candidate bundles under `.brigade/release/candidates/`. They do not push, tag, create releases, or mutate remotes. See [`release-candidates.md`](release-candidates.md).

`brigade release schema` is read-only and emits the local release evidence schema manifest for wrappers. It documents the release readiness, candidate, fleet train, waiver, and manual evidence record shapes, then reports missing latest or referenced receipts.

`brigade release ci doctor` reads local GitHub Actions workflow files plus saved CI summaries or logs and reports platform deprecation warnings with redacted safe excerpts. `brigade release ci import-issues` routes those warnings into the local work inbox as `source: ci-platform-deprecation`. Brigade does not call GitHub, edit workflows, rerun CI, or mutate remotes.

`brigade release smoke plan` shows the supported install smoke matrix for depth and harness combinations. `record` stores a local receipt or parses a local receipt JSON file, while `list`, `show`, and `doctor` inspect missing, failed, blocked, or stale smoke evidence. These commands record local evidence only; they do not run installs.

Use `brigade release candidate audit` and `compare` before publishing from an older candidate. Audit reports stale evidence, missing references, changed HEAD/docs/command contracts, and privacy-boundary issues. Compare reports changed HEAD, missing referenced receipts, newer local verification, review, scanner, or security evidence, and docs changed after candidate build. Use `brigade release candidate import-issues` to route audit findings into local work, and use `brigade release candidate closeout` to mark a candidate draft, reviewed, superseded, or archived without moving the bundle.

`brigade center readiness plan` is the higher-level local operator closeout over release readiness. It reuses the latest release readiness receipt plus roadmap audit, docs command inventory, center state, repo fleet, security, memory, tools, context, learning, and waiver state, then writes only local readiness receipts and manual-only checklist files.

## Evidence

Release readiness includes:

- latest work closeout
- latest work verification receipt
- latest code review closeout
- latest scanner sweep review state
- latest security health, public template privacy audit, and evidence state
- local GitHub Actions platform deprecation warnings when workflow files or saved CI summaries are present
- local install smoke matrix receipts for supported depth and harness combinations
- handoff draft and ingest health
- local closeout state for backup, handoff, memory-care, security, and release candidates when available
- context pack freshness, project consolidation state, learning candidates, operator-center pending review summaries, and operator report freshness where configured
- git branch, dirty tracked files, upstream, ahead count, and behind count
- README, CHANGELOG, and ROADMAP touch warnings when user-facing code changed
- content-guard tip scan when available
- introduced-content scan when a base ref is provided and content-guard is available

## Blockers And Warnings

Blockers include missing or blocked work closeout, missing or failed verification, unclosed review runs, unresolved code-review findings, unresolved scanner sweep issues, open security issues, stale or invalid handoff draft state, content-guard failures, and dirty tracked files.

Warnings include missing content-guard, docs/changelog/roadmap touch expectations, stale release candidate bundles, missing candidate receipt references, changed git HEAD since candidate build, and candidates built from blocked readiness. Warnings do not make the receipt blocked by themselves.

## Boundary

Release readiness is local and explicit. Brigade does not run `git push`, create tags, create GitHub releases, write pull request comments, mutate GitHub, approve content-guard findings, store secrets, start daemons, or install schedulers.
