# Memory Handoff

## Type
decision

## Title
#494 slim template-profile snapshot (no public registry)

## Summary
Operator closed #847 for inventing `brigade.template_registry.v1`, `brigade://template-profiles/*`, `supported_harness_versions` with synthetic `0.0.0`, and an unwired `check_harness_version`. The approved replacement keeps bundled profile selection, `brigade init --profile`, and deterministic render-hash snapshot/check, sourcing compatibility truth from existing `harness-contract.v1` `tested_version` / provenance / evidence fields only.

## Durable facts
- Module is `template_profiles` with snapshot schema `brigade.template_profile_snapshot.v1` (managed-snapshot style), not a public registry.
- Built-in profiles: `repo-claude`, `workspace-claude-codex`, `repo-claude-full`. `init --profile` is mutually exclusive with `--depth` / `--harnesses`; profile-less init is unchanged.
- Render digests use the shared `install.build_render_context` + `resolve_manifests` + `render()` path; `scripts/template_profile_snapshot.py --check` is wired into `./scripts/verify` and CI lint.
- Per-harness compatibility is projected from `docs/research/fixtures/harness-contract.v1` via `HARNESS_CONTRACT_FIXTURES` (e.g. `claude` → `claude-code`). Unmapped harnesses refuse invented claims; no min/tested `0.0.0` and no install-time version gate.
- Runtime `resolve_profile` loads the bundled snapshot with `verify_contracts=False` so package installs do not need the docs fixtures; `--check` verifies contracts against live fixtures in-repo.
- Protected #750 surfaces (`publish.yml`, `verify_version_check_endpoint.py`, `test_publish_workflow.py`) were not touched.

## Evidence
- files changed: `src/brigade/template_profiles.py`, `src/brigade/templates/template-profile-snapshot.json`, `src/brigade/install.py`, `src/brigade/cli/init.py`, `scripts/template_profile_snapshot.py`, `scripts/verify`, `.github/workflows/ci.yml`, `.gitignore`, `tests/test_template_profiles.py`, `CHANGELOG.md`, `.grok/memory-handoffs/2026-08-11-494-slim-template-profile-snapshot.md`
- commands run: GraphTrail sync + `impact install_selection` / `resolve_manifests`; focused pytest `20260811-044801-work-verify-4dc1ce` (exit 0); install+profile pytest `20260811-044831-work-verify-ef336b` (59 passed); full `./scripts/verify` `20260811-044839-work-verify-98986a` (6749 passed, 5 skipped, coverage 83.11%); final-tree gates `20260811-061232-work-verify-4ed846` (ruff/mypy/version/managed/template-profile snapshot all exit 0); focused+run_cli rerun `20260811-061157-work-verify-12870d` (template/install/surface + previously flaky run_cli cancel tests exit 0; shell `&&` gate argv rejected by design)
- error strings: ruff format wanted single-line `tested_version` assignment; CI wiring assert must match `scripts/template_profile_snapshot.py --check` substring inside `"$PY/python" ...`; second full verify `20260811-053018-work-verify-a4d22f` failed 5 unrelated `test_run_cli` SIGINT cancel races with `TimeoutExpired` after 3s under suite load (reproduced green in isolation)

## Recommended memory action
no-card

## Target document
.learnings/LEARNINGS.md

## Suggested document content
### #494 template-profile snapshot contract
Keep bundled `init --profile` and render-hash snapshot/check. Project harness compatibility from `harness-contract.v1` fixtures only. Do not reintroduce `brigade.template_registry.v1`, `brigade://template-profiles/*`, `supported_harness_versions`, synthetic `0.0.0` claims, or an unwired `check_harness_version`.
