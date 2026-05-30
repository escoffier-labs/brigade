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

## Boundary

Release candidate commands are local and explicit. Brigade does not push, tag, create GitHub releases, mutate pull requests, edit changelogs outside the candidate bundle, upload artifacts, approve content-guard findings, start daemons, install schedulers, or store secrets.
