# Memory Handoff

## Type

workflow

## Title

Claude Action job and artifact evidence have different execution scopes

## Summary

GitHub lists jobs for an exact workflow run attempt, but lists artifacts across
the workflow run. Associating every artifact with the queried attempt would
assign evidence from another execution to the current attempt.

## Durable facts

- The jobs endpoint is `GET /repos/{owner}/{repo}/actions/runs/{run_id}/attempts/{attempt}/jobs`.
- The artifacts endpoint is `GET /repos/{owner}/{repo}/actions/runs/{run_id}/artifacts`.
  The documented artifact response has a `workflow_run` object without an attempt.
- Run-wide artifact evidence must retain unknown attempt association and must
  not inherit a particular attempt's definition revision.
- Known artifact and confirmed run heads must agree. GitHub reruns retain the
  original event's commit SHA.
- Runner labels and names do not independently establish hosting authority or
  an enrolled local node. Job-specific caller confirmation remains separate.
- A missing metadata resource cannot establish deletion, review completion,
  credential readiness or provider health.

## Evidence

- Sources: [GitHub workflow jobs](https://docs.github.com/en/rest/actions/workflow-jobs#list-jobs-for-a-workflow-run-attempt)
  and [GitHub artifacts](https://docs.github.com/en/rest/actions/artifacts#list-workflow-run-artifacts).
  Retrieved 2026-10-11 at 02:37 UTC. No publication date supplied.
- [GitHub reruns](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/re-run-workflows-and-jobs)
  retrieved 2026-10-11 at 02:49 UTC. No publication date supplied.
- Regression command: `brigade work verify run --target . --argv-json '["./scripts/verify-focused","tests/test_claude_actions_metadata.py"]' --capture brigade-work`.
- Receipt `20261011-023808-work-verify-c85087`: one regression failed before
  implementation with `ModuleNotFoundError: No module named 'brigade.claude_actions_metadata'`.
- Receipt `20261011-024024-work-verify-61879f`: expanded fixtures had 60 failures
  before implementation. These receipts are audit evidence.
- Receipt `20261011-025014-work-verify-a37e1b`: two review regressions failed
  before fixing artifact head consistency and quarantine page provenance.
- Receipt `20261011-025141-work-verify-255419`: focused metadata and transport
  suites passed with 322 tests after both fixes. The receipt is audit evidence.

## Recommended memory action

no-card

## Target document

.learnings/LEARNINGS.md

## Suggested document content

### Keep Actions metadata at its documented scope

GitHub exposes attempt-specific jobs and run-wide artifacts through different
endpoints. Retain unknown artifact attempt association until independent metadata
establishes the attempt. Compare each object's head with the current comparison head.
Successful execution and available metadata do not establish review coverage.
