# Memory Handoff

## Type

workflow

## Title

Release tooling preflight for published native assets

## Summary

The future stable release path now proves that Unix published acceptance cannot resolve Go before managed setup or managed-binary smoke. Darwin amd64 acceptance also executes and validates agent-notify under Rosetta, agent-notify build dates derive from the tagged commit, and the release guide records the 25-native-asset and 27-file inventory.

## Durable facts

- Unix published acceptance removes PATH directories containing `go`, then checks `shutil.which("go", path=...)` immediately before online setup and immediately before managed-component smoke.
- Darwin amd64 acceptance runs `arch -x86_64 <agent-notify> version --json` and applies the same version, commit, and UTC build-date validation as the managed host binary.
- The publish workflow derives agent-notify `BUILD_DATE` from the committer timestamp of `github.sha`, so strict release-asset resume checks can reproduce binary metadata.
- A complete GitHub release contains five managed components across five platforms, for 25 native assets and 27 files after `component-manifest-v1.json` and `checksums.txt`.
- Package version `0.25.1`, changelog headings, component manifest hashes, issue state, and publication state were intentionally unchanged.

## Evidence

- files changed: `.github/workflows/publish.yml`, `RELEASE.md`, `scripts/published-artifact-acceptance.py`, `tests/test_publish_workflow.py`, `tests/test_published_artifact_acceptance.py`, `tests/test_release_checklist.py`
- Brigade implementation run: `20260729-193355-10e68a42`, with `code_graph_brief.attached=true`
- focused verification: `brigade work verify run --target . --command '.venv/bin/pytest -q tests/test_published_artifact_acceptance.py tests/test_publish_workflow.py tests/test_release_checklist.py' --capture brigade-work`
- focused receipt: `20260729-195000-work-verify-7142ca`
- full verification: `brigade work verify run --target . --command './scripts/verify' --capture brigade-work`
- full receipt: `20260729-195037-work-verify-1cbdad`
- full result: `4905 passed, 3 skipped in 460.33s`, total coverage `82.98%`
- corrected fast-gate error: `unformatted: File would be reformatted`

## Recommended memory action

no-card

## Target document

TOOLS.md

## Suggested document content

### Brigade stable-release native asset preflight

Published Unix acceptance must construct a Go-free PATH and confirm `shutil.which("go", path=...)` returns `None` immediately before managed setup and managed-binary smoke. Darwin amd64 acceptance must run agent-notify with `arch -x86_64 ... version --json` and validate its version, commit SHA, and UTC build date. Agent-notify `BUILD_DATE` comes from the tagged commit timestamp. The release inventory is 25 native assets and 27 total GitHub release files after the generated manifest and checksum file.
