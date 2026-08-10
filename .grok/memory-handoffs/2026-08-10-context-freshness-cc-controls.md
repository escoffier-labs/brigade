# Memory Handoff

## Type

bugfix

## Title

Context freshness rejects full Unicode Cc control characters in persisted paths

## Summary

PR #838 final sendback closed a gap in `_safe_persisted_path`: the prior fix rejected C0 controls (`ord(ch) < 32`) but still accepted DEL (U+007F) and C1 controls such as NEL (U+0085). Those bytes could reach `source_drift` detail instead of generic unsafe-path findings.

## Durable facts

- `_safe_persisted_path` now rejects every Unicode control character (`unicodedata.category(ch) == "Cc"`) before `Path` normalization.
- Doctor reports `unsafe_source_path`, `unsafe_dependent_receipt_path`, or `unsafe_source_reference` with generic detail; malformed bytes never appear in issue JSON.
- Regression coverage spans sources, dependent receipts, and source references for C0, DEL, and representative C1 values.

## Evidence

- files changed: `src/brigade/context_cmd.py`, `tests/test_context_cmd.py`
- commands run: `brigade work verify run --target . --command "pytest -q tests/test_context_cmd.py::test_context_freshness_rejects_nul_and_control_character_paths tests/test_context_cmd.py::test_context_freshness_rejects_nul_and_control_character_source_references" --capture brigade-work`
- verify run id: `20260810-122905-work-verify-f7e6df`

## Recommended memory action

no-card

## Target document

.learnings/ERRORS.md

## Suggested document content

### Context freshness malformed persisted paths

Persisted relative paths in context-pack freshness snapshots must not contain Unicode control characters (category Cc). `_safe_persisted_path` rejects Cc before normalization; otherwise doctor can misclassify the path as drift and echo the raw value in `detail`.
