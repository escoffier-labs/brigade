# Memory Handoff

## Type

bugfix

## Title

PR #668: classify crashed checkpoint temps before decoding; tolerate decoder resource failures on hash-named checkpoints

## Summary

Fixed the native Codex P2 in PR #668 by reordering `_expected_verify_archive_manifest` so `.checkpoint.*.tmp` files are classified and omitted before any bytes are read or JSON-decoded. Also hardened JSON parsing in `_expected_verify_archive_manifest` and `strip_checkpoint_bodies_for_export` so `UnicodeDecodeError`, `JSONDecodeError`, `ValueError`, `RecursionError`, and `MemoryError` are treated as non-reference checkpoint content and continue to hash/filename validation instead of aborting archival. This prevents a deeply nested crashed temp from pinning a stale run, while allowing legitimate deeply nested hash-named checkpoints to export as artifact references.

## Durable facts

- `.checkpoint.*.tmp` files have no canonical `{sha256}.json` path and must be omitted from the archive export tree unconditionally; this decision can be made from the filename alone.
- JSON decoding of checkpoint bodies is only used to detect an already-canonical artifact reference. Decoder/resource failures on `.json` files must not abort archival or pin retention; they should fall through to content-hash/filename validation.
- `strip_checkpoint_bodies_for_export` needs the same error handling as `_expected_verify_archive_manifest`; otherwise a hash-named deeply nested checkpoint would raise `RecursionError` during staging export even though the expected manifest was computed successfully.
- Canonical artifact-reference filename validation is unchanged: a reference payload is only accepted as canonical at `<sha256>.json`; any other filename still falls through to hash validation and is omitted as a mismatch.

## Evidence

- files changed: `src/brigade/work_cmd/verification.py`, `src/brigade/run_checkpoint.py`, `tests/test_work_cmd_verification.py`
- commands run:
  - `brigade work verify run --target . --command "python3 -m pytest tests/test_work_cmd_verification.py -k 'archive or prune or checkpoint' -q" --capture brigade-work` (31 passed)
  - `brigade work verify run --target . --command "ruff check src/brigade/work_cmd/verification.py src/brigade/run_checkpoint.py tests/test_work_cmd_verification.py" --capture brigade-work` (All checks passed!)
  - `brigade work verify run --target . --command "ruff format --check src/brigade/work_cmd/verification.py src/brigade/run_checkpoint.py tests/test_work_cmd_verification.py" --capture brigade-work` (3 files already formatted)
  - `brigade work verify run --target . --command ".venv/bin/mypy src/brigade/work_cmd/verification.py src/brigade/run_checkpoint.py" --capture brigade-work` (Success: no issues found in 2 source files)
  - `brigade work verify run --target . --command "git diff --check" --capture brigade-work` (clean)
- tests added:
  - `test_archive_verify_run_omits_deeply_nested_crashed_checkpoint_temp`
  - `test_archive_verify_run_exports_deeply_nested_hash_named_checkpoint_as_reference`

## Recommended memory action

no-card

## Target document

.learnings/LEARNINGS.md

## Suggested document content

### Verification archive: classify crashed checkpoint temps before decoding

In `_expected_verify_archive_manifest`, always classify and omit `.checkpoint.*.tmp` files from the verify-archive export based on the filename alone, before reading or decoding their arbitrary body bytes. Crashed temp bytes are private and uncanonical; decoding them first can raise `RecursionError` or other decoder exceptions, which aborts archival and leaves the stale run unprunable on every retry.

### Decoder/resource failures on checkpoint JSON are non-reference, not fatal

When checking whether a non-temp `.json` checkpoint is already an artifact reference, catch `UnicodeDecodeError`, `JSONDecodeError`, `ValueError`, `RecursionError`, and `MemoryError` and treat the result as non-reference content. Continue to content-hash/filename validation so a valid hash-named checkpoint still exports as an artifact reference, while a malformed or mismatched file is omitted with a bounded diagnostic. Apply the same exception handling in `strip_checkpoint_bodies_for_export` so the staging export does not abort after the expected manifest has been computed.

### Do not weaken canonical artifact-reference filename validation

An artifact-reference payload is only accepted as already-canonical when it lives at its declared `<sha256>.json` path. A reference-shaped payload under any other filename must still fall through to hash/filename validation and be omitted as a hash mismatch.
