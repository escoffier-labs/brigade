"""Receipt digest verification for work, runbook, and outcome artifacts."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from . import localio

OK = "OK"
MISMATCH = "MISMATCH"
MISSING = "MISSING"
LEGACY = "LEGACY"


def _rel(path: Path, target: Path) -> str:
    try:
        return str(path.relative_to(target))
    except ValueError:
        return str(path)


def _item(
    *,
    artifact_type: str,
    artifact_id: str,
    status: str,
    check: str,
    detail: str,
    path: Path,
    target: Path,
    expected: str | None = None,
    actual: str | None = None,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "artifact_type": artifact_type,
        "artifact_id": artifact_id,
        "status": status,
        "check": check,
        "detail": detail,
        "path": _rel(path, target),
    }
    if expected is not None:
        item["expected"] = expected
    if actual is not None:
        item["actual"] = actual
    return item


def _read_receipt(
    path: Path, *, artifact_type: str, target: Path
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    try:
        payload = json.loads(path.read_text())
    except OSError as exc:
        return None, [
            _item(
                artifact_type=artifact_type,
                artifact_id=_rel(path, target),
                status=MISSING,
                check="receipt",
                detail=f"receipt unreadable: {exc}",
                path=path,
                target=target,
            )
        ]
    except json.JSONDecodeError as exc:
        return None, [
            _item(
                artifact_type=artifact_type,
                artifact_id=_rel(path, target),
                status=MISMATCH,
                check="receipt",
                detail=f"receipt is not valid JSON: {exc}",
                path=path,
                target=target,
            )
        ]
    if not isinstance(payload, dict):
        return None, [
            _item(
                artifact_type=artifact_type,
                artifact_id=_rel(path, target),
                status=MISMATCH,
                check="receipt",
                detail="receipt JSON is not an object",
                path=path,
                target=target,
            )
        ]
    return payload, []


def _verify_receipt(path: Path, *, artifact_type: str, log_type: str, target: Path) -> list[dict[str, Any]]:
    payload, problems = _read_receipt(path, artifact_type=artifact_type, target=target)
    if payload is None:
        return problems
    artifact_id = _rel(path, target)
    digests = payload.get("digests")
    if not isinstance(digests, dict):
        return [
            _item(
                artifact_type=artifact_type,
                artifact_id=artifact_id,
                status=LEGACY,
                check="digests",
                detail="receipt has no digests block",
                path=path,
                target=target,
            )
        ]
    items: list[dict[str, Any]] = []
    if digests.get("algorithm") != "sha256":
        items.append(
            _item(
                artifact_type=artifact_type,
                artifact_id=artifact_id,
                status=MISSING,
                check="algorithm",
                detail="digests.algorithm is missing or not sha256",
                path=path,
                target=target,
                expected="sha256",
                actual=str(digests.get("algorithm")),
            )
        )
    expected_receipt = digests.get("receipt_sha256")
    actual_receipt = localio.canonical_json_digest(payload, exclude_keys={"digests"})
    if not isinstance(expected_receipt, str) or not expected_receipt:
        items.append(
            _item(
                artifact_type=artifact_type,
                artifact_id=artifact_id,
                status=MISSING,
                check="receipt_sha256",
                detail="digests.receipt_sha256 is missing",
                path=path,
                target=target,
            )
        )
    elif expected_receipt != actual_receipt:
        items.append(
            _item(
                artifact_type=artifact_type,
                artifact_id=artifact_id,
                status=MISMATCH,
                check="receipt_sha256",
                detail="receipt digest does not match canonical receipt payload",
                path=path,
                target=target,
                expected=expected_receipt,
                actual=actual_receipt,
            )
        )
    else:
        items.append(
            _item(
                artifact_type=artifact_type,
                artifact_id=artifact_id,
                status=OK,
                check="receipt_sha256",
                detail="receipt digest matches",
                path=path,
                target=target,
                expected=expected_receipt,
                actual=actual_receipt,
            )
        )

    logs = digests.get("logs")
    if not isinstance(logs, dict):
        items.append(
            _item(
                artifact_type=artifact_type,
                artifact_id=artifact_id,
                status=MISSING,
                check="logs",
                detail="digests.logs is missing or not an object",
                path=path,
                target=target,
            )
        )
        return items
    for name, expected_digest in sorted(logs.items()):
        log_rel = Path(str(name))
        log_path = path.parent / log_rel
        log_id = f"{artifact_id}:{name}"
        if log_rel.is_absolute() or ".." in log_rel.parts:
            items.append(
                _item(
                    artifact_type=log_type,
                    artifact_id=log_id,
                    status=MISSING,
                    check="log_path",
                    detail="referenced log path is outside the receipt directory",
                    path=path.parent,
                    target=target,
                )
            )
            continue
        if not isinstance(expected_digest, str) or not expected_digest:
            items.append(
                _item(
                    artifact_type=log_type,
                    artifact_id=log_id,
                    status=MISSING,
                    check="log_sha256",
                    detail="log digest is missing",
                    path=log_path,
                    target=target,
                )
            )
            continue
        if not log_path.is_file():
            items.append(
                _item(
                    artifact_type=log_type,
                    artifact_id=log_id,
                    status=MISSING,
                    check="log_sha256",
                    detail="referenced log file is missing",
                    path=log_path,
                    target=target,
                    expected=expected_digest,
                )
            )
            continue
        actual_digest = localio.file_sha256(log_path)
        if actual_digest != expected_digest:
            items.append(
                _item(
                    artifact_type=log_type,
                    artifact_id=log_id,
                    status=MISMATCH,
                    check="log_sha256",
                    detail="log digest does not match file bytes",
                    path=log_path,
                    target=target,
                    expected=expected_digest,
                    actual=actual_digest,
                )
            )
        else:
            items.append(
                _item(
                    artifact_type=log_type,
                    artifact_id=log_id,
                    status=OK,
                    check="log_sha256",
                    detail="log digest matches",
                    path=log_path,
                    target=target,
                    expected=expected_digest,
                    actual=actual_digest,
                )
            )
    return items


def _verify_receipt_tree(target: Path) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for path in sorted((target / ".brigade" / "work" / "verify-runs").glob("*/receipt.json")):
        items.extend(
            _verify_receipt(path, artifact_type="work-verify-receipt", log_type="work-verify-log", target=target)
        )
    for path in sorted((target / ".brigade" / "runbooks" / "runs").glob("*/receipt.json")):
        items.extend(_verify_receipt(path, artifact_type="runbook-receipt", log_type="runbook-log", target=target))
    return items


def _ledger_item(
    *,
    path: Path,
    target: Path,
    line_no: int,
    status: str,
    check: str,
    detail: str,
    expected: str | None = None,
    actual: str | None = None,
) -> dict[str, Any]:
    return _item(
        artifact_type="outcome-ledger-record",
        artifact_id=f"{_rel(path, target)}:{line_no}",
        status=status,
        check=check,
        detail=detail,
        path=path,
        target=target,
        expected=expected,
        actual=actual,
    )


def _verify_outcome_ledger(target: Path) -> list[dict[str, Any]]:
    path = target / "memory" / "outcome" / "records.jsonl"
    if not path.exists():
        return []
    try:
        lines = path.read_text().splitlines()
    except OSError as exc:
        return [
            _ledger_item(
                path=path,
                target=target,
                line_no=0,
                status=MISSING,
                check="records.jsonl",
                detail=f"outcome ledger unreadable: {exc}",
            )
        ]
    items: list[dict[str, Any]] = []
    previous_digest: str | None = None
    for line_no, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            items.append(
                _ledger_item(
                    path=path,
                    target=target,
                    line_no=line_no,
                    status=MISMATCH,
                    check="json",
                    detail=f"ledger line is not valid JSON: {exc}",
                )
            )
            continue
        if not isinstance(row, dict):
            items.append(
                _ledger_item(
                    path=path,
                    target=target,
                    line_no=line_no,
                    status=MISMATCH,
                    check="json",
                    detail="ledger line is not an object",
                )
            )
            continue
        recorded_digest = row.get("digest")
        if not isinstance(recorded_digest, str) or not recorded_digest:
            items.append(
                _ledger_item(
                    path=path,
                    target=target,
                    line_no=line_no,
                    status=LEGACY,
                    check="digest",
                    detail="ledger record has no digest",
                )
            )
            continue
        if "prev_digest" not in row:
            items.append(
                _ledger_item(
                    path=path,
                    target=target,
                    line_no=line_no,
                    status=MISSING,
                    check="prev_digest",
                    detail="ledger record has no prev_digest",
                )
            )
            previous_digest = recorded_digest
            continue
        actual_prev = row.get("prev_digest")
        if actual_prev != previous_digest:
            items.append(
                _ledger_item(
                    path=path,
                    target=target,
                    line_no=line_no,
                    status=MISMATCH,
                    check="prev_digest",
                    detail="ledger chain link does not point to the previous digest",
                    expected=previous_digest,
                    actual=str(actual_prev),
                )
            )
            previous_digest = recorded_digest
            continue
        recomputed = localio.canonical_json_digest(row, exclude_keys={"digest"})
        if recomputed != recorded_digest:
            items.append(
                _ledger_item(
                    path=path,
                    target=target,
                    line_no=line_no,
                    status=MISMATCH,
                    check="digest",
                    detail="ledger record digest does not match canonical record payload",
                    expected=recorded_digest,
                    actual=recomputed,
                )
            )
        else:
            items.append(
                _ledger_item(
                    path=path,
                    target=target,
                    line_no=line_no,
                    status=OK,
                    check="digest",
                    detail="ledger record digest matches",
                    expected=recorded_digest,
                    actual=recomputed,
                )
            )
        previous_digest = recorded_digest
    return items


def _summary(items: list[dict[str, Any]]) -> dict[str, int]:
    counts = {OK: 0, MISMATCH: 0, MISSING: 0, LEGACY: 0}
    for item in items:
        status = str(item.get("status") or "")
        if status in counts:
            counts[status] += 1
    return {
        "total": len(items),
        "ok": counts[OK],
        "mismatch": counts[MISMATCH],
        "missing": counts[MISSING],
        "legacy": counts[LEGACY],
    }


def verify_payload(target: Path) -> dict[str, Any]:
    target = target.expanduser().resolve()
    artifacts = _verify_receipt_tree(target)
    artifacts.extend(_verify_outcome_ledger(target))
    return {"target": str(target), "summary": _summary(artifacts), "artifacts": artifacts}


def summary_detail(target: Path) -> str:
    summary = verify_payload(target)["summary"]
    return (
        f"checked={summary['total']} ok={summary['ok']} mismatch={summary['mismatch']} "
        f"missing={summary['missing']} legacy={summary['legacy']}"
    )


def verify(*, target: Path, json_output: bool = False) -> int:
    payload = verify_payload(target)
    summary = payload["summary"]
    failed = int(summary["mismatch"]) + int(summary["missing"])
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 1 if failed else 0
    print(f"receipts verify: {payload['target']}")
    print(
        f"summary: checked={summary['total']} ok={summary['ok']} mismatch={summary['mismatch']} "
        f"missing={summary['missing']} legacy={summary['legacy']}"
    )
    for item in payload["artifacts"]:
        if item["status"] in {MISMATCH, MISSING, LEGACY}:
            print(f"- {item['status']} {item['artifact_id']} [{item['check']}] {item['detail']}")
    return 1 if failed else 0


def doctor(*, target: Path, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    payload = verify_payload(target)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"outcome doctor: {target}")
    print(f"receipts: {summary_detail(target)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    from .cli import main as cli_main

    return cli_main(argv)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
