# Release Candidates

`brigade release candidate` turns local release readiness evidence into a reviewable candidate packet. It is designed for the step after `brigade release run` and before any manual publish action.

## Commands

```bash
brigade release candidate plan
brigade release candidate build
brigade release candidate list
brigade release candidate show <candidate-id>
brigade release candidate compare <candidate-id>
brigade release candidate closeout <candidate-id>
brigade release candidate archive <candidate-id>
```

`plan` previews the candidate evidence without writing files.

`build` writes a bundle under:

```text
.brigade/release/candidates/
```

`list` and `show` inspect bundles. `compare` checks the candidate against current local state, including changed HEAD, missing referenced receipts, newer verification or review evidence, newer scanner, security, or operator report evidence, and docs changed after the bundle was built.

`closeout` writes a local `CLOSEOUT.json` into the candidate bundle with `draft`, `reviewed`, `superseded`, or `archived` state. `archive` moves a reviewed candidate into the local archive so it no longer appears as the latest candidate.

## Bundle Files

Each candidate contains:

- `RELEASE_CANDIDATE.md`, a human-readable summary of readiness, blockers, warnings, and changed files.
- `RELEASE_NOTES_DRAFT.md`, an editable draft inferred from `CHANGELOG.md`, commit subjects since the base ref, and touched docs. Uncertain sections are marked `review-needed`.
- `PUBLISH_PLAN.md`, a manual checklist for verification, closeout, release doctor, content guard, tag, push, and release creation. Remote-mutating commands are marked manual-only and are not run.
- `EVIDENCE.json`, stable wrapper-friendly evidence for the release readiness receipt, work closeout, verification receipt, code review closeout, scanner sweep state, security state, handoff state, git state, changed files, docs touch status, content-guard summaries, blockers, warnings, and suggested next commands.
- latest operator report health, including stale reports, missing receipt references, changed HEAD, and newer center activity warnings.

## Candidate Health

`brigade release doctor` warns when the latest candidate is stale, references missing receipts, was built from blocked readiness, or no longer matches the current git HEAD.

These are warnings because candidate bundles are review artifacts. They do not replace release readiness checks, verification, code review closeout, or content guard.

Release readiness, candidate bundles, and candidate compare include operator report health so an operator can tell whether the local daily review packet is fresh and closed out before publishing manually.

## Fleet Release Trains

`brigade repos release plan/build/list/show/compare/closeout/archive` coordinates release readiness across configured local repos. Fleet release train bundles live under `.brigade/repos/releases/` and include `FLEET_RELEASE_TRAIN.md`, `FLEET_RELEASE_EVIDENCE.json`, and `MANUAL_PUBLISH_PLAN.md`.

The train evidence uses safe repo ids, safe labels, local ids, statuses, counts, fingerprints, receipt labels, and suggested next commands only. It collects per-repo release readiness, release candidates, fleet action reconciliation, verification, review, security, and operator evidence, then classifies each repo as ready, blocked, needing review or dispatch, in progress, stale, missing a release candidate, or deferred.

The fleet publish plan is manual-only. It can include checklist labels for verification, release doctor, candidate compare, tagging, pushing, and release creation, but Brigade does not run those commands or mutate remotes.

Reviewed release trains can produce local release action queues with `brigade repos release actions build`. Operators can also record manual publish evidence with `brigade repos release evidence record`. Evidence records track the repo id, train id, step, status, safe summary, and fingerprints. `brigade repos release reconcile` marks train actions done only after required evidence is completed, skipped, or deferred, and `brigade repos release summary` shows missing or blocked evidence. These are receipts of manual work, not commands to run.

For final review, `brigade repos release report` writes local train review report files, `checklist` shows required manual evidence, `import-issues` routes unresolved evidence into the local work inbox, `activity` shows the local train ledger, `manifest` records bundle file labels and fingerprints, and `audit` checks bundle and evidence health. `ready` fails until blocked repos, unresolved actions, missing evidence, and blocked evidence are cleared or explicitly waived with `brigade repos release waivers`.

## Boundary

Release candidate and fleet release train commands are local and explicit. Brigade does not push, tag, create releases, mutate pull requests, edit changelogs outside generated local bundles, upload artifacts, approve content-guard findings, start daemons, install schedulers, or store secrets.
