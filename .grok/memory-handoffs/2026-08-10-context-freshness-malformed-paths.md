# Memory Handoff

## Type

bugfix

## Title

Context freshness rejects NUL and control-character persisted paths

## Summary

PR #838 sendback fixed a correctness/privacy defect in context-pack freshness reconciliation. `_safe_persisted_path` allowed NUL and other control characters, so malformed snapshot paths could be compared as drift and leak raw private path bytes in doctor output instead of reporting generic unsafe-path findings.

## Durable facts

- `_safe_persisted_path` now rejects any character with `ord(ch) < 32` before `Path` normalization.
- Doctor reports `unsafe_source_path`, `unsafe_dependent_receipt_path`, or `unsafe_source_reference` with generic detail; malformed bytes never appear in issue JSON.
- Regression coverage spans sources, dependent receipts, and source references for NUL and representative control characters.

## Evidence

- files changed: `src/brigade/context_cmd.py`, `tests/test_context_cmd.py`
- commands run: `brigade work verify run --target . --command "pytest -q tests/test_context_cmd.py::test_context_freshness_rejects_nul_and_control_character_paths tests/test_context_cmd.py::test_context_freshness_rejects_nul_and_control_character_source_references tests/test_context_cmd.py::test_context_freshness_malformed_and_private_paths_fail_safely" --capture brigade-work`
- verify run id: `20260810-122023-work-verify-ca7a75`

## Recommended memory action

no-card

## Target document

.learnings/ERRORS.md

## Suggested document content

### Context freshness malformed persisted paths

Persisted relative paths in context-pack freshness snapshots must not contain NUL or other control characters. `_safe_persisted_path` rejects `ord(ch) < 32`; otherwise doctor can misclassify the path as drift and echo the raw value in `detail`.
