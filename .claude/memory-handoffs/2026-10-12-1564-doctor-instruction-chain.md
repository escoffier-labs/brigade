# Memory Handoff

## Type

decision

## Title

Doctor reports a Codex instruction-chain estimate under stated assumptions

## Summary

`brigade doctor` now adds an `instruction-chain: codex` check for codex-harness
targets by calling the existing pinned preview with doctor-supplied assumptions.
Doctor cannot observe effective Codex trust or configuration, so every result is
labeled an estimate and names its assumptions and cap source.

## Durable facts

- Doctor assumes Codex 0.160.0, trust `trusted`, read access `full`, cwd equal
  to the target, Codex defaults for omitted settings, and no global scope.
- Status mapping: `OK` within the cap, `WARN` for a truncated or cap-exhausted
  selected file, and `MANUAL` with non-baseline reason codes when the preview
  is not complete or does not match Codex.
- A target without a `.git` marker reports `MANUAL root_outside_scope_or_unknown`,
  because the preview never searches above the approved scope.
- The existing `bootstrap-budget` check reads only `AGENTS.md`. The chain check
  reports the Codex winner, which may be `AGENTS.override.md`.
- The roster's orchestrator seat `sol61` cannot be targeted with
  `brigade run --worker`. A review must be routed through an orchestrated run.
- Still open on #1564: automatic effective-configuration observation,
  actual-session consumption, and other harnesses or platforms.

## Evidence

- files changed: `src/brigade/doctor.py`, `src/brigade/doctor_agent.py`,
  `tests/test_doctor.py`, `docs/codex-context-preview.md`, `CHANGELOG.md`
- graph: `brigade code affected src/brigade/doctor.py` lists 11 impacted files,
  including `status.py`, `doctor_agent.py` and `cli/doctor.py`. Only one Go
  test is statically attributed, so this is a lower bound.
- fail-first: receipt `20261012-154157-work-verify-a4b067`,
  `./scripts/verify-focused tests/test_doctor.py -k codex_instruction_chain`,
  `8 failed, 1 passed`.
- focused: receipt `20261012-154231-work-verify-68b3ad`, `9 passed`; broader
  slice receipt `20261012-154248-work-verify-bc02f8`, `402 passed in 20.12s`.

## Recommended memory action

no-card

## Target document

.learnings/LEARNINGS.md

## Suggested document content

### Doctor integrations of caller-supplied inspections

When doctor wraps an inspection that requires caller-supplied effective settings,
state every assumption in the check detail, label the result an estimate, and
print the explicit command for rerunning with effective settings. Map
unevaluated results to `MANUAL` with reason codes rather than `OK`.
