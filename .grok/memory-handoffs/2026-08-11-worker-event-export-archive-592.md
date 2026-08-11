# Memory Handoff

## Type

project-context

## Title

Worker-event export/archive fail-closed boundary (#592)

## Summary

Completed the remaining #592 export/archive slice after #823/#862. `brigade runs export` no longer copies raw `events/<worker>.jsonl` as public support data; it emits `events/<worker>.scrubbed.json` via the existing scrubber (distinct media type, artifact role, redacted privacy) or refuses with a bounded export-privacy diagnostic. Source raw streams stay local/unclassified for `policy=local-only`.

## Durable facts

- Export helpers: `worker_events.project_worker_streams_for_export` and `assert_export_tree_has_no_raw_worker_streams`
- Archive classification: scrubbed sidecar `role=artifact`, `privacy_class=redacted`, `media_type=SCRUBBED_MEDIA_TYPE`, `nested_schema=STREAM_SCHEMA`
- Raw `events/*.jsonl` (except lifecycle) fail closed in `_classify_path` / `validate_archive`
- Source run directories are unchanged; resume/audit local-only policy is preserved
- Legacy backfill of on-disk historical streams remains out of scope

## Evidence

- files changed: `src/brigade/worker_events.py`, `src/brigade/work_run_archive.py`, `tests/test_work_run_archive.py`, `docs/worker-event-streams.md`, `docs/receipt-schemas.md`, `CHANGELOG.md`
- commands run: `pytest -q tests/test_work_run_archive.py tests/test_worker_events.py`, `./scripts/verify`
- error strings: `raw worker stream crossed an export boundary`; `export refuses raw worker stream`

## Recommended memory action

no-card

## Target document

.learnings/FEATURE_REQUESTS.md

## Suggested document content

### Export boundary for worker streams

Issue #592 export/archive wiring now fail-closes portable `brigade.work-run`
archives: scrubbed sidecars only, never raw public support classification.
Legacy on-disk backfill remains optional follow-up work.
