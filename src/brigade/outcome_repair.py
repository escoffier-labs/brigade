"""Operator-only repair for a broken completed outcome ledger digest chain.

Normal ledger writes remain append-only. This module is the exceptional
incident procedure when a completed ``records.jsonl`` fails chain validation:

1. require explicit operator confirmation;
2. diagnose the first break (line, expected/actual digests, suspected cause);
3. quarantine the original ledger write-once and preserve the invalid segment;
4. replace the active ledger with the valid prefix plus a neutral repair record;
5. re-verify the full chain before returning.

The quarantine is retained for audit. Standard library only.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

from brigade import localio, outcome as core, outcome_cmd

REPAIR_SCHEMA = "brigade.outcome_ledger_repair.v1"
REPAIR_SCHEMA_VERSION = 1
REPAIR_SOURCE = "ledger-repair"
REPAIR_ARTIFACT_ID = "outcome-ledger"


@dataclasses.dataclass(frozen=True)
class LedgerBreak:
    """First integrity failure in a completed outcome ledger."""

    path: Path
    kind: str
    line_no: int
    expected_prev: str | None
    actual_prev: str | None
    suspected_cause: str
    valid_prefix_bytes: bytes
    invalid_segment_bytes: bytes
    valid_prefix_lines: int
    invalid_segment_start: int
    invalid_segment_end: int
    last_valid_digest: str | None


@dataclasses.dataclass(frozen=True)
class LedgerRepairReport:
    """Paths and bounds for one ledger repair."""

    operation_id: str
    break_line: int
    kind: str
    suspected_cause: str
    quarantine_path: Path
    invalid_segment_path: Path
    record_path: Path
    valid_prefix_lines: int
    invalid_segment_start: int
    invalid_segment_end: int


def _payload_without_digests(row: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in row.items() if key not in {"digest", "prev_digest"}}


def _suspect_duplicate_writer(previous_row: dict[str, Any] | None, broken_row: dict[str, Any]) -> bool:
    if previous_row is None:
        return False
    return _payload_without_digests(previous_row) == _payload_without_digests(broken_row)


def _break(
    *,
    path: Path,
    kind: str,
    line_no: int,
    expected_prev: str | None,
    actual_prev: str | None,
    suspected_cause: str,
    raw: bytes,
    offset: int,
    end_line: int,
    last_valid_digest: str | None,
) -> LedgerBreak:
    return LedgerBreak(
        path=path,
        kind=kind,
        line_no=line_no,
        expected_prev=expected_prev,
        actual_prev=actual_prev,
        suspected_cause=suspected_cause,
        valid_prefix_bytes=raw[:offset],
        invalid_segment_bytes=raw[offset:],
        valid_prefix_lines=raw[:offset].count(b"\n"),
        invalid_segment_start=line_no,
        invalid_segment_end=end_line,
        last_valid_digest=last_valid_digest,
    )


def _diagnose_completed_lines(path: Path, raw: bytes) -> LedgerBreak | None:
    """Diagnose newline-terminated ledger bytes; return None when the chain is healthy."""
    previous_digest: str | None = None
    previous_row: dict[str, Any] | None = None
    offset = 0
    lines = raw.splitlines(keepends=True)
    end_line = len(lines)
    for line_no, line in enumerate(lines, start=1):
        line_bytes = line if line.endswith(b"\n") else line + b"\n"
        if not line.strip():
            return _break(
                path=path,
                kind="empty_line",
                line_no=line_no,
                expected_prev=previous_digest,
                actual_prev=None,
                suspected_cause="empty ledger line",
                raw=raw,
                offset=offset,
                end_line=end_line,
                last_valid_digest=previous_digest,
            )
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            return _break(
                path=path,
                kind="invalid_json",
                line_no=line_no,
                expected_prev=previous_digest,
                actual_prev=None,
                suspected_cause=f"invalid JSON ({exc})",
                raw=raw,
                offset=offset,
                end_line=end_line,
                last_valid_digest=previous_digest,
            )
        if not isinstance(row, dict):
            return _break(
                path=path,
                kind="invalid_json",
                line_no=line_no,
                expected_prev=previous_digest,
                actual_prev=None,
                suspected_cause="ledger line is not an object",
                raw=raw,
                offset=offset,
                end_line=end_line,
                last_valid_digest=previous_digest,
            )
        recorded_digest = row.get("digest")
        if not isinstance(recorded_digest, str) or not recorded_digest:
            previous_row = row
            offset += len(line_bytes)
            continue
        recomputed = localio.canonical_json_digest(row, exclude_keys={"digest"})
        if recomputed != recorded_digest:
            return _break(
                path=path,
                kind="digest_mismatch",
                line_no=line_no,
                expected_prev=previous_digest,
                actual_prev=str(row.get("prev_digest")) if "prev_digest" in row else None,
                suspected_cause="tampered or rewritten record",
                raw=raw,
                offset=offset,
                end_line=end_line,
                last_valid_digest=previous_digest,
            )
        if "prev_digest" not in row:
            previous_digest = recorded_digest
            previous_row = row
            offset += len(line_bytes)
            continue
        actual_prev = row.get("prev_digest")
        if actual_prev != previous_digest:
            suspected = (
                "duplicate-writer records"
                if _suspect_duplicate_writer(previous_row, row)
                else "digest chain discontinuity"
            )
            return _break(
                path=path,
                kind="digest_chain_break",
                line_no=line_no,
                expected_prev=previous_digest,
                actual_prev=None if actual_prev is None else str(actual_prev),
                suspected_cause=suspected,
                raw=raw,
                offset=offset,
                end_line=end_line,
                last_valid_digest=previous_digest,
            )
        previous_digest = recorded_digest
        previous_row = row
        offset += len(line_bytes)
    return None


def diagnose_completed_ledger(path: Path) -> LedgerBreak | None:
    """Return the first completed-ledger integrity break, or None when healthy."""
    if not path.is_file():
        return None
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise outcome_cmd.OutcomeLedgerError(f"could not read outcome ledger: {path}: {exc}") from exc
    if not raw:
        return None

    if raw.endswith(b"\n"):
        return _diagnose_completed_lines(path, raw)

    last_newline = raw.rfind(b"\n")
    if last_newline < 0:
        return _break(
            path=path,
            kind="incomplete_trailing",
            line_no=1,
            expected_prev=None,
            actual_prev=None,
            suspected_cause="incomplete trailing record",
            raw=raw,
            offset=0,
            end_line=1,
            last_valid_digest=None,
        )

    prefix = raw[: last_newline + 1]
    prefix_break = _diagnose_completed_lines(path, prefix)
    if prefix_break is not None:
        # Keep the incomplete tail inside the preserved invalid segment.
        return LedgerBreak(
            path=path,
            kind=prefix_break.kind,
            line_no=prefix_break.line_no,
            expected_prev=prefix_break.expected_prev,
            actual_prev=prefix_break.actual_prev,
            suspected_cause=prefix_break.suspected_cause,
            valid_prefix_bytes=prefix_break.valid_prefix_bytes,
            invalid_segment_bytes=raw[len(prefix_break.valid_prefix_bytes) :],
            valid_prefix_lines=prefix_break.valid_prefix_lines,
            invalid_segment_start=prefix_break.invalid_segment_start,
            invalid_segment_end=prefix.count(b"\n") + 1,
            last_valid_digest=prefix_break.last_valid_digest,
        )

    try:
        last_digest = outcome_cmd._validate_completed_ledger_bytes(prefix, path)
    except outcome_cmd.OutcomeLedgerError:
        last_digest = None
    start_line = prefix.count(b"\n") + 1
    return _break(
        path=path,
        kind="incomplete_trailing",
        line_no=start_line,
        expected_prev=last_digest,
        actual_prev=None,
        suspected_cause="incomplete trailing record",
        raw=raw,
        offset=len(prefix),
        end_line=start_line,
        last_valid_digest=last_digest,
    )


def format_ledger_corrupt_error(break_info: LedgerBreak) -> str:
    """Bounded, actionable error used by capture and repair preflight."""
    expected = break_info.expected_prev
    actual = break_info.actual_prev
    digest_tail = ""
    if break_info.kind == "digest_chain_break":
        digest_tail = f" (expected prev_digest={expected!r}, actual={actual!r})"
    return (
        f"ledger corrupt at line {break_info.line_no}: {break_info.kind}{digest_tail}; "
        f"suspected cause: {break_info.suspected_cause}; "
        f"run `brigade outcome repair --operator-confirm`"
    )


def _operation_id(original: bytes, break_info: LedgerBreak) -> str:
    material = {
        "sha256": hashlib.sha256(original).hexdigest(),
        "line_no": break_info.line_no,
        "kind": break_info.kind,
    }
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"repair-{hashlib.sha256(encoded).hexdigest()[:16]}"


def _repair_paths(target: Path, operation_id: str) -> tuple[Path, Path, Path, Path]:
    # Keep quarantine under gitignored .brigade/ so operators can retain the
    # original corrupted ledger for audit without committing it.
    operation_dir = target / ".brigade" / "outcome" / "repairs" / operation_id
    return (
        operation_dir,
        operation_dir / "original.jsonl",
        operation_dir / "invalid-segment.jsonl",
        operation_dir / "record.json",
    )


def _build_repair_record(break_info: LedgerBreak, *, operation_id: str, quarantine_path: Path) -> core.OutcomeRecord:
    return core.OutcomeRecord(
        artifact_id=REPAIR_ARTIFACT_ID,
        artifact_kind="skill",
        task_id=f"{operation_id}:lines-{break_info.invalid_segment_start}-{break_info.invalid_segment_end}",
        source=REPAIR_SOURCE,
        signal_value=0,
        evidence_ref=str(quarantine_path),
        ts=localio.utc_now_iso(),
        context={
            "schema": REPAIR_SCHEMA,
            "schema_version": REPAIR_SCHEMA_VERSION,
            "kind": break_info.kind,
            "break_line": break_info.line_no,
            "suspected_cause": break_info.suspected_cause,
            "invalid_segment_start": break_info.invalid_segment_start,
            "invalid_segment_end": break_info.invalid_segment_end,
            "expected_prev": break_info.expected_prev,
            "actual_prev": break_info.actual_prev,
        },
    )


def _write_repaired_ledger(path: Path, prefix: bytes, repair_row: dict[str, Any]) -> None:
    line = (json.dumps(repair_row, sort_keys=True) + "\n").encode("utf-8")
    localio.write_bytes_atomic(path, prefix + line)


def _publish_exclusive_bytes(path: Path, data: bytes) -> None:
    """Write-once publish for quarantine evidence (UTF-8 when possible)."""
    try:
        localio.write_text_exclusive(path, data.decode("utf-8"))
    except UnicodeDecodeError:
        # Ledger JSONL is UTF-8 in practice; keep a bytes fallback for evidence only.
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            path.unlink(missing_ok=True)
            raise


def repair_ledger(
    *,
    target: Path,
    operator_confirmed: bool = False,
    json_output: bool = False,
) -> int:
    """Quarantine a broken completed ledger, keep the valid prefix, append a repair record."""
    if operator_confirmed is not True:
        print("error: operator confirmation is required (--operator-confirm)", file=sys.stderr)
        return 2
    target = target.expanduser().resolve()
    path = outcome_cmd._records_path(target)
    lock_path = outcome_cmd._records_lock_path(target)
    if not path.is_file():
        print(f"outcome repair: no ledger at {path}")
        return 0

    with outcome_cmd._records_append_lock(lock_path):
        outcome_cmd._recover_interrupted_append(path)
        break_info = diagnose_completed_ledger(path)
        if break_info is None:
            if json_output:
                print(json.dumps({"target": str(target), "status": "healthy"}, indent=2, sort_keys=True))
            else:
                print(f"outcome repair: ledger healthy ({path})")
            return 0

        try:
            original = path.read_bytes()
        except OSError as exc:
            print(f"error: could not read outcome ledger: {path}: {exc}", file=sys.stderr)
            return 1

        operation_id = _operation_id(original, break_info)
        operation_dir, quarantine_path, invalid_path, record_path = _repair_paths(target, operation_id)
        operation_dir.mkdir(parents=True, exist_ok=True)

        if quarantine_path.exists():
            try:
                existing = quarantine_path.read_bytes()
            except OSError as exc:
                print(f"error: could not read quarantine: {quarantine_path}: {exc}", file=sys.stderr)
                return 1
            if existing != original:
                print(
                    f"error: repair quarantine already exists with different bytes: {quarantine_path}",
                    file=sys.stderr,
                )
                return 1
        else:
            try:
                _publish_exclusive_bytes(quarantine_path, original)
            except FileExistsError:
                print(f"error: repair quarantine already exists: {quarantine_path}", file=sys.stderr)
                return 1
            except OSError as exc:
                print(f"error: could not quarantine ledger: {exc}", file=sys.stderr)
                return 1

        if not invalid_path.exists():
            try:
                _publish_exclusive_bytes(invalid_path, break_info.invalid_segment_bytes)
            except FileExistsError:
                pass
            except OSError as exc:
                print(f"error: could not preserve invalid segment: {exc}", file=sys.stderr)
                return 1

        repair_record = _build_repair_record(break_info, operation_id=operation_id, quarantine_path=quarantine_path)
        row = outcome_cmd._record_payload(repair_record)
        row["prev_digest"] = break_info.last_valid_digest
        row["digest"] = localio.canonical_json_digest(row, exclude_keys={"digest"})

        audit = {
            "schema": REPAIR_SCHEMA,
            "schema_version": REPAIR_SCHEMA_VERSION,
            "operation_id": operation_id,
            "break_line": break_info.line_no,
            "kind": break_info.kind,
            "suspected_cause": break_info.suspected_cause,
            "expected_prev": break_info.expected_prev,
            "actual_prev": break_info.actual_prev,
            "valid_prefix_lines": break_info.valid_prefix_lines,
            "invalid_segment_start": break_info.invalid_segment_start,
            "invalid_segment_end": break_info.invalid_segment_end,
            "quarantine_path": str(quarantine_path),
            "invalid_segment_path": str(invalid_path),
            "repair_digest": row["digest"],
        }
        if not record_path.exists():
            try:
                localio.write_json_exclusive(record_path, audit)
            except FileExistsError:
                pass
            except OSError as exc:
                print(f"error: could not write repair record: {exc}", file=sys.stderr)
                return 1

        try:
            _write_repaired_ledger(path, break_info.valid_prefix_bytes, row)
            outcome_cmd._validate_completed_ledger(path)
        except outcome_cmd.OutcomeLedgerError as exc:
            print(f"error: repaired ledger failed verification: {exc}", file=sys.stderr)
            return 1
        except OSError as exc:
            print(f"error: could not write repaired ledger: {exc}", file=sys.stderr)
            return 1

    report = LedgerRepairReport(
        operation_id=operation_id,
        break_line=break_info.line_no,
        kind=break_info.kind,
        suspected_cause=break_info.suspected_cause,
        quarantine_path=quarantine_path,
        invalid_segment_path=invalid_path,
        record_path=record_path,
        valid_prefix_lines=break_info.valid_prefix_lines,
        invalid_segment_start=break_info.invalid_segment_start,
        invalid_segment_end=break_info.invalid_segment_end,
    )
    if json_output:
        print(json.dumps(dataclasses.asdict(report), indent=2, sort_keys=True, default=str))
        return 0
    print(f"outcome repair: repaired {path}")
    print(f"operation: {report.operation_id}")
    print(f"break: line {report.break_line} [{report.kind}] suspected={report.suspected_cause}")
    print(f"quarantine: {report.quarantine_path}")
    print(f"invalid_segment: {report.invalid_segment_path}")
    print(f"record: {report.record_path}")
    print(f"kept_prefix_lines: {report.valid_prefix_lines}")
    return 0


def repair(
    *,
    target: Path,
    operator_confirmed: bool = False,
    json_output: bool = False,
) -> int:
    """CLI entry point alias."""
    return repair_ledger(target=target, operator_confirmed=operator_confirmed, json_output=json_output)
