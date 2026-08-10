# Memory Handoff

## Type
bugfix

## Title
skills audit load_error redaction and collision-resistant external labels (#818)

## Summary
Independent review of PR #818 found two remaining output-safety defects after the first path-redaction repair: missing path-based skill selectors were echoed raw into `load_error`/`load_warnings`, and `_public_path` basename-only `external:<name>` labels collided for distinct outside paths. Both are fixed without changing producer_run_id matching, advisory semantics, or transport behavior.

## Durable facts
- `_load_skill_metadata` must emit public skill selectors in error text (`skill not found: <public>`; loadable exceptions scrub known path spellings via `_public_load_error_text`).
- Outside-target public labels are now `external:<basename>-<sha256[:12]>` of the resolved posix path (or raw text on resolve failure): deterministic, collision-resistant, non-revealing.
- GraphTrail impact (post-edit): `_external_public_label` ← `_public_path` ← `_public_skill_id` / `_public_source` / `_evidence_ref` / `build_audit_payload`; `_public_load_error_text` ← `_load_skill_metadata` ← `build_audit_payload`. No new callers outside `src/brigade/skill_obligations.py`.
- Prior basename-only `external:<name>` contract is superseded; tests assert distinct labels for same-basename `/a/foo` vs `/b/foo`.

## Evidence
- files changed: `src/brigade/skill_obligations.py`, `tests/test_skill_obligations_audit.py`, `CHANGELOG.md`
- commands run: final GREEN `brigade work verify run ... pytest ...` run `20260810-114735-work-verify-8df36c` (exit 0); ruff check `20260810-114716-work-verify-f90ddd` (exit 0); ruff format check `20260810-114724-work-verify-4b7e3f` (exit 0)
- error strings: pre-fix `skill not found: /.../missing-skill` and colliding `external:foo`

## Recommended memory action
no-card

## Target document
.learnings/LEARNINGS.md

## Suggested document content
### skills audit external path labels
Public `brigade skills audit` labels outside the audited target as `external:<basename>-<12-hex digest>` of the resolved path key. Missing path-based skill selectors must use the same public label inside `load_error` and `load_warnings.detail`; never echo the absolute selector.
