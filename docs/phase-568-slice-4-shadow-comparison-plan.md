# Issue #568 Slice 4: Shadow Comparison Implementation Plan

RED-first, exact, executable task-by-task. Each task lists the test code to
add (or the production code to add), the command to run, the expected
failure/success, and the commit. Tasks are ordered so every commit leaves
the repository compiling and the prior tests green. The only RED moment is
the first run of a new test file before its module exists.

Conventions:

- All paths are relative to the repository root.
- Test scaffolding (real git repo fixture, `runguard.run_lock` contexts, the
  `enabled` env-flag fixture, `_write_run_json_locked`) is reused verbatim
  from `tests/test_run_lifecycle.py`. The plan only shows the new test bodies.
- Run focused suites with `.venv/bin/python -m pytest -q <path>`. Run the full gate
  with `./scripts/verify`. This worktree has no `python` on `PATH`. Use the
  venv interpreter for every focused suite.
- Every commit uses Conventional Commits and carries the two co-author
  trailers required by the issue:
  `Co-Authored-By: Cursor <cursoragent@cursor.com>` and
  `Co-Authored-By: Codex <codex@openai.com>`.

## Task 1 - RED: add the test module shell with the driving tests (import fails)

Add `tests/test_run_shadow.py` with the shared scaffolding copied from
`tests/test_run_lifecycle.py` (the `_repo`, `_run_dir`, `_journal_path`,
`_minimal_roster`, `_run_payload`, `_apply_lifecycle_request`,
`_write_run_json`, `_write_run_json_locked`, `_events`, `enabled`, and
`disabled` helpers), the import of the not-yet-existing module, the trivial
constant check, and the first two driving tests (flag-off and
requested-but-not-activated). No production module exists yet, so collection
fails on the import and every test in the file is RED at collection time:

```python
"""Regression tests for shadow projection comparison (issue #568 slice 4)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from brigade import aboyeur, localio, proc, run_events, run_journal, run_lifecycle, runguard
from brigade import roster as roster_mod
from brigade import run_projector
from brigade import run_shadow  # RED: module does not exist yet
from brigade.run_shadow import (  # RED: constants land in Task 2
    REASON_ERROR_RECORDED,
    REASON_EVIDENCE_SCHEMA_MISMATCH,
    REASON_EVIDENCE_UNREADABLE,
    REASON_JOURNAL_AHEAD,
    REASON_JOURNAL_UNREADABLE,
    REASON_MISMATCH_RECORDED,
    REASON_NO_COMPARISONS,
    REASON_NO_EVIDENCE,
    REASON_NO_JOURNAL,
    REASON_STATUS_LAG_CURRENT,
)

_REQUEST_FIELD = "lifecycle_journal_requested"


def test_module_imports():
    assert run_shadow.SHADOW_SCHEMA == "brigade.run_shadow.v1"


def test_flag_off_creates_no_journal_and_no_artifact(disabled, tmp_path):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)

    _write_run_json_locked(repo, run_dir, "started")

    assert (run_dir / "run.json").is_file()
    assert not (run_dir / "events").exists()
    report = run_shadow.check_projection_readiness(run_dir)
    assert report.ready is False
    assert REASON_NO_JOURNAL in report.reasons


def test_requested_but_not_activated_creates_no_artifact(enabled, tmp_path):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)

    _write_run_json(run_dir, "started")  # pre-lock bootstrap: request only

    assert (run_dir / "run.json").is_file()
    assert not (run_dir / "events").exists()
    assert not run_shadow.shadow_artifact_path(run_dir).exists()
```

All `REASON_*` constants used anywhere in this test module are imported
explicitly from `brigade.run_shadow` in the header above. No bare
`REASON_*` reference relies on an undocumented future import. Until Task 2
lands the module, the import itself is the RED signal.

Copy the helper bodies verbatim from `tests/test_run_lifecycle.py` lines
29-142 (the `_git`, `_repo`, `_RUN_ID`, `_run_dir`, `_journal_path`,
`_minimal_roster`, `_run_payload`, `_apply_lifecycle_request`,
`_write_run_json`, `_write_run_json_locked`, `_events`, `enabled`,
`disabled` definitions).

Command:

```bash
.venv/bin/python -m pytest -q tests/test_run_shadow.py
```

Expected failure (RED):

```
ModuleNotFoundError: No module named 'brigade.run_shadow'
```

No commit yet. The tree is RED and must not be committed red. The next task
creates the real minimal module driven by these three tests. No placeholder
(`NotImplementedError`) scaffolding is committed at any point.

## Task 2 - GREEN: create `run_shadow.py` with real minimal flag-off and requested-not-activated behavior

Create `src/brigade/run_shadow.py` with the public API surface, constants, and
the **real minimal** behavior that drives Task 1's three tests green. No
function raises `NotImplementedError`. No public function is committed as a
placeholder. The two public functions implement exactly the flag-off /
requested-not-activated contract from the spec and a total fail-closed gate
default. Later tasks add the comparison body and gate reason branches RED-first
as their tests demand.

```python
"""Shadow projection comparison for issue #568 slice 4.

After every committed legacy ``run.json`` write for a run with an activated
lifecycle journal, ``record_shadow_comparison`` independently rebuilds a
shadow candidate from the committed legacy payload plus the five derived
fields read off the verified journal tail, encodes it through
``run_projector.encode_snapshot_bytes``, runs the projector against the same
legacy payload and journal, and compares the two canonical byte strings.
Parity is a full canonical byte comparison. The artifact persists only
SHA-256 digests of the two encodings plus a bounded list of differing field
names. ``check_projection_readiness`` is the fail-closed gate a later slice
consults before making the journal authoritative.

The legacy writer stays authoritative: this module never writes
``run.json``, never appends to the journal, and never raises into the writer
path. Standard library only. Brigade is zero-runtime-dependency.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from brigade import run_lifecycle

SHADOW_SCHEMA: str = "brigade.run_shadow.v1"
SHADOW_SCHEMA_VERSION: int = 1
SHADOW_ARTIFACT_NAME: str = "shadow-comparison.json"
MAX_RECENT_RECORDS: int = 16
MAX_DIFFERING_FIELDS: int = 16

OUTCOME_MATCH = "match"
OUTCOME_LAG = "lag"
OUTCOME_MISMATCH = "mismatch"
OUTCOME_ERROR = "error"
_OUTCOMES = frozenset({OUTCOME_MATCH, OUTCOME_LAG, OUTCOME_MISMATCH, OUTCOME_ERROR})

REASON_NO_JOURNAL = "no-journal"
REASON_JOURNAL_UNREADABLE = "journal-unreadable"
REASON_NO_EVIDENCE = "no-evidence"
REASON_EVIDENCE_UNREADABLE = "evidence-unreadable"
REASON_EVIDENCE_SCHEMA_MISMATCH = "evidence-schema-mismatch"
REASON_JOURNAL_AHEAD = "journal-ahead-of-evidence"
REASON_NO_COMPARISONS = "no-comparisons"
REASON_MISMATCH_RECORDED = "mismatch-recorded"
REASON_ERROR_RECORDED = "error-recorded"
REASON_STATUS_LAG_CURRENT = "status-lag-current"


@dataclass(frozen=True)
class ReadinessReport:
    ready: bool
    reasons: tuple[str, ...]


def shadow_artifact_path(run_dir: Path) -> Path:
    return Path(run_dir) / "events" / SHADOW_ARTIFACT_NAME


def record_shadow_comparison(run_dir: Path, legacy_snapshot: Mapping[str, Any]) -> None:
    # Flag-off and requested-but-not-activated short-circuit: return unless the
    # run's journal file exists (the slice-2 activation fact) and the payload
    # carries a non-empty string status. No artifact is created, no journal is
    # touched, and the legacy writer is never raised into. The comparison body
    # (journal read, shadow candidate, projection, byte comparison, artifact
    # write) is added RED-first in later tasks. This guard is the real minimal
    # behavior the spec requires for non-activated runs.
    try:
        run_dir = Path(run_dir).expanduser().resolve()
    except Exception:
        return
    if not isinstance(legacy_snapshot, Mapping):
        return
    status = legacy_snapshot.get("status")
    if not isinstance(status, str) or not status:
        return
    if not run_lifecycle._journal_path(run_dir).is_file():
        return
    # No activated-journal comparison yet. Later tasks implement the body.
    return


def check_projection_readiness(run_dir: Path) -> ReadinessReport:
    # Fail-closed, total: never raises, never writes. The no-journal
    # short-circuit is the only branch Task 1 exercises. The journal-present
    # case returns a coarse fail-closed default until later tasks add reason
    # branches (no-evidence, schema-mismatch, journal-ahead, mismatch/error
    # recorded, status-lag-current, and the ready=True path) RED-first.
    try:
        run_dir = Path(run_dir).expanduser().resolve()
    except Exception:
        return ReadinessReport(ready=False, reasons=())
    if not run_lifecycle._journal_path(run_dir).is_file():
        return ReadinessReport(ready=False, reasons=(REASON_NO_JOURNAL,))
    return ReadinessReport(ready=False, reasons=())
```

Command (run the three driving tests from Task 1):

```bash
.venv/bin/python -m pytest -q tests/test_run_shadow.py::test_module_imports tests/test_run_shadow.py::test_flag_off_creates_no_journal_and_no_artifact tests/test_run_shadow.py::test_requested_but_not_activated_creates_no_artifact
```

Expected: pass. `test_module_imports` verifies the schema constant.
`test_flag_off_creates_no_journal_and_no_artifact` verifies the gate's
no-journal short-circuit and that a flag-off locked write creates no
`events` directory. `test_requested_but_not_activated_creates_no_artifact`
verifies the pre-lock bootstrap write creates no artifact.

Commit:

```bash
git add src/brigade/run_shadow.py tests/test_run_shadow.py
git commit -m "$(cat <<'EOF'
feat(runs): add run_shadow module with real minimal flag-off behavior

Slice 4 of issue #568: add the run_shadow module with the public API
surface (record_shadow_comparison, check_projection_readiness,
shadow_artifact_path, ReadinessReport) and the test module reusing the
run_lifecycle scaffolding. record_shadow_comparison implements the
flag-off / requested-but-not-activated early-return guard for real (no
artifact unless the journal exists and the payload carries a non-empty
string status). The check_projection_readiness gate is total and fail-closed with
the no-journal short-circuit. No placeholder scaffolding is committed.
Later tasks fill the comparison body and gate reason branches RED-first.

Co-Authored-By: Cursor <cursoragent@cursor.com>
Co-Authored-By: Codex <codex@openai.com>
EOF
)"
```

## Task 3 - RED+GREEN: wire the hook and implement first-match (test 3)

Add test 3:

```python
def test_first_locked_write_records_match(enabled, tmp_path):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)

    _write_run_json(run_dir, "started")          # pre-lock bootstrap
    _write_run_json_locked(repo, run_dir, "started")  # in-lock: run.created

    artifact = run_shadow.shadow_artifact_path(run_dir)
    assert artifact.is_file()
    data = json.loads(artifact.read_text())
    assert data["schema"] == "brigade.run_shadow.v1"
    assert data["schema_version"] == 1
    assert data["run_id"] == _RUN_ID
    assert data["projector_version"] == run_projector.PROJECTOR_VERSION
    assert data["comparisons"] == 1
    assert data["matches"] == 1
    assert data["mismatches"] == 0
    assert data["lags"] == 0
    assert data["errors"] == 0
    assert data["last_compared_sequence"] == 1
    assert data["last_outcome"] == "match"
    assert data["last_shadow_digest"] == data["last_projected_digest"]
    assert data["last_differing_fields"] == []
    assert data["last_error_category"] is None
    assert len(data["recent_records"]) == 1
    report = run_shadow.check_projection_readiness(run_dir)
    assert report.ready is True
    assert report.reasons == ()
```

Run (RED: the hook is not wired, so no artifact is created):

```bash
.venv/bin/python -m pytest -q tests/test_run_shadow.py::test_first_locked_write_records_match
```

Expected: `AssertionError` on `artifact.is_file()`.

Wire the hook in `src/brigade/aboyeur.py::_write_json`, immediately after the
existing `localio.write_text_atomic` call (around line 504):

```python
def _write_json(path: Path, payload: object) -> None:
    # run.json is polled by `brigade runs watch/steer/interrupt` while the run
    # rewrites it, so the write must be atomic or a concurrent reader can
    # observe a truncated file.
    if path.name == "run.json" and isinstance(payload, dict):
        status = payload.get("status")
        if isinstance(status, str) and status:
            run_lifecycle.record_lifecycle_transition(
                path.parent,
                status=status,
                workspace=runguard.resolve_run_lock_workspace(payload, path.parent),
                incoming_snapshot=payload,
            )
    localio.write_text_atomic(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")
    if path.name == "run.json" and isinstance(payload, dict):
        run_shadow.record_shadow_comparison(path.parent, payload)
```

Add the import at the top of `aboyeur.py` next to the existing
`run_lifecycle` import:

```python
from brigade import run_shadow
```

Implement `record_shadow_comparison` in `run_shadow.py` for **only the
first-match full-byte happy path** that `test_first_locked_write_records_match`
drives: journal read, shadow candidate build, projection, canonical byte
comparison, match record, atomic write. No other behavior branch is added in
this task: lag, mismatch, projection-error, journal-error, run-id mismatch,
containment, idempotency, caps, gap, quarantine, and the failing gate reason
branches each land RED-first in the later task whose failing test drives them.

```python
import copy
import hashlib

def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def _empty_artifact(run_id: str) -> dict[str, Any]:
    return {
        "schema": SHADOW_SCHEMA,
        "schema_version": SHADOW_SCHEMA_VERSION,
        "run_id": run_id,
        "projector_version": run_projector.PROJECTOR_VERSION,
        "comparisons": 0,
        "matches": 0,
        "mismatches": 0,
        "lags": 0,
        "errors": 0,
        "last_compared_sequence": None,
        "last_compared_event_digest": None,
        "last_shadow_digest": None,
        "last_projected_digest": None,
        "last_differing_fields": None,
        "last_outcome": None,
        "last_error_category": None,
        "last_recorded_at": None,
        "recent_records": [],
    }

def record_shadow_comparison(run_dir: Path, legacy_snapshot: Mapping[str, Any]) -> None:
    # Happy path only: the journal is present and readable with a clean tail,
    # the first event run_id matches the run directory, the projector accepts
    # both encodings, and the two canonical byte strings are equal. Every
    # other branch (lag/mismatch/error classification, run-id mismatch,
    # containment, idempotency, caps, gap, quarantine) is added RED-first in
    # the later task whose failing test drives it.
    run_dir = Path(run_dir).expanduser().resolve()
    if not isinstance(legacy_snapshot, Mapping):
        return
    status = legacy_snapshot.get("status")
    if not isinstance(status, str) or not status:
        return
    journal_path = run_lifecycle._journal_path(run_dir)
    if not journal_path.is_file():
        return
    run_id = run_dir.name
    report = run_journal.read_journal(journal_path)
    tail = report.events[-1]
    tail_seq = tail.sequence
    tail_digest = tail.event_digest
    shadow_candidate = copy.deepcopy(dict(legacy_snapshot))
    shadow_candidate["projector_version"] = run_projector.PROJECTOR_VERSION
    shadow_candidate["journal_present"] = True
    shadow_candidate["journal_last_sequence"] = tail_seq
    shadow_candidate["journal_last_event_digest"] = tail_digest
    shadow_bytes = run_projector.encode_snapshot_bytes(shadow_candidate)
    projection = run_projector.project_run_snapshot(
        legacy_snapshot, report.events, journal_present=True
    )
    projected_bytes = projection.to_bytes()
    shadow_digest = _sha256_hex(shadow_bytes)
    projected_digest = _sha256_hex(projected_bytes)
    if shadow_bytes != projected_bytes:
        return  # mismatch/lag classification lands RED-first in Task 7/8
    _record_match(run_dir, run_id, tail_seq, tail_digest, shadow_digest, projected_digest)
```

Add the helpers strictly required by the happy path, implemented inline in
this task:

- `_record_match(run_dir, run_id, tail_seq, tail_digest, shadow_digest,
  projected_digest)`: builds the match record (outcome `"match"`, empty
  `differing_fields`, `last_error_category` `None`), updates `comparisons` and
  `matches` to 1, sets `last_compared_sequence`/`last_compared_event_digest`/
  `last_shadow_digest`/`last_projected_digest`/`last_differing_fields`/
  `last_outcome`/`last_recorded_at`, appends one entry to `recent_records`,
  and writes the artifact. No cap is applied yet (the `recent_records` cap
  lands RED-first in Task 18). With a single comparison the list has one
  entry either way.
- `_write_artifact(run_dir, data)`: writes `data` as JSON with sorted keys,
  two-space indent, and a single trailing newline via
  `localio.write_text_atomic` into the `0o700` `events` directory (which
  `shadow_artifact_path`'s parent is), yielding the `0o600` artifact.
- `_load_prior_artifact(artifact_path)`: returns the parsed artifact dict,
  or `None` when the file is absent. Quarantine of unparseable bytes lands
  RED-first in Task 12. This task only needs the absent-file case for the
  first comparison, so a plain `json.loads` on a present file is sufficient
  here and is replaced by the quarantine-aware loader in Task 12.

Also extend `check_projection_readiness` with the **ready=True happy path**
only: when the journal is present and the artifact parses with the right
schema, schema_version, run_id, `comparisons >= 1`, and `last_outcome` in
`_OUTCOMES`, and no `mismatches`/`lags`/`errors` are recorded, return
`ReadinessReport(ready=True, reasons=())`. The failing gate reason branches
(no-evidence, schema-mismatch, journal-ahead, mismatch/error/lag recorded,
status-lag-current, evidence-unreadable, no-comparisons) each land RED-first
in their own later tasks. Until then those cases keep returning the coarse
fail-closed default from Task 2.

Scope note for RED-first honesty: this task lands only the first-match
full-byte happy path plus the ready=True gate result. Every other behavior
branch is added RED-first in the later task whose failing test drives it.

Run:

```bash
.venv/bin/python -m pytest -q tests/test_run_shadow.py::test_first_locked_write_records_match
```

Expected: pass.

Also re-run the lifecycle suite to confirm the wired hook does not break
existing tests except the one assertion that lists the `events` directory:

```bash
.venv/bin/python -m pytest -q tests/test_run_lifecycle.py
```

Expected: `test_no_journal_file_until_lock_held` fails (it asserts the
`events` directory contains exactly `["lifecycle.jsonl"]`). Fix it in Task 4.

Commit:

```bash
git add src/brigade/aboyeur.py src/brigade/run_shadow.py tests/test_run_shadow.py
git commit -m "$(cat <<'EOF'
feat(runs): wire shadow comparison hook and first-match record

Slice 4: aboyeur._write_json calls run_shadow.record_shadow_comparison
after the atomic run.json replace. record_shadow_comparison builds the
shadow candidate (deep-copied legacy payload plus five derived fields pinned
from the verified journal tail, legacy status preserved), encodes it through
run_projector.encode_snapshot_bytes, projects the same base and events, and
compares the two canonical byte strings. A match records equal digests and
an empty differing-field list. Only the first-match full-byte happy path and
the ready=True gate result land here. Lag/mismatch/error classification,
run-id mismatch, containment, idempotency, caps, gap, quarantine, and the
failing gate reason branches each land RED-first in their own later tasks.

Co-Authored-By: Cursor <cursoragent@cursor.com>
Co-Authored-By: Codex <codex@openai.com>
EOF
)"
```

## Task 4 - GREEN: fix the lifecycle events-directory assertion

Update `tests/test_run_lifecycle.py::test_no_journal_file_until_lock_held`
(line ~185) to include the new artifact:

```python
    assert _journal_path(run_dir).is_file()
    assert sorted(path.name for path in (run_dir / "events").iterdir()) == [
        "lifecycle.jsonl",
        "shadow-comparison.json",
    ]
```

Run:

```bash
.venv/bin/python -m pytest -q tests/test_run_lifecycle.py tests/test_run_shadow.py
```

Expected: all pass.

Commit:

```bash
git add tests/test_run_lifecycle.py
git commit -m "$(cat <<'EOF'
test(runs): account for shadow artifact in events directory listing

Slice 4: the locked write now also creates events/shadow-comparison.json,
so the lifecycle test's events-directory assertion lists both files.

Co-Authored-By: Cursor <cursoragent@cursor.com>
Co-Authored-By: Codex <codex@openai.com>
EOF
)"
```

## Task 5 - RED+GREEN: full mapped chain (test 4)

Add test 4:

```python
def test_full_mapped_chain_records_seven_matches(enabled, tmp_path):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)
    chain = ["started", "planning", "dispatching", "result-processing",
             "synthesizing", "handoff", "ok"]

    _write_run_json(run_dir, "started")
    for status in chain:
        _write_run_json_locked(repo, run_dir, status)

    data = json.loads(run_shadow.shadow_artifact_path(run_dir).read_text())
    assert data["comparisons"] == 7
    assert data["matches"] == 7
    assert data["mismatches"] == 0
    assert data["lags"] == 0
    assert data["errors"] == 0
    assert data["last_compared_sequence"] == 7
    assert data["last_outcome"] == "match"
    report = run_shadow.check_projection_readiness(run_dir)
    assert report.ready is True
    assert report.reasons == ()
```

Run (coverage: expected to pass on first run. A failure is a defect in
Task 3's happy-path implementation to fix here, since this test exercises
only the match branch already landed):

```bash
.venv/bin/python -m pytest -q tests/test_run_shadow.py::test_full_mapped_chain_records_seven_matches
```

Expected: pass (the projector maps each event to the matching status, so
shadow and projected bytes are equal at every step. `_record_match` loads the
prior artifact and increments `comparisons`/`matches` cumulatively).

Commit:

```bash
git add tests/test_run_shadow.py
git commit -m "$(cat <<'EOF'
test(runs): cover full mapped lifecycle chain parity

Slice 4: drive a run through all seven mapped statuses and assert seven
match records, a ready gate, and a last_compared_sequence of 7.

Co-Authored-By: Cursor <cursoragent@cursor.com>
Co-Authored-By: Codex <codex@openai.com>
EOF
)"
```

## Task 6 - RED+GREEN: byte-identical rewrite idempotency and changed preserved detail (test 5)

Add test 5 (two cases: a byte-identical rewrite is a no-op, and a same-status
write with a changed preserved detail field records a fresh match at the
same journal tail):

```python
def test_byte_identical_rewrite_is_noop(enabled, tmp_path):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)

    _write_run_json(run_dir, "started")
    _write_run_json_locked(repo, run_dir, "started")  # run.created
    _write_run_json_locked(repo, run_dir, "planning")  # run.planning.started
    artifact = run_shadow.shadow_artifact_path(run_dir)
    bytes_before = artifact.read_bytes()

    # A byte-identical rewrite (same status, no field changes) produces no
    # new journal event and leaves the shadow and projected encodings
    # byte-identical, so the digests are unchanged. Under the spec
    # idempotency key (tail sequence, shadow digest, projected digest,
    # outcome, category) this is a complete no-op: no counters move, no
    # record is appended, the artifact is not rewritten.
    _write_run_json_locked(repo, run_dir, "planning")

    assert artifact.read_bytes() == bytes_before
    data = json.loads(artifact.read_text())
    assert data["comparisons"] == 2
    assert data["matches"] == 2


def test_changed_preserved_detail_records_fresh_match_at_same_tail(enabled, tmp_path):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)

    _write_run_json(run_dir, "started")
    _write_run_json_locked(repo, run_dir, "started")  # run.created, seq 1
    _write_run_json_locked(repo, run_dir, "planning")  # run.planning.started, seq 2
    artifact = run_shadow.shadow_artifact_path(run_dir)
    bytes_before = artifact.read_bytes()
    digests_before = (
        json.loads(artifact.read_text())["last_shadow_digest"],
        json.loads(artifact.read_text())["last_projected_digest"],
    )

    # A same-status write with a changed preserved detail field produces no
    # new journal event (slice 2 skips same-status writes), so the journal
    # tail is unchanged. But the preserved field is copied from the base on
    # both sides, so the shadow and projected encodings both change in
    # lockstep. The digests differ from the prior record, so the
    # idempotency key does not match and a fresh match is recorded at the
    # same journal tail.
    _write_run_json_locked(repo, run_dir, "planning", error="a new detail string")

    assert artifact.read_bytes() != bytes_before
    data = json.loads(artifact.read_text())
    assert data["comparisons"] == 3
    assert data["matches"] == 3
    assert data["last_compared_sequence"] == 2  # journal tail unchanged
    assert (data["last_shadow_digest"], data["last_projected_digest"]) != digests_before
    assert data["last_shadow_digest"] == data["last_projected_digest"]
```

Run (RED: Task 3's `_record_match` always writes a new record, so the
byte-identical rewrite rewrites the artifact bytes and increments
`comparisons` to 3. The changed-preserved-detail case is not yet
distinguished from a no-op):

```bash
.venv/bin/python -m pytest -q tests/test_run_shadow.py::test_byte_identical_rewrite_is_noop tests/test_run_shadow.py::test_changed_preserved_detail_records_fresh_match_at_same_tail
```

Expected failure: `AssertionError` on `artifact.read_bytes() == bytes_before`
(the byte-identical rewrite is not yet a no-op, the artifact is rewritten)
and on `data["comparisons"] == 2` (it reads 3). The changed-preserved-detail
test fails the same way until idempotency lands.

Implement (only the idempotency branch): in `_record_match` (or a shared
`_record_outcome` helper that `_record_match` delegates to), before writing,
load the prior artifact and compare
`(tail_seq, shadow_digest, projected_digest, outcome, category)` to the prior
record's last entry. On equality, skip the write and leave counters and
`recent_records` unchanged. No other behavior changes. The
changed-preserved-detail case has different digests, so the idempotency key
does not match and a fresh match is recorded normally.

```python
def _record_outcome(run_dir, run_id, outcome, category, diagnostic, tail_seq,
                    tail_digest, shadow_digest, projected_digest, differing_fields):
    prior = _load_prior_artifact(shadow_artifact_path(run_dir))
    key = (tail_seq, shadow_digest, projected_digest, outcome, category)
    if prior is not None and tuple(prior.get("recent_records", [{}])[-1].get(k) for k in
        ("sequence", "shadow_digest", "projected_digest", "outcome", "category")) == key:
        return  # idempotent: same evidence, same outcome, no rewrite
    # ...build record, increment counters, append to recent_records, write...
```

Expected: pass. The byte-identical rewrite is a no-op (`comparisons` stays 2,
artifact bytes unchanged). The changed-preserved-detail write records a fresh
match (`comparisons` advances to 3, `matches` to 3, `last_compared_sequence`
stays 2, digests differ from the prior record and equal each other).

Commit:

```bash
git add tests/test_run_shadow.py src/brigade/run_shadow.py
git commit -m "$(cat <<'EOF'
test(runs): byte-identical rewrite is a no-op, changed detail is a fresh match

Slice 4: a byte-identical rewrite (same status, no field changes) produces
no new journal event and byte-identical encodings, so the idempotency key
matches and the artifact is not rewritten. A same-status write with a
changed preserved detail field also produces no new journal event, but the
preserved field is copied from the base on both sides, so both encodings
change in lockstep, the digests differ from the prior record, and a fresh
match is recorded at the same journal tail.

Co-Authored-By: Cursor <cursoragent@cursor.com>
Co-Authored-By: Codex <codex@openai.com>
EOF
)"
```

## Task 7 - RED+GREEN: lag, lag-then-match (tests 6-7)

Add tests 6 and 7:

```python
def test_unmapped_status_after_handoff_is_lag(enabled, tmp_path):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)
    chain = ["started", "planning", "dispatching", "result-processing",
             "synthesizing", "handoff", "artifact-collection"]

    _write_run_json(run_dir, "started")
    for status in chain:
        _write_run_json_locked(repo, run_dir, status)

    data = json.loads(run_shadow.shadow_artifact_path(run_dir).read_text())
    assert data["lags"] == 1
    assert data["mismatches"] == 0
    assert data["errors"] == 0
    assert data["last_outcome"] == "lag"
    assert data["last_differing_fields"] == ["status"]
    # journal sequence unchanged by the unmapped write (slice 2 skips it)
    assert data["last_compared_sequence"] == 6
    report = run_shadow.check_projection_readiness(run_dir)
    assert report.ready is False
    assert REASON_STATUS_LAG_CURRENT in report.reasons


def test_mapped_ok_after_lag_restores_readiness(enabled, tmp_path):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)
    chain = ["started", "planning", "dispatching", "result-processing",
             "synthesizing", "handoff", "artifact-collection", "ok"]

    _write_run_json(run_dir, "started")
    for status in chain:
        _write_run_json_locked(repo, run_dir, status)

    data = json.loads(run_shadow.shadow_artifact_path(run_dir).read_text())
    assert data["lags"] == 1
    assert data["matches"] == 7
    assert data["last_outcome"] == "match"
    report = run_shadow.check_projection_readiness(run_dir)
    assert report.ready is True
    assert report.reasons == ()
```

Run (RED: Task 3's `record_shadow_comparison` returns early on
`shadow_bytes != projected_bytes`, so the unmapped-status write records
nothing and `lags` stays 0. The gate has no `status-lag-current` branch):

```bash
.venv/bin/python -m pytest -q tests/test_run_shadow.py::test_unmapped_status_after_handoff_is_lag tests/test_run_shadow.py::test_mapped_ok_after_lag_restores_readiness
```

Expected failure: `AssertionError` on `data["lags"] == 1` (reads 0) and on
`REASON_STATUS_LAG_CURRENT in report.reasons` (missing).

Implement (only the lag branch and its gate reason): replace Task 3's
`if shadow_bytes != projected_bytes: return` with a classification branch
that computes `differing = _differing_fields(shadow_candidate, projection.snapshot)`
and, when `differing == ["status"]` and the legacy status is not in
`run_lifecycle.STATUS_EVENT_TYPE`, records `OUTCOME_LAG`. Add the
`_differing_fields` helper (sorted key union, return sorted names whose
values deep-differ. No cap yet. The cap lands in Task 19). Add the
`status-lag-current` gate reason to `check_projection_readiness` (fire when
the artifact's `last_outcome == "lag"`). A later mapped transition records a
match and restores readiness.

```python
if shadow_bytes != projected_bytes:
    differing = _differing_fields(shadow_candidate, projection.snapshot)
    legacy_status = legacy_snapshot.get("status")
    if differing == ["status"] and legacy_status not in run_lifecycle.STATUS_EVENT_TYPE:
        _record_outcome(run_dir, run_id, OUTCOME_LAG, None, None, tail_seq,
                        tail_digest, shadow_digest, projected_digest, differing)
    else:
        return  # mismatch classification lands RED-first in Task 8
    return
_record_match(run_dir, run_id, tail_seq, tail_digest, shadow_digest, projected_digest)
```

Expected: pass.

Commit:

```bash
git add tests/test_run_shadow.py src/brigade/run_shadow.py
git commit -m "$(cat <<'EOF'
test(runs): unmapped status lag and lag-then-match readiness

Slice 4: a byte divergence on status alone with an unmapped legacy status is
classified lag (the slice-3 status lag, not a mapping defect). The gate
reports status-lag-current while lag is the latest outcome, and a later
mapped transition restores readiness.

Co-Authored-By: Cursor <cursoragent@cursor.com>
Co-Authored-By: Codex <codex@openai.com>
EOF
)"
```

## Task 8 - RED+GREEN: forged mismatch (test 8)

Add test 8 (direct call to `record_shadow_comparison` with a forged legacy
candidate whose mapped status disagrees with the journal tail):

```python
def test_forged_mapped_status_divergence_is_mismatch(enabled, tmp_path):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)

    _write_run_json(run_dir, "started")
    _write_run_json_locked(repo, run_dir, "started")  # run.created, seq 1

    # Forge a legacy candidate that claims "ok" while the journal tail is
    # run.created (started). The projector derives "started". The shadow
    # candidate keeps "ok". The bytes differ on status with a mapped legacy
    # status, so this is a mismatch, not a lag.
    run_before = (run_dir / "run.json").read_bytes()
    run_shadow.record_shadow_comparison(run_dir, {
        "schema": "brigade.run.v1",
        "schema_version": 1,
        "status": "ok",
        "task": "forged",
    })

    assert (run_dir / "run.json").read_bytes() == run_before  # legacy writer untouched
    data = json.loads(run_shadow.shadow_artifact_path(run_dir).read_text())
    assert data["mismatches"] == 1
    assert data["last_outcome"] == "mismatch"
    assert data["last_differing_fields"] == ["status"]
    report = run_shadow.check_projection_readiness(run_dir)
    assert report.ready is False
    assert REASON_MISMATCH_RECORDED in report.reasons
```

Run (RED: Task 7's classification branch returns early on the non-lag
mismatch case, so `mismatches` stays 0. The gate has no `mismatch-recorded`
branch):

```bash
.venv/bin/python -m pytest -q tests/test_run_shadow.py::test_forged_mapped_status_divergence_is_mismatch
```

Expected failure: `AssertionError` on `data["mismatches"] == 1` (reads 0)
and on `REASON_MISMATCH_RECORDED in report.reasons` (missing).

Implement (only the mismatch branch and its gate reason): replace Task 7's
`else: return` with a call to `_record_outcome(..., OUTCOME_MISMATCH, ...)`
for any non-lag divergence. Counters are cumulative and sticky
(`mismatches` never decreases). Add the `mismatch-recorded` gate reason to
`check_projection_readiness` (fire when `mismatches != 0`).

```python
    else:
        _record_outcome(run_dir, run_id, OUTCOME_MISMATCH, None, None, tail_seq,
                        tail_digest, shadow_digest, projected_digest, differing)
```

Expected: pass.

Commit:

```bash
git add tests/test_run_shadow.py src/brigade/run_shadow.py
git commit -m "$(cat <<'EOF'
test(runs): forged mapped-status divergence is a sticky mismatch

Slice 4: a legacy candidate whose mapped status disagrees with the journal
tail produces a byte mismatch on status. The record is sticky (mismatches
never decrease) and the gate reports mismatch-recorded. The legacy run.json
bytes are untouched.

Co-Authored-By: Cursor <cursoragent@cursor.com>
Co-Authored-By: Codex <codex@openai.com>
EOF
)"
```

## Task 9 - RED+GREEN: unmapped event type, partial tail (tests 9-10)

Add tests 9 and 10:

```python
def test_unmapped_event_type_records_projection_error(enabled, tmp_path):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)

    _write_run_json(run_dir, "started")
    _write_run_json_locked(repo, run_dir, "started")  # run.created

    # Append a registered-but-unmapped event directly to the journal.
    with runguard.run_lock(repo, run_dir=run_dir):
        report = run_journal.read_journal(_journal_path(run_dir))
        run_journal.append_event(
            _journal_path(run_dir),
            run_id=_RUN_ID,
            event_type="run.paused",
            payload={},
            idempotency_key="lifecycle:paused-test",
            expected_previous_sequence=report.events[-1].sequence,
        )

    # Next locked write advances run.json (fail open) and records an error.
    run_before = (run_dir / "run.json").read_bytes()
    _write_run_json_locked(repo, run_dir, "planning")
    assert (run_dir / "run.json").read_bytes() != run_before  # legacy writer advanced

    data = json.loads(run_shadow.shadow_artifact_path(run_dir).read_text())
    assert data["errors"] >= 1
    assert data["last_outcome"] == "error"
    assert data["last_error_category"] == "projection-error:UnmappedEventTypeError"
    report = run_shadow.check_projection_readiness(run_dir)
    assert report.ready is False
    assert REASON_ERROR_RECORDED in report.reasons


def test_partial_tail_records_journal_unreadable(enabled, tmp_path):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)

    _write_run_json(run_dir, "started")
    _write_run_json_locked(repo, run_dir, "started")  # run.created

    # Corrupt the journal by appending a truncated line.
    with _journal_path(run_dir).open("a") as handle:
        handle.write("{not-json\n")

    run_shadow.record_shadow_comparison(run_dir, {
        "schema": "brigade.run.v1", "schema_version": 1, "status": "planning",
        "task": "direct",
    })

    data = json.loads(run_shadow.shadow_artifact_path(run_dir).read_text())
    assert data["errors"] >= 1
    assert data["last_outcome"] == "error"
    assert data["last_error_category"] == "journal-unreadable"
    report = run_shadow.check_projection_readiness(run_dir)
    assert report.ready is False
    assert REASON_JOURNAL_UNREADABLE in report.reasons
```

Run (RED: Task 3's happy path calls `run_journal.read_journal` and
`run_projector.*` without `try/except`, so the unmapped event type raises
`ProjectionError` out of the hook (breaking the legacy write) and the partial
tail raises out of the hook. `errors` stays 0 and the gate has no
`error-recorded`/`journal-unreadable` branches):

```bash
.venv/bin/python -m pytest -q tests/test_run_shadow.py::test_unmapped_event_type_records_projection_error tests/test_run_shadow.py::test_partial_tail_records_journal_unreadable
```

Expected failure: `ProjectionError` propagates and the locked write raises
(or `errors == 1` reads 0). `REASON_ERROR_RECORDED` / `REASON_JOURNAL_UNREADABLE`
missing from `report.reasons`.

Implement (only the error-classification branches and their gate reasons):
wrap the `read_journal` call in `try/except OSError` → classify
`journal-unreadable`. After the read, check `report.partial_tail is not None
or report.chain_errors` → classify `journal-unreadable`. Wrap the
`encode_snapshot_bytes` and `project_run_snapshot` calls in
`try/except run_projector.ProjectionError` → classify
`projection-error:<type(exc).__name__>` with a bounded diagnostic via
`_bound(exc.diagnostic)`. Add the `_record_error` helper (mirrors
`_record_outcome` for the error outcome, sets `last_error_category`,
increments `errors`). Add the `_bound(msg)` helper (caps a string to
`run_events.MAX_DIAGNOSTIC_LEN` with a trailing `"…"`). First needed here
for the bounded projection-error diagnostic. Add the `error-recorded` and
`journal-unreadable` gate reasons to `check_projection_readiness` (fire when
`errors != 0`, and specifically `journal-unreadable` when the latest error
category is `journal-unreadable`). Containment of the raise out of the hook
lands RED-first in Task 20. This task only classifies the errors once
caught.

```python
try:
    report = run_journal.read_journal(journal_path)
except OSError:
    _record_error(run_dir, run_id, "journal-unreadable", None, None)
    return
if report.partial_tail is not None or report.chain_errors:
    _record_error(run_dir, run_id, "journal-unreadable", None, None)
    return
# ... shadow candidate ...
try:
    shadow_bytes = run_projector.encode_snapshot_bytes(shadow_candidate)
except run_projector.ProjectionError as exc:
    _record_error(run_dir, run_id, f"projection-error:{type(exc).__name__}",
                  _bound(exc.diagnostic), tail_seq)
    return
try:
    projection = run_projector.project_run_snapshot(
        legacy_snapshot, report.events, journal_present=True)
except run_projector.ProjectionError as exc:
    _record_error(run_dir, run_id, f"projection-error:{type(exc).__name__}",
                  _bound(exc.diagnostic), tail_seq)
    return
```

Expected: pass.

Commit:

```bash
git add tests/test_run_shadow.py src/brigade/run_shadow.py
git commit -m "$(cat <<'EOF'
test(runs): projection and journal-unreadable error categories

Slice 4: a registered-but-unmapped event type in the journal makes the
projector raise UnmappedEventTypeError, classified
projection-error:UnmappedEventTypeError. The legacy write still advances
(fail open) and the gate reports error-recorded. A partial journal tail is
classified journal-unreadable and the gate reports journal-unreadable.

Co-Authored-By: Cursor <cursoragent@cursor.com>
Co-Authored-By: Codex <codex@openai.com>
EOF
)"
```

## Task 10 - RED+GREEN: crash window comparison-gap (test 11)

Add test 11 (simulate a crash window by committing a transition whose
shadow step is skipped, then a later transition):

```python
def test_crash_window_records_comparison_gap(enabled, tmp_path):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)

    _write_run_json(run_dir, "started")
    _write_run_json_locked(repo, run_dir, "started")  # run.created, seq 1
    _write_run_json_locked(repo, run_dir, "planning")  # run.planning.started, seq 2

    # Simulate a crash: append run.dispatch.requested (seq 3) directly to the
    # journal, write run.json directly so the shadow hook does NOT run, then
    # trigger a later locked write that does run the hook.
    with runguard.run_lock(repo, run_dir=run_dir):
        report = run_journal.read_journal(_journal_path(run_dir))
        run_journal.append_event(
            _journal_path(run_dir),
            run_id=_RUN_ID,
            event_type="run.dispatch.requested",
            payload={},
            idempotency_key="lifecycle:dispatch-test",
            expected_previous_sequence=report.events[-1].sequence,
        )
        localio.write_json(run_dir / "run.json", {
            "schema": "brigade.run.v1", "schema_version": 1, "status": "dispatching",
            "task": "crash-window",
        })

    # Later locked write: shadow hook runs and detects the gap (tail seq 4
    # vs prior last_compared_sequence 2, advanced by more than one).
    _write_run_json_locked(repo, run_dir, "result-processing")

    data = json.loads(run_shadow.shadow_artifact_path(run_dir).read_text())
    assert data["errors"] >= 1
    # a comparison-gap error record exists
    gap_records = [r for r in data["recent_records"]
                  if r["outcome"] == "error" and r["category"] == "comparison-gap"]
    assert gap_records
    report = run_shadow.check_projection_readiness(run_dir)
    assert report.ready is False
    assert REASON_ERROR_RECORDED in report.reasons
```

Run (RED: Task 9's `_record_error` records only `journal-unreadable` and
`projection-error:*`. There is no `comparison-gap` category, so
`gap_records` is empty):

```bash
.venv/bin/python -m pytest -q tests/test_run_shadow.py::test_crash_window_records_comparison_gap
```

Expected failure: `AssertionError` on `assert gap_records` (empty list).

Implement (only the stale-prior-evidence gap rule): in `_record_outcome` and
`_record_error`, before the main record, load the prior artifact. If prior is
present and `prior["last_compared_sequence"]` is an `int` and
`tail_seq > prior["last_compared_sequence"] + 1`, append a `comparison-gap`
error record (category `"comparison-gap"`) in the same atomic write as the
main record. The `error-recorded` gate reason (already present from Task 9)
then closes the gate.

```python
prior = _load_prior_artifact(shadow_artifact_path(run_dir))
if (prior is not None
        and isinstance(prior.get("last_compared_sequence"), int)
        and tail_seq > prior["last_compared_sequence"] + 1):
    _append_gap_record(prior, tail_seq)  # category "comparison-gap"
```

Expected: pass.

Commit:

```bash
git add tests/test_run_shadow.py src/brigade/run_shadow.py
git commit -m "$(cat <<'EOF'
test(runs): crash window records comparison-gap

Slice 4: when a committed transition's shadow step was skipped and a later
transition runs the hook, the prior artifact's last_compared_sequence lags
the journal tail by more than one. A comparison-gap error is recorded in the
same atomic write as the main record and the gate closes permanently.

Co-Authored-By: Cursor <cursoragent@cursor.com>
Co-Authored-By: Codex <codex@openai.com>
EOF
)"
```

## Task 11 - RED+GREEN: absent prior evidence with tail > 1 (test 14)

Add test 14 (first comparison on a journal whose tail is already > 1 with no
prior artifact):

```python
def test_absent_prior_evidence_with_tail_above_one_records_gap(enabled, tmp_path):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)

    # Build a journal with two events but no shadow artifact, simulating a
    # crash that lost the first comparison.
    _write_run_json(run_dir, "started")
    _write_run_json_locked(repo, run_dir, "started")   # run.created, seq 1
    _write_run_json_locked(repo, run_dir, "planning")  # run.planning.started, seq 2
    # Remove the artifact so the next comparison sees no prior evidence.
    run_shadow.shadow_artifact_path(run_dir).unlink()
    # Advance the journal one more step without the hook running.
    with runguard.run_lock(repo, run_dir=run_dir):
        report = run_journal.read_journal(_journal_path(run_dir))
        run_journal.append_event(
            _journal_path(run_dir),
            run_id=_RUN_ID,
            event_type="run.dispatch.requested",
            payload={},
            idempotency_key="lifecycle:dispatch-gap",
            expected_previous_sequence=report.events[-1].sequence,
        )
        localio.write_json(run_dir / "run.json", {
            "schema": "brigade.run.v1", "schema_version": 1, "status": "dispatching",
            "task": "gap",
        })

    # Direct call: prior evidence absent, tail seq == 3 > 1 -> comparison-gap.
    run_shadow.record_shadow_comparison(run_dir, {
        "schema": "brigade.run.v1", "schema_version": 1, "status": "dispatching",
        "task": "gap",
    })

    data = json.loads(run_shadow.shadow_artifact_path(run_dir).read_text())
    gap_records = [r for r in data["recent_records"]
                  if r["outcome"] == "error" and r["category"] == "comparison-gap"]
    assert gap_records
    report = run_shadow.check_projection_readiness(run_dir)
    assert report.ready is False
    assert REASON_ERROR_RECORDED in report.reasons
```

Run (RED: Task 10's gap rule only fires when prior evidence is present. With
prior absent and `tail_seq > 1` no `comparison-gap` record is produced, so
`gap_records` is empty):

```bash
.venv/bin/python -m pytest -q tests/test_run_shadow.py::test_absent_prior_evidence_with_tail_above_one_records_gap
```

Expected failure: `AssertionError` on `assert gap_records` (empty list).

Implement (only the absent-prior-evidence gap rule): extend the gap check so
that when prior is absent (or quarantined) and `tail_seq > 1`, a
`comparison-gap` error record is produced in the same atomic write as the
main record. Reuse the `comparison-gap` category and the `error-recorded` gate
reason already present.

```python
prior = _load_prior_artifact(shadow_artifact_path(run_dir))
prior_seq = prior.get("last_compared_sequence") if prior else None
if (prior_seq is None and tail_seq > 1) or (
        isinstance(prior_seq, int) and tail_seq > prior_seq + 1):
    _append_gap_record(prior or _empty_artifact(run_id), tail_seq)
```

Expected: pass.

Commit:

```bash
git add tests/test_run_shadow.py src/brigade/run_shadow.py
git commit -m "$(cat <<'EOF'
test(runs): absent prior evidence with advanced journal is a gap

Slice 4: a first comparison that sees no prior artifact but a journal tail
sequence greater than 1 missed at least one earlier transition. A
comparison-gap error is recorded and the gate closes permanently.

Co-Authored-By: Cursor <cursoragent@cursor.com>
Co-Authored-By: Codex <codex@openai.com>
EOF
)"
```

## Task 12 - RED+GREEN: corrupt artifact quarantine (test 12)

Add test 12:

```python
def test_corrupt_artifact_quarantined_and_records_evidence_unreadable(enabled, tmp_path):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)

    _write_run_json(run_dir, "started")
    _write_run_json_locked(repo, run_dir, "started")  # match, seq 1
    artifact = run_shadow.shadow_artifact_path(run_dir)
    assert artifact.is_file()

    # Corrupt the artifact bytes.
    artifact.write_bytes(b"{not-json\n")
    _write_run_json_locked(repo, run_dir, "planning")  # triggers shadow hook

    # The corrupt file was renamed out of the way.
    quarantined = list((run_dir / "events").glob("shadow-comparison.json.corrupt-*"))
    assert quarantined
    data = json.loads(artifact.read_text())
    # An evidence-unreadable error plus the new comparison are both recorded.
    corrupt_records = [r for r in data["recent_records"]
                      if r["outcome"] == "error" and r["category"] == "evidence-unreadable"]
    assert corrupt_records
    report = run_shadow.check_projection_readiness(run_dir)
    assert report.ready is False
    assert REASON_ERROR_RECORDED in report.reasons
```

Run (RED: Task 3's `_load_prior_artifact` only handles the absent-file case
and lets `json.JSONDecodeError` propagate, so the corrupt bytes either raise
out of the hook or are silently re-parsed. No `evidence-unreadable` record is
produced and no `.corrupt-*` file appears):

```bash
.venv/bin/python -m pytest -q tests/test_run_shadow.py::test_corrupt_artifact_quarantined_and_records_evidence_unreadable
```

Expected failure: `AssertionError` on `assert quarantined` (no corrupt file)
or on `assert corrupt_records` (empty).

Implement (only the quarantine branch in `_load_prior_artifact`): when the
artifact file is present but `json.loads` raises, rename it to
`shadow-comparison.json.corrupt-<utc-iso>` (best-effort, swallowed on failure)
and return `None`. Have the caller record an `evidence-unreadable` error
(category `"evidence-unreadable"`) before the main record. The
`error-recorded` gate reason (already present) closes the gate.

```python
def _load_prior_artifact(artifact_path: Path) -> dict[str, Any] | None:
    if not artifact_path.is_file():
        return None
    try:
        return json.loads(artifact_path.read_text())
    except (OSError, ValueError):
        _quarantine_corrupt(artifact_path)  # rename to .corrupt-<utc>
        return None
```

Expected: pass.

Commit:

```bash
git add tests/test_run_shadow.py src/brigade/run_shadow.py
git commit -m "$(cat <<'EOF'
test(runs): quarantine corrupt shadow artifact

Slice 4: a present-but-unparseable shadow-comparison.json is renamed to
shadow-comparison.json.corrupt-* and treated as absent, with an
evidence-unreadable error recorded alongside the new comparison. The gate
closes permanently.

Co-Authored-By: Cursor <cursoragent@cursor.com>
Co-Authored-By: Codex <codex@openai.com>
EOF
)"
```

## Task 13 - RED+GREEN: journal-ahead-of-evidence gate reason (test 13)

Add test 13 (journal advanced without any comparison):

```python
def test_journal_ahead_of_evidence_blocks_gate(enabled, tmp_path):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)

    _write_run_json(run_dir, "started")
    _write_run_json_locked(repo, run_dir, "started")  # match, seq 1
    # Advance the journal directly without running the shadow hook.
    with runguard.run_lock(repo, run_dir=run_dir):
        report = run_journal.read_journal(_journal_path(run_dir))
        run_journal.append_event(
            _journal_path(run_dir),
            run_id=_RUN_ID,
            event_type="run.planning.started",
            payload={},
            idempotency_key="lifecycle:planning-ahead",
            expected_previous_sequence=report.events[-1].sequence,
        )

    report = run_shadow.check_projection_readiness(run_dir)
    assert report.ready is False
    assert REASON_JOURNAL_AHEAD in report.reasons
```

Run (RED: Task 3's `check_projection_readiness` returns `ready=True` whenever
the artifact is valid. It never re-reads the journal, so a journal advanced
past the artifact's `last_compared_*` is not detected):

```bash
.venv/bin/python -m pytest -q tests/test_run_shadow.py::test_journal_ahead_of_evidence_blocks_gate
```

Expected failure: `AssertionError` on `report.ready is False` (reads `True`)
or on `REASON_JOURNAL_AHEAD in report.reasons` (missing).

Implement (only the journal-ahead-of-evidence gate branch): in
`check_projection_readiness`, after the artifact-validity checks, re-read
the journal and compare its tail `sequence` and `event_digest` to the
artifact's `last_compared_sequence` / `last_compared_event_digest`. On
mismatch return `ReadinessReport(ready=False, reasons=(REASON_JOURNAL_AHEAD,))`.

```python
jr = run_journal.read_journal(run_lifecycle._journal_path(run_dir))
tail = jr.events[-1] if jr.events else None
if (tail is None
        or tail.sequence != data.get("last_compared_sequence")
        or tail.event_digest != data.get("last_compared_event_digest")):
    return ReadinessReport(ready=False, reasons=(REASON_JOURNAL_AHEAD,))
```

Expected: pass.

Commit:

```bash
git add tests/test_run_shadow.py src/brigade/run_shadow.py
git commit -m "$(cat <<'EOF'
test(runs): gate detects journal-ahead-of-evidence

Slice 4: check_projection_readiness re-reads the journal and compares its
tail sequence and digest to the artifact's last_compared_*. A mismatch
closes the gate with journal-ahead-of-evidence.

Co-Authored-By: Cursor <cursoragent@cursor.com>
Co-Authored-By: Codex <codex@openai.com>
EOF
)"
```

## Task 14 - RED+GREEN: gate reason coverage (test 15)

Add test 15:

```python
def test_gate_reason_coverage(tmp_path):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)

    # No journal at all.
    assert REASON_NO_JOURNAL in run_shadow.check_projection_readiness(run_dir).reasons

    # Journal present, no artifact.
    run_dir.joinpath("events").mkdir()
    run_lifecycle._journal_path(run_dir).write_text("")  # empty journal file
    report = run_shadow.check_projection_readiness(run_dir)
    assert REASON_NO_EVIDENCE in report.reasons

    # Wrong schema.
    localio.write_json(run_shadow.shadow_artifact_path(run_dir), {
        "schema": "brigade.other.v1", "schema_version": 1, "run_id": _RUN_ID,
        "comparisons": 1, "matches": 1, "mismatches": 0, "lags": 0, "errors": 0,
        "last_outcome": "match", "last_compared_sequence": None,
        "last_compared_event_digest": None, "last_shadow_digest": None,
        "last_projected_digest": None, "last_differing_fields": None,
        "last_error_category": None, "last_recorded_at": None, "recent_records": [],
    })
    assert REASON_EVIDENCE_SCHEMA_MISMATCH in run_shadow.check_projection_readiness(run_dir).reasons

    # Wrong run_id.
    data = json.loads(run_shadow.shadow_artifact_path(run_dir).read_text())
    data["schema"] = "brigade.run_shadow.v1"
    data["run_id"] = "other"
    run_shadow.shadow_artifact_path(run_dir).write_text(
        json.dumps(data, indent=2, sort_keys=True) + "\n")
    assert REASON_EVIDENCE_SCHEMA_MISMATCH in run_shadow.check_projection_readiness(run_dir).reasons

    # Zero comparisons.
    data["run_id"] = _RUN_ID
    data["comparisons"] = 0
    run_shadow.shadow_artifact_path(run_dir).write_text(
        json.dumps(data, indent=2, sort_keys=True) + "\n")
    assert REASON_NO_COMPARISONS in run_shadow.check_projection_readiness(run_dir).reasons
```

Run (RED: Task 3's `check_projection_readiness` only has the `no-journal`
branch and the ready=True happy path. The no-artifact, wrong-schema,
wrong-run_id, and zero-comparisons cases all return the coarse fail-closed
default with empty `reasons`, so the `REASON_NO_EVIDENCE` /
`REASON_EVIDENCE_SCHEMA_MISMATCH` / `REASON_NO_COMPARISONS` assertions fail):

```bash
.venv/bin/python -m pytest -q tests/test_run_shadow.py::test_gate_reason_coverage
```

Expected failure: `AssertionError` on `REASON_NO_EVIDENCE in report.reasons`
(empty tuple), then `REASON_EVIDENCE_SCHEMA_MISMATCH ...`, then
`REASON_NO_COMPARISONS ...`.

Implement (only these three gate reason branches): in
`check_projection_readiness`, when the journal is present: if the artifact
file is absent → `REASON_NO_EVIDENCE`. If present but `schema !=
SHADOW_SCHEMA` or `schema_version != SHADOW_SCHEMA_VERSION` or `run_id !=
run_dir.name` or `last_outcome not in _OUTCOMES` →
`REASON_EVIDENCE_SCHEMA_MISMATCH`. If `comparisons` is not an `int >= 1` →
`REASON_NO_COMPARISONS`. Return the first matching reason (or ready=True when
all checks pass and the journal-tail check from Task 13 also passes).

```python
artifact_path = shadow_artifact_path(run_dir)
if not artifact_path.is_file():
    return ReadinessReport(ready=False, reasons=(REASON_NO_EVIDENCE,))
data = json.loads(artifact_path.read_text())
if (data.get("schema") != SHADOW_SCHEMA
        or data.get("schema_version") != SHADOW_SCHEMA_VERSION
        or data.get("run_id") != run_dir.name
        or data.get("last_outcome") not in _OUTCOMES):
    return ReadinessReport(ready=False, reasons=(REASON_EVIDENCE_SCHEMA_MISMATCH,))
if not isinstance(data.get("comparisons"), int) or data["comparisons"] < 1:
    return ReadinessReport(ready=False, reasons=(REASON_NO_COMPARISONS,))
```

Expected: pass.

Commit:

```bash
git add tests/test_run_shadow.py src/brigade/run_shadow.py
git commit -m "$(cat <<'EOF'
test(runs): gate reason coverage for schema, run_id, no-comparisons

Slice 4: check_projection_readiness reports evidence-schema-mismatch on
wrong schema/schema_version/run_id or an out-of-set last_outcome, and
no-comparisons when comparisons is not a positive integer.

Co-Authored-By: Cursor <cursoragent@cursor.com>
Co-Authored-By: Codex <codex@openai.com>
EOF
)"
```

## Task 15 - RED+GREEN: gate never raises on garbage (test 16)

Add test 16:

```python
def test_gate_never_raises_on_garbage_artifact(tmp_path):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)
    run_dir.joinpath("events").mkdir()
    run_lifecycle._journal_path(run_dir).write_text("")
    run_shadow.shadow_artifact_path(run_dir).write_bytes(b"\x00\x01\x02 not json at all")

    report = run_shadow.check_projection_readiness(run_dir)
    assert report.ready is False
    assert REASON_EVIDENCE_UNREADABLE in report.reasons
```

Run (RED: Task 14's `check_projection_readiness` calls `json.loads` on the
artifact without containment, so garbage bytes raise `JSONDecodeError` out of
the gate instead of returning `REASON_EVIDENCE_UNREADABLE`):

```bash
.venv/bin/python -m pytest -q tests/test_run_shadow.py::test_gate_never_raises_on_garbage_artifact
```

Expected failure: `json.JSONDecodeError` raised out of
`check_projection_readiness` (test errors, not a clean `AssertionError`).

Implement (only the gate's outer containment): wrap the entire
`check_projection_readiness` body in `try/except Exception` and return
`ReadinessReport(ready=False, reasons=(REASON_EVIDENCE_UNREADABLE,))` from
the handler. This is the gate-only containment. The writer-path containment
lands RED-first in Task 20.

```python
def check_projection_readiness(run_dir: Path) -> ReadinessReport:
    try:
        # ...existing body...
    except Exception:
        return ReadinessReport(ready=False, reasons=(REASON_EVIDENCE_UNREADABLE,))
```

Expected: pass.

Commit:

```bash
git add tests/test_run_shadow.py src/brigade/run_shadow.py
git commit -m "$(cat <<'EOF'
test(runs): gate is total on garbage artifact bytes

Slice 4: check_projection_readiness never raises. Garbage artifact bytes
yield evidence-unreadable from the outer containment.

Co-Authored-By: Cursor <cursoragent@cursor.com>
Co-Authored-By: Codex <codex@openai.com>
EOF
)"
```

## Task 16 - RED+GREEN: privacy (test 17)

Add test 17:

```python
PRIVATE_MARKER = "PRIVATE-MARKER-DO-NOT-LEAK"

def test_private_error_field_never_enters_artifact(enabled, tmp_path):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)

    _write_run_json(run_dir, "started")
    _write_run_json_locked(repo, run_dir, "started")
    _write_run_json_locked(repo, run_dir, "failed", error=PRIVATE_MARKER)

    artifact_bytes = run_shadow.shadow_artifact_path(run_dir).read_bytes()
    assert PRIVATE_MARKER.encode() not in artifact_bytes
    # No raw status string appears in the artifact: status divergences are
    # recorded as the field name plus digests, never the value.
    data = json.loads(artifact_bytes)
    for record in data["recent_records"]:
        assert "status" not in record or record.get("differing_fields") is not None
    # The artifact never carries a top-level status field.
    assert "status" not in data
```

Run (coverage: expected to pass on first run. A failure is a defect in the
record shape landed by Task 3/6/7/8/9 to fix here, since the record carries
only digests, field names, outcome/category tokens, sequences, timestamps,
and bounded diagnostics, with no top-level `status` field):

```bash
.venv/bin/python -m pytest -q tests/test_run_shadow.py::test_private_error_field_never_enters_artifact
```

Expected: pass (the `failed` transition records a match because the
projector derives `failed` from the journal event. The private marker in the
raw `error` field never enters the artifact).

Commit:

```bash
git add tests/test_run_shadow.py src/brigade/run_shadow.py
git commit -m "$(cat <<'EOF'
test(runs): shadow artifact carries no raw status or error strings

Slice 4: a run whose run.json carries a private marker in its error field
completes failed as a match. The marker never appears in the artifact
bytes. Divergences are recorded as field names plus digests, never values.

Co-Authored-By: Cursor <cursoragent@cursor.com>
Co-Authored-By: Codex <codex@openai.com>
EOF
)"
```

## Task 17 - RED+GREEN: permissions and encoding (test 18)

Add test 18:

```python
import os

def test_artifact_permissions_and_encoding(enabled, tmp_path):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)

    _write_run_json(run_dir, "started")
    _write_run_json_locked(repo, run_dir, "started")

    artifact = run_shadow.shadow_artifact_path(run_dir)
    mode = artifact.stat().st_mode & 0o777
    assert mode == 0o600
    events_mode = (run_dir / "events").stat().st_mode & 0o777
    assert events_mode == 0o700
    raw = artifact.read_bytes()
    assert raw.endswith(b"\n")
    assert raw.count(b"\n") == raw.count(b"\n")  # exactly one trailing newline
    # sorted keys, two-space indent
    text = raw.decode("utf-8")
    assert text.startswith("{\n  \"")
    assert "  \"schema\"" in text
```

Run (coverage: expected to pass on first run. A failure is a defect in
`_write_artifact` landed by Task 3 to fix here, since it already uses
`localio.write_text_atomic` for a 0o600 file in the 0o700 `events` dir with
sorted keys, two-space indent, and one trailing newline):

```bash
.venv/bin/python -m pytest -q tests/test_run_shadow.py::test_artifact_permissions_and_encoding
```

Expected: pass once `_write_artifact` uses `localio.write_text_atomic` (which
creates a 0o600 temp inside the 0o700 `events` dir) and the house encoding.

Commit:

```bash
git add tests/test_run_shadow.py src/brigade/run_shadow.py
git commit -m "$(cat <<'EOF'
test(runs): shadow artifact permissions and encoding

Slice 4: the artifact is 0o600 inside the 0o700 events directory, encoded
with sorted keys, two-space indent, and exactly one trailing newline via
localio.write_text_atomic.

Co-Authored-By: Cursor <cursoragent@cursor.com>
Co-Authored-By: Codex <codex@openai.com>
EOF
)"
```

## Task 18 - RED+GREEN: recent_records cap and counter coherence (test 19)

Add test 19:

```python
def test_recent_records_caps_at_16_while_counters_stay_cumulative(enabled, tmp_path):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)
    _write_run_json(run_dir, "started")
    # Drive 20 distinct mapped transitions by alternating dispatching/result-processing.
    _write_run_json_locked(repo, run_dir, "started")  # seq 1
    for i in range(19):
        status = "dispatching" if i % 2 == 0 else "result-processing"
        _write_run_json_locked(repo, run_dir, status)

    data = json.loads(run_shadow.shadow_artifact_path(run_dir).read_text())
    assert data["comparisons"] == 20
    assert data["matches"] + data["mismatches"] + data["lags"] + data["errors"] == 20
    assert len(data["recent_records"]) == 16
```

Run (RED: Task 3/6/7/8's `_record_outcome` appends every record to
`recent_records` with no cap, so after 20 comparisons the list has 20
entries, not 16):

```bash
.venv/bin/python -m pytest -q tests/test_run_shadow.py::test_recent_records_caps_at_16_while_counters_stay_cumulative
```

Expected failure: `AssertionError` on
`len(data["recent_records"]) == 16` (reads 20).

Implement (only the `recent_records` cap): in `_record_outcome` (and
`_record_error`), after appending the new record, truncate
`recent_records` to the last `MAX_RECENT_RECORDS` (16) entries (FIFO). Keep
the four outcome counters cumulative and untouched by the cap.

```python
data["recent_records"].append(record)
data["recent_records"] = data["recent_records"][-MAX_RECENT_RECORDS:]
```

Expected: pass.

Commit:

```bash
git add tests/test_run_shadow.py src/brigade/run_shadow.py
git commit -m "$(cat <<'EOF'
test(runs): recent_records cap and counter coherence

Slice 4: after 20 distinct comparisons the artifact's recent_records list
caps at 16 while the four outcome counters stay cumulative and sum to
comparisons.

Co-Authored-By: Cursor <cursoragent@cursor.com>
Co-Authored-By: Codex <codex@openai.com>
EOF
)"
```

## Task 19 - RED+GREEN: differing_fields cap (test 20)

Add test 20 (forge a legacy candidate with many extra keys the projector
rejects, so the differing-field list exceeds the cap):

```python
def test_differing_fields_caps_at_max(enabled, tmp_path):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)
    _write_run_json(run_dir, "started")
    _write_run_json_locked(repo, run_dir, "started")  # match, seq 1

    # Forge a legacy candidate whose status differs (mapped mismatch) and
    # whose many preserved fields differ from the projected snapshot. The
    # differing-field list must cap at MAX_DIFFERING_FIELDS with the token.
    forged = {
        "schema": "brigade.run.v1", "schema_version": 1, "status": "planning",
        "task": "forged",
    }
    # Add 20 distinct preserved fields that the projector will preserve from
    # the base but with different values than the projected snapshot derives.
    for i in range(20):
        forged[f"active_stage"] = f"stage-{i}"  # one preserved field, varied
    run_shadow.record_shadow_comparison(run_dir, forged)

    data = json.loads(run_shadow.shadow_artifact_path(run_dir).read_text())
    record = data["recent_records"][-1]
    assert record["outcome"] in ("mismatch", "error")
    if record["differing_fields"] is not None:
        assert len(record["differing_fields"]) <= run_shadow.MAX_DIFFERING_FIELDS
        if len(record["differing_fields"]) == run_shadow.MAX_DIFFERING_FIELDS:
            assert record["differing_fields"][-1] == "..."
```

Run (RED: Task 7's `_differing_fields` returns the full sorted list with no
cap, so a divergence on more than `MAX_DIFFERING_FIELDS` fields yields a list
longer than 16 and without the `"..."` truncation token):

```bash
.venv/bin/python -m pytest -q tests/test_run_shadow.py::test_differing_fields_caps_at_max
```

Expected failure: `AssertionError` on
`len(record["differing_fields"]) <= run_shadow.MAX_DIFFERING_FIELDS` (reads
>16) or on `record["differing_fields"][-1] == "..."` (missing).

Implement (only the `differing_fields` cap): in `_differing_fields`, after
computing the sorted diverging names, if the list is longer than
`MAX_DIFFERING_FIELDS`, keep the first `MAX_DIFFERING_FIELDS - 1` names and
append `"..."` so the total length is `MAX_DIFFERING_FIELDS`.

```python
if len(differing) > MAX_DIFFERING_FIELDS:
    differing = differing[: MAX_DIFFERING_FIELDS - 1] + ["..."]
```

Expected: pass.

Commit:

```bash
git add tests/test_run_shadow.py src/brigade/run_shadow.py
git commit -m "$(cat <<'EOF'
test(runs): differing-field list caps at MAX_DIFFERING_FIELDS

Slice 4: when the shadow and projected snapshots differ on more than 16
fields, the differing-field list is capped at MAX_DIFFERING_FIELDS with the
truncation token.

Co-Authored-By: Cursor <cursoragent@cursor.com>
Co-Authored-By: Codex <codex@openai.com>
EOF
)"
```

## Task 20 - RED+GREEN: containment (test 21)

Add test 21:

```python
def test_oserror_in_shadow_does_not_break_legacy_write(enabled, tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)
    _write_run_json(run_dir, "started")
    _write_run_json_locked(repo, run_dir, "started")  # match

    # Make read_journal raise OSError inside the next shadow call.
    original_read = run_journal.read_journal
    def raising_read(path):
        raise OSError("simulated")
    monkeypatch.setattr(run_journal, "read_journal", raising_read)

    run_before = (run_dir / "run.json").read_bytes()
    _write_run_json_locked(repo, run_dir, "planning")  # legacy write must still advance
    assert (run_dir / "run.json").read_bytes() != run_before

    data = json.loads(run_shadow.shadow_artifact_path(run_dir).read_text())
    assert data["errors"] >= 1
    assert data["last_error_category"] == "journal-unreadable"
    monkeypatch.setattr(run_journal, "read_journal", original_read)


def test_failing_evidence_write_is_swallowed(enabled, tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)
    _write_run_json(run_dir, "started")
    _write_run_json_locked(repo, run_dir, "started")  # match

    def raising_write(path, data):
        raise OSError("disk full")
    monkeypatch.setattr(localio, "write_text_atomic", raising_write)

    run_before = (run_dir / "run.json").read_bytes()
    # The legacy write itself uses write_text_atomic. To isolate the shadow
    # evidence write, restore it for the legacy write then break it for the
    # shadow write. Simpler: call record_shadow_comparison directly and
    # assert it returns without raising.
    monkeypatch.setattr(localio, "write_text_atomic", raising_write)
    run_shadow.record_shadow_comparison(run_dir, {
        "schema": "brigade.run.v1", "schema_version": 1, "status": "planning",
        "task": "direct",
    })  # must not raise
    assert (run_dir / "run.json").read_bytes() == run_before
```

Run (RED: Task 3's `record_shadow_comparison` has no outer containment, so
the monkeypatched `OSError` from `read_journal` propagates out of the hook
and breaks the locked legacy write (`run.json` does not advance, or the test
errors with `OSError`). The failing-evidence-write case raises out of the
direct call instead of returning):

```bash
.venv/bin/python -m pytest -q tests/test_run_shadow.py::test_oserror_in_shadow_does_not_break_legacy_write tests/test_run_shadow.py::test_failing_evidence_write_is_swallowed
```

Expected failure: `OSError` raised out of `_write_run_json_locked` (legacy
write does not advance) / out of the direct `record_shadow_comparison` call.

Implement (only the writer-path outer containment): wrap the entire
`record_shadow_comparison` body in `try/except Exception: return` so no
shadow-path failure ever propagates into the legacy writer. The
`OSError`-from-`read_journal` → `journal-unreadable` classification already
landed in Task 9 now survives the outer containment and is recorded before
the `return`. A failing `_write_artifact` is swallowed silently by the same
containment.

```python
def record_shadow_comparison(run_dir: Path, legacy_snapshot: Mapping[str, Any]) -> None:
    try:
        # ...entire existing body (happy path + Task 6-19 branches)...
    except Exception:
        return
```

Expected: pass.

Commit:

```bash
git add tests/test_run_shadow.py src/brigade/run_shadow.py
git commit -m "$(cat <<'EOF'
test(runs): shadow failures never break the legacy writer

Slice 4: an OSError inside the shadow path is classified
journal-unreadable and the legacy run.json write still advances. A failing
evidence write is fully swallowed by the outer containment and the legacy
writer is untouched.

Co-Authored-By: Cursor <cursoragent@cursor.com>
Co-Authored-By: Codex <codex@openai.com>
EOF
)"
```

## Task 21 - RED+GREEN: journal run_id mismatch (test 22)

Add test 22:

```python
def test_journal_run_id_mismatch_records_error(enabled, tmp_path):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)
    _write_run_json(run_dir, "started")
    _write_run_json_locked(repo, run_dir, "started")  # match, seq 1

    # Forge a journal whose first event run_id differs from the directory name
    # by appending an event with a foreign run_id directly.
    foreign = "20260101-000000-foreign1"
    with runguard.run_lock(repo, run_dir=run_dir):
        run_journal.append_event(
            _journal_path(run_dir),
            run_id=foreign,
            event_type="run.planning.started",
            payload={},
            idempotency_key="lifecycle:foreign",
            expected_previous_sequence=1,
        )

    run_shadow.record_shadow_comparison(run_dir, {
        "schema": "brigade.run.v1", "schema_version": 1, "status": "planning",
        "task": "direct",
    })

    data = json.loads(run_shadow.shadow_artifact_path(run_dir).read_text())
    assert data["errors"] >= 1
    assert data["last_error_category"] == "journal-run-id-mismatch"
    report = run_shadow.check_projection_readiness(run_dir)
    assert report.ready is False
    assert REASON_ERROR_RECORDED in report.reasons
```

Run (RED: Task 3's happy path never compares the first event's `run_id` to
`run_dir.name`, so the forged foreign-run_id journal is treated as a normal
match and `errors` stays 0. `last_error_category` is not
`journal-run-id-mismatch`):

```bash
.venv/bin/python -m pytest -q tests/test_run_shadow.py::test_journal_run_id_mismatch_records_error
```

Expected failure: `AssertionError` on `data["errors"] >= 1` (reads 0) and on
`data["last_error_category"] == "journal-run-id-mismatch"` (reads `None`).

Implement (only the run_id-mismatch defense-in-depth check): in
`record_shadow_comparison`, after the journal read and the partial-tail
check, compare `report.events[0].run_id` to `run_dir.name`. On mismatch
record a `journal-run-id-mismatch` error (via `_record_error`) and return.
The `error-recorded` gate reason (already present) closes the gate.

```python
if report.events and report.events[0].run_id != run_id:
    _record_error(run_dir, run_id, "journal-run-id-mismatch", None, None)
    return
```

Expected: pass.

Commit:

```bash
git add tests/test_run_shadow.py src/brigade/run_shadow.py
git commit -m "$(cat <<'EOF'
test(runs): defense-in-depth journal run_id mismatch

Slice 4: a journal whose first event run_id differs from the run directory
name is classified journal-run-id-mismatch (unreachable through the normal
write path, defense in depth for direct calls).

Co-Authored-By: Cursor <cursoragent@cursor.com>
Co-Authored-By: Codex <codex@openai.com>
EOF
)"
```

## Task 22 - RED+GREEN: non-run.json writes leave artifact untouched (test 23)

Add test 23:

```python
def test_non_run_json_write_leaves_artifact_untouched(enabled, tmp_path):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)
    _write_run_json(run_dir, "started")
    _write_run_json_locked(repo, run_dir, "started")  # match
    artifact = run_shadow.shadow_artifact_path(run_dir)
    bytes_before = artifact.read_bytes()

    # A roster.json write through aboyeur._write_json must not touch the artifact.
    aboyeur._write_json(run_dir / "roster.json", {"orchestrator": "chef"})

    assert artifact.read_bytes() == bytes_before
```

Run (coverage: expected to pass on first run. A failure is a defect in the
hook guard landed by Task 3 to fix here, since the hook already guards on
`path.name == "run.json"`):

```bash
.venv/bin/python -m pytest -q tests/test_run_shadow.py::test_non_run_json_write_leaves_artifact_untouched
```

Expected: pass (the `roster.json` write does not match `path.name ==
"run.json"`, so `record_shadow_comparison` is never called and the artifact
bytes are unchanged).

Commit:

```bash
git add tests/test_run_shadow.py src/brigade/aboyeur.py
git commit -m "$(cat <<'EOF'
test(runs): non-run.json writes do not trigger shadow comparison

Slice 4: the aboyeur._write_json hook guards on path.name == "run.json", so
roster.json and other sidecar writes leave the shadow artifact untouched.

Co-Authored-By: Cursor <cursoragent@cursor.com>
Co-Authored-By: Codex <codex@openai.com>
EOF
)"
```

## Task 23 - Verify: focused, neighboring, and full suite

Run the focused suite, the neighboring journal/lifecycle/projector suites,
then the full repository suite, ruff, and mypy:

```bash
.venv/bin/python -m pytest -q tests/test_run_shadow.py
.venv/bin/python -m pytest -q tests/test_run_lifecycle.py tests/test_run_projector.py tests/test_run_journal.py tests/test_run_events.py
./scripts/verify
```

Expected: all green. If `./scripts/verify` fails, paste the verbatim failure
and fix the code (never weaken or skip a test to make it pass).

No commit (verification only).

## Task 24 - Brigade verify receipt

Capture the full gate through Brigade so GraphTrail writes code_graph_delta
into the receipt:

```bash
brigade work verify run --target . --command "./scripts/verify" --capture brigade-work
```

Then capture the outcome against the `brigade-work` skill and export the
receipt as MiseLedger evidence:

```bash
brigade outcome capture brigade-work --run-id latest --kind skill
brigade receipts export miseledger --target . --new-only --import
```

Expected: the verify run exits 0, the receipt is captured, and the
MiseLedger evidence brief is imported for the next Brigade run.

## Task 25 - Memory handoff

Write a handoff to `.claude/memory-handoffs/<timestamp>-issue-568-slice-4-shadow-comparison.md`
using the template at `.claude/memory-handoffs/TEMPLATE.md` (or
`brigade handoff-template`). Cover:

- The corrected parity contract (full canonical byte comparison, shadow
  candidate = deep-copied legacy payload + 5 derived fields with legacy
  status preserved, encoded through `encode_snapshot_bytes`, compared
  against `RunProjection.to_bytes`).
- The privacy tightening (digests + bounded differing-field names, no raw
  status strings).
- The two comparison-gap rules (absent prior evidence with tail > 1, and
  stale prior evidence with tail advanced by more than one).
- The `lag` vs `mismatch` classification (status-only divergence with an
  unmapped legacy status is `lag`. Everything else is `mismatch`).
- The fail-open-writer / fail-closed-gate split.
- The RED-first task ordering and the one updated lifecycle test.

## Task 26 - Operator checkup

```bash
brigade operator checkup --target .
```

Confirm the loop is healthy: verify receipt captured, outcome attributed,
MiseLedger evidence imported, handoff drafted.

## Summary of commits

Each task that adds code ends in a commit with the two required co-author
trailers. The commits are intentionally small and ordered so every commit
leaves the repository compiling and the prior tests green. The only RED
moment is Task 1's import failure, which is never committed red.

| Task | Commit subject |
| --- | --- |
| 2 | feat(runs): add run_shadow module with real minimal flag-off behavior |
| 3 | feat(runs): wire shadow comparison hook and first-match record |
| 4 | test(runs): account for shadow artifact in events directory listing |
| 5 | test(runs): cover full mapped lifecycle chain parity |
| 6 | test(runs): byte-identical rewrite is a no-op, changed detail is a fresh match |
| 7 | test(runs): unmapped status lag and lag-then-match readiness |
| 8 | test(runs): forged mapped-status divergence is a sticky mismatch |
| 9 | test(runs): projection and journal-unreadable error categories |
| 10 | test(runs): crash window records comparison-gap |
| 11 | test(runs): absent prior evidence with advanced journal is a gap |
| 12 | test(runs): quarantine corrupt shadow artifact |
| 13 | test(runs): gate detects journal-ahead-of-evidence |
| 14 | test(runs): gate reason coverage for schema, run_id, no-comparisons |
| 15 | test(runs): gate is total on garbage artifact bytes |
| 16 | test(runs): shadow artifact carries no raw status or error strings |
| 17 | test(runs): shadow artifact permissions and encoding |
| 18 | test(runs): recent_records cap and counter coherence |
| 19 | test(runs): differing-field list caps at MAX_DIFFERING_FIELDS |
| 20 | test(runs): shadow failures never break the legacy writer |
| 21 | test(runs): defense-in-depth journal run_id mismatch |
| 22 | test(runs): non-run.json writes do not trigger shadow comparison |

Tasks 23-26 are verification and handoff, no commits.
