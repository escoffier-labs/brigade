# Memory Handoff

## Type
bugfix

## Title
skills audit public path redaction (#818 / #499)

## Summary
`brigade skills audit` was emitting host-private absolute paths in JSON `target`/`run_dir`, receipt evidence paths (including unattributed), path-based skill `source.identity`, and text `target`/`run`. Public output now uses repo-relative labels under the audited target and `external:<name>` outside it, without changing producer_run_id matching or advisory semantics.

## Durable facts
- Public path helper lives in `src/brigade/skill_obligations.py`: under-target → repo-relative posix; outside → `external:<basename>`; audit `target` is always `.`.
- Path-kind skill source identities are rewritten only in the audit payload (`path:external:<name>` / `path:.brigade/...`); `_source_identity` in `skills_cmd` is unchanged.
- Exact `producer_run_id` attribution, advisory exit 0, and legacy unattributed receipt compatibility remain as approved on PR #818.
- GraphTrail pre-edit impact set for this repair: primary `src/brigade/skill_obligations.py` (`build_audit_payload` / `audit` / `_evidence_ref`); direct CLI caller `src/brigade/cli/skills.py`; skill load via `skills_cmd._load_skill` / `_source_identity`; receipt collectors `scorecard.discover_verify_receipt_paths`, `work_cmd.reviews._review_receipts`, `handoff_cmd.drafts._ingest_receipts`. Focused tests: `tests/test_skill_obligations_audit.py`, `tests/test_producer_run_id.py`.

## Evidence
- files changed: `src/brigade/skill_obligations.py`, `tests/test_skill_obligations_audit.py`, `tests/test_producer_run_id.py`, `CHANGELOG.md`
- commands run: `brigade work verify run --target . --command ".venv/bin/pytest -q tests/test_skill_obligations_audit.py tests/test_producer_run_id.py::test_skills_audit_text_output_labels_unattributed_without_secrets" --capture brigade-work` (run `20260810-114225-work-verify-1492db`, exit 0)
- earlier verify runs: `20260810-114153-work-verify-ffb7a5`, `20260810-114202-work-verify-787abb`
- error strings: none after fix

## Recommended memory action
no-card

## Target document
.learnings/LEARNINGS.md

## Suggested document content
### skills audit path labels
Public `brigade skills audit` JSON/text must never print absolute host paths. Use repo-relative paths under the audited target and `external:<name>` for outside paths; keep matching logic on real filesystem paths internally.
