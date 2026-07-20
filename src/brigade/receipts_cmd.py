"""Receipt digest verification for work, runbook, and outcome artifacts."""

from __future__ import annotations

import hmac
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from . import __version__
from . import code_references
from . import localio
from . import receipt_signing

OK = "OK"
MISMATCH = "MISMATCH"
MISSING = "MISSING"
LEGACY = "LEGACY"
SIGNED_OK = "SIGNED-OK"
SIGNATURE_MISMATCH = "SIGNATURE-MISMATCH"
UNVERIFIABLE_SIGNATURE = "UNVERIFIABLE-SIGNATURE"
MISELEDGER_SCHEMA = "miseledger.adapter.v1"
MISELEDGER_SOURCE = {"kind": "brigade", "name": "Brigade", "version": __version__}
MISELEDGER_ACTOR = {"external_id": "brigade:system", "type": "system", "name": "Brigade"}
MISELEDGER_CURSOR_REL = Path(".brigade") / "work" / "miseledger-export-cursor.json"
MISELEDGER_EXPORT_RESULT_SCHEMA = "brigade.miseledger_export_result.v1"
MISELEDGER_FLEET_EXPORT_RESULT_SCHEMA = "brigade.miseledger_fleet_export_result.v1"
_FLEET_STATUS_PRECEDENCE = ("failed", "exported", "nothing-new", "empty")
_CODE_REFERENCE_LIMIT = 100
_COMPACT_CODE_REFERENCE_NODE_LIMIT = 20


def _rel(path: Path, target: Path) -> str:
    try:
        return str(path.relative_to(target))
    except ValueError:
        return str(path)


def keygen(*, target: Path, force: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    try:
        path, key_id = receipt_signing.generate_key(target, force=force)
    except FileExistsError:
        print(f"error: receipt signing key already exists: {receipt_signing.key_path(target)}", file=sys.stderr)
        print("hint: pass --force to overwrite it", file=sys.stderr)
        return 1
    print(f"receipt signing key: {path}")
    print(f"key_id: {key_id}")
    print("reminder: keep this key gitignored; Brigade's .brigade/ ignore convention covers it.")
    return 0


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
    signature_item = _verify_digest_signature(
        digests=digests,
        receipt_digest=expected_receipt,
        artifact_type=artifact_type,
        artifact_id=artifact_id,
        path=path,
        target=target,
    )
    if signature_item is not None:
        items.append(signature_item)

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


def _verify_digest_signature(
    *,
    digests: dict[str, Any],
    receipt_digest: object,
    artifact_type: str,
    artifact_id: str,
    path: Path,
    target: Path,
) -> dict[str, Any] | None:
    signature = digests.get("signature")
    key_id = digests.get("key_id")
    if signature is None and key_id is None:
        return None
    if not isinstance(signature, str) or not signature or not isinstance(key_id, str) or not key_id:
        return _item(
            artifact_type=artifact_type,
            artifact_id=artifact_id,
            status=UNVERIFIABLE_SIGNATURE,
            check="digest_signature",
            detail="unverifiable-signature: signature or key_id is missing",
            path=path,
            target=target,
        )
    if not isinstance(receipt_digest, str) or not receipt_digest:
        return _item(
            artifact_type=artifact_type,
            artifact_id=artifact_id,
            status=UNVERIFIABLE_SIGNATURE,
            check="digest_signature",
            detail="unverifiable-signature: receipt_sha256 is missing",
            path=path,
            target=target,
        )
    try:
        loaded = receipt_signing.load_key(target)
    except (OSError, ValueError) as exc:
        return _item(
            artifact_type=artifact_type,
            artifact_id=artifact_id,
            status=UNVERIFIABLE_SIGNATURE,
            check="digest_signature",
            detail=f"unverifiable-signature: local key unavailable: {exc}",
            path=path,
            target=target,
            expected=key_id,
        )
    if loaded is None:
        return _item(
            artifact_type=artifact_type,
            artifact_id=artifact_id,
            status=UNVERIFIABLE_SIGNATURE,
            check="digest_signature",
            detail="unverifiable-signature: no local receipt signing key",
            path=path,
            target=target,
            expected=key_id,
        )
    key, local_key_id = loaded
    if local_key_id != key_id:
        return _item(
            artifact_type=artifact_type,
            artifact_id=artifact_id,
            status=UNVERIFIABLE_SIGNATURE,
            check="digest_signature",
            detail=f"unverifiable-signature: foreign key_id {key_id}",
            path=path,
            target=target,
            expected=key_id,
            actual=local_key_id,
        )
    actual_signature = receipt_signing.sign(receipt_digest, key)
    if not hmac.compare_digest(signature, actual_signature):
        return _item(
            artifact_type=artifact_type,
            artifact_id=artifact_id,
            status=SIGNATURE_MISMATCH,
            check="digest_signature",
            detail="SIGNATURE-MISMATCH: receipt digest signature does not match local key",
            path=path,
            target=target,
            expected=signature,
            actual=actual_signature,
        )
    return _item(
        artifact_type=artifact_type,
        artifact_id=artifact_id,
        status=SIGNED_OK,
        check="digest_signature",
        detail="receipt digest signature matches local key",
        path=path,
        target=target,
        expected=signature,
        actual=actual_signature,
    )


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
        if status == SIGNED_OK:
            counts[OK] += 1
        elif status == SIGNATURE_MISMATCH:
            counts[MISMATCH] += 1
        elif status in counts:
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
        if item["status"] in {MISMATCH, MISSING, LEGACY, SIGNATURE_MISMATCH, UNVERIFIABLE_SIGNATURE}:
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


def _one_line(value: object, limit: int = 500) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _hash_value(value: str) -> str:
    return value if value.startswith("sha256:") else f"sha256:{value}"


def _receipt_hash(payload: dict[str, Any], path: Path) -> tuple[str, str]:
    digests = payload.get("digests")
    if isinstance(digests, dict):
        receipt_digest = digests.get("receipt_sha256")
        if isinstance(receipt_digest, str) and receipt_digest:
            return _hash_value(receipt_digest), "receipt_digest"
    try:
        return _hash_value(localio.file_sha256(path)), "file_sha256"
    except OSError:
        return _hash_value(localio.canonical_json_digest(payload)), "canonical_json_digest"


def _git_value(target: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(target), *args],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None


def _github_host(host: str | None) -> bool:
    return bool(host and (host == "github.com" or host.endswith(".github.com")))


def _strip_git_suffix(repo: str) -> str:
    return repo[:-4] if repo.endswith(".git") else repo


def _github_remote_parts(remote: str) -> tuple[str, str, str] | None:
    parsed = urlparse(remote)
    if parsed.scheme == "https" and _github_host(parsed.hostname):
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) == 2:
            return parsed.hostname or "", parts[0], _strip_git_suffix(parts[1])
        return None
    match = re.match(r"^git@([^:]+):([^/]+)/(.+)$", remote)
    if match and _github_host(match.group(1)):
        return match.group(1), match.group(2), _strip_git_suffix(match.group(3))
    return None


def _receipt_git(payload: dict[str, Any]) -> dict[str, Any] | None:
    git = payload.get("git")
    if not isinstance(git, dict):
        return None
    head = git.get("head")
    if not isinstance(head, str) or not head:
        return None
    copied = {key: git[key] for key in ("head", "branch", "dirty_files") if key in git}
    return copied if copied else None


def _git_commit_links(item_external_id: str, payload: dict[str, Any], target: Path) -> list[dict[str, Any]]:
    git = _receipt_git(payload)
    if git is None:
        return []
    remote = _git_value(target, "remote", "get-url", "origin")
    if remote is None:
        return []
    parts = _github_remote_parts(remote)
    if parts is None:
        return []
    host, org, repo = parts
    return [
        {
            "external_id": f"{item_external_id}:git-commit",
            "kind": "url",
            "url": f"https://{host}/{org}/{repo}/commit/{git['head']}",
        }
    ]


def _metadata_with_git(metadata: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    git = _receipt_git(payload)
    if git is not None:
        metadata["git"] = git
    return metadata


def _code_reference_repository(target: Path) -> str | None:
    remote = _git_value(target, "remote", "get-url", "origin")
    if remote is None:
        return None
    parts = _github_remote_parts(remote)
    if parts is None:
        return None
    _, owner, repository = parts
    return f"{owner}/{repository}"


def _code_references_from_delta(payload: dict[str, Any], target: Path) -> tuple[list[dict[str, Any]], int, bool]:
    delta = payload.get("code_graph_delta")
    git = _receipt_git(payload)
    repository = _code_reference_repository(target)
    if not isinstance(delta, dict) or git is None or repository is None:
        return [], 0, False
    commit = git.get("head")
    if not isinstance(commit, str):
        return [], 0, False
    references: list[dict[str, Any]] = []
    compact_nodes = delta.get("code_reference_nodes")
    if isinstance(compact_nodes, list):
        node_sources: list[tuple[Any, str | None]] = [(compact_nodes, None)]
    else:
        node_sources = [
            (delta.get("added_nodes"), "added"),
            (delta.get("changed_nodes"), "changed"),
            (delta.get("removed_nodes"), "removed"),
        ]
    malformed_candidate = False
    for nodes, fallback_change_kind in node_sources:
        if not isinstance(nodes, list):
            continue
        for node in nodes:
            normalized = code_references.normalize_delta_node(node, fallback_change_kind)
            if normalized is None:
                malformed_candidate = True
                continue
            change_kind = normalized["change_kind"]
            file_path = normalized["file_path"]
            qualified_name = normalized["qualified_name"]
            symbol_kind = normalized["kind"]
            start_line = normalized["start_line"]
            end_line = normalized["end_line"]
            reference = {
                "schema": code_references.SCHEMA,
                "repository": repository,
                "revision": {"commit": commit},
                "file_path": file_path,
                "qualified_name": qualified_name,
                "symbol_kind": symbol_kind,
                "source_span": {"start_line": start_line, "line_count": end_line - start_line + 1},
                "change_kind": change_kind,
            }
            try:
                code_references.validate(reference)
            except ValueError:
                continue
            references.append(reference)
    references.sort(key=code_references.canonical_json)
    total = len(references)
    declared_total = delta.get("code_reference_nodes_total")
    declared_truncated = delta.get("code_reference_nodes_truncated")
    trusted_compaction_total = (
        isinstance(compact_nodes, list)
        and not malformed_candidate
        and isinstance(declared_total, int)
        and not isinstance(declared_total, bool)
        and (
            (declared_truncated is False and declared_total == total)
            or (
                declared_truncated is True
                and total == _COMPACT_CODE_REFERENCE_NODE_LIMIT
                and declared_total > _COMPACT_CODE_REFERENCE_NODE_LIMIT
            )
        )
    )
    if trusted_compaction_total and isinstance(declared_total, int):
        total = declared_total
    truncated = (trusted_compaction_total and declared_truncated is True) or total > _CODE_REFERENCE_LIMIT
    return references, total, truncated


def _metadata_with_code_references(metadata: dict[str, Any], payload: dict[str, Any], target: Path) -> dict[str, Any]:
    references, total, truncated = _code_references_from_delta(payload, target)
    if not references:
        return metadata
    metadata["code_references"] = references[:_CODE_REFERENCE_LIMIT]
    metadata["code_references_total"] = total
    metadata["code_references_truncated"] = truncated
    return metadata


def _metadata_with_digest_signature(metadata: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    digests = payload.get("digests")
    if not isinstance(digests, dict):
        return metadata
    signature = digests.get("signature")
    key_id = digests.get("key_id")
    if isinstance(signature, str) and signature and isinstance(key_id, str) and key_id:
        metadata["digest_signature"] = {"signature": signature, "key_id": key_id}
    return metadata


def _read_export_receipt(path: Path, target: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        payload = json.loads(path.read_text())
    except OSError as exc:
        print(f"warning: skipped unreadable receipt {_rel(path, target)}: {exc}", file=sys.stderr)
        return None, "unreadable"
    except json.JSONDecodeError as exc:
        print(f"warning: skipped malformed receipt {_rel(path, target)}: {exc}", file=sys.stderr)
        return None, "malformed"
    if not isinstance(payload, dict):
        print(f"warning: skipped malformed receipt {_rel(path, target)}: JSON is not an object", file=sys.stderr)
        return None, "malformed"
    return payload, None


def _timestamp_for_sort(payload: dict[str, Any], path: Path) -> str:
    for key in ("started_at", "completed_at", "finished_at", "created_at"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return path.parent.name


def _collect_export_receipts(target: Path) -> tuple[list[dict[str, Any]], int, list[str]]:
    specs = [
        ("work_verify", target / ".brigade" / "work" / "verify-runs", "*/receipt.json"),
        ("run", target / ".brigade" / "runs", "*/run.json"),
    ]
    receipts: list[dict[str, Any]] = []
    candidate_count = 0
    collection_error_kinds: list[str] = []
    for receipt_type, root, pattern in specs:
        if not root.is_dir():
            continue
        for path in sorted(root.glob(pattern)):
            candidate_count += 1
            payload, error_kind = _read_export_receipt(path, target)
            if payload is None:
                if error_kind is not None:
                    collection_error_kinds.append(error_kind)
                continue
            receipts.append(
                {
                    "receipt_type": receipt_type,
                    "path": path,
                    "payload": payload,
                    "sort_key": (_timestamp_for_sort(payload, path), str(path)),
                }
            )
    receipts.sort(key=lambda item: item["sort_key"], reverse=True)
    return receipts, candidate_count, collection_error_kinds


def _metadata_with_delta(metadata: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    delta = payload.get("code_graph_delta")
    if isinstance(delta, dict):
        summary = _one_line(delta.get("summary") or delta.get("status") or "code graph delta present")
        metadata["code_graph_delta_summary"] = summary
        metadata["code_graph_delta"] = delta
    return metadata


def _append_delta_text(text: str, payload: dict[str, Any]) -> str:
    delta = payload.get("code_graph_delta")
    if not isinstance(delta, dict):
        return text
    summary = _one_line(delta.get("summary") or delta.get("status") or "code graph delta present", 200)
    return f"{text} Code graph delta: {summary}."


def _mime_for(path: Path) -> str:
    if path.suffix == ".json":
        return "application/json"
    if path.suffix == ".log":
        return "text/plain"
    return "application/octet-stream"


def _safe_digest_log_path(run_dir: Path, name: object) -> Path | None:
    if not isinstance(name, str) or not name:
        return None
    rel = Path(name)
    if rel.is_absolute() or ".." in rel.parts:
        return None
    return run_dir / rel


def _artifact_path(path: Path, target: Path) -> str:
    return _rel(path, target)


def _receipt_artifact(item_external_id: str, path: Path, target: Path, digest: str) -> dict[str, Any]:
    return {
        "external_id": f"{item_external_id}:receipt",
        "kind": "receipt",
        "path": _artifact_path(path, target),
        "mime_type": _mime_for(path),
        "hash": digest,
    }


def _verify_log_artifacts(
    item_external_id: str, payload: dict[str, Any], path: Path, target: Path
) -> list[dict[str, Any]]:
    run_dir = path.parent
    artifacts: list[dict[str, Any]] = []
    digests = payload.get("digests")
    logs = digests.get("logs") if isinstance(digests, dict) else None
    if isinstance(logs, dict):
        for name, digest in sorted(logs.items()):
            log_path = _safe_digest_log_path(run_dir, name)
            if log_path is None or not isinstance(digest, str) or not digest:
                continue
            artifacts.append(
                {
                    "external_id": f"{item_external_id}:artifact:{name}",
                    "kind": "code_graph_delta" if Path(str(name)).name == "graph-delta.json" else "log",
                    "path": _artifact_path(log_path, target),
                    "mime_type": _mime_for(log_path),
                    "hash": _hash_value(digest),
                }
            )
        return artifacts

    seen: set[str] = set()
    commands = payload.get("commands")
    if not isinstance(commands, list):
        return artifacts
    for command in commands:
        if not isinstance(command, dict):
            continue
        for key in ("stdout_log_path", "stderr_log_path"):
            raw_path = command.get(key)
            if not isinstance(raw_path, str) or not raw_path:
                continue
            log_path = Path(raw_path)
            if not log_path.is_absolute():
                log_path = run_dir / log_path
            if not log_path.is_file():
                continue
            rel_path = _artifact_path(log_path, target)
            if rel_path in seen:
                continue
            seen.add(rel_path)
            try:
                digest = _hash_value(localio.file_sha256(log_path))
            except OSError:
                continue
            artifacts.append(
                {
                    "external_id": f"{item_external_id}:artifact:{Path(rel_path).name}",
                    "kind": "log",
                    "path": rel_path,
                    "mime_type": _mime_for(log_path),
                    "hash": digest,
                }
            )
    return artifacts


def _short_commands(payload: dict[str, Any]) -> list[dict[str, Any]]:
    commands = payload.get("commands")
    if not isinstance(commands, list):
        return []
    out: list[dict[str, Any]] = []
    for command in commands:
        if not isinstance(command, dict):
            continue
        item: dict[str, Any] = {}
        for key in ("command", "status", "exit_code"):
            if key in command:
                item[key] = command[key]
        if item:
            out.append(item)
    return out


def _verify_miseledger_item(payload: dict[str, Any], path: Path, target: Path, ordinal: int) -> dict[str, Any]:
    run_id = str(payload.get("run_id") or path.parent.name)
    item_external_id = f"brigade:work-verify:{run_id}"
    receipt_hash, digest_source = _receipt_hash(payload, path)
    commands = _short_commands(payload)
    metadata: dict[str, Any] = {
        "receipt_type": "work_verify",
        "run_id": run_id,
        "status": payload.get("status"),
        "target": payload.get("target"),
        "path": _rel(path, target),
        "digest": receipt_hash,
        "digest_source": digest_source,
        "command_count": len(commands),
        "commands": commands,
    }
    metadata = _metadata_with_delta(metadata, payload)
    metadata = _metadata_with_code_references(metadata, payload, target)
    metadata = _metadata_with_git(metadata, payload)
    metadata = _metadata_with_digest_signature(metadata, payload)
    text = f"Brigade work verify run {run_id} status={payload.get('status') or 'unknown'} commands={len(commands)}."
    command_text = _one_line(
        " ; ".join(str(command.get("command") or "") for command in commands if isinstance(command, dict)),
        limit=240,
    )
    if command_text:
        text = f"{text} Commands: {command_text}."
    text = _append_delta_text(text, payload)
    artifacts = [_receipt_artifact(item_external_id, path, target, receipt_hash)]
    artifacts.extend(_verify_log_artifacts(item_external_id, payload, path, target))
    return {
        "schema": MISELEDGER_SCHEMA,
        "source": MISELEDGER_SOURCE,
        "collection": {
            "external_id": "brigade:work:verify-runs",
            "kind": "brigade_work_verify_runs",
            "name": "Brigade work verify runs",
        },
        "item": {
            "external_id": item_external_id,
            "kind": "brigade_work_verify_receipt",
            "created_at": str(payload.get("started_at") or ""),
            "updated_at": str(payload.get("completed_at") or ""),
            "text": text,
            "tags": ["brigade", "receipt", "work-verify"],
            "metadata": metadata,
        },
        "actor": MISELEDGER_ACTOR,
        "artifacts": artifacts,
        "links": _git_commit_links(item_external_id, payload, target),
        "relations": [],
        "raw": {
            "format": "json",
            "hash": receipt_hash,
            "path": _rel(path, target),
            "ordinal": ordinal,
        },
    }


def _run_miseledger_item(payload: dict[str, Any], path: Path, target: Path, ordinal: int) -> dict[str, Any]:
    run_id = path.parent.name
    item_external_id = f"brigade:run:{run_id}"
    receipt_hash, digest_source = _receipt_hash(payload, path)
    task = _one_line(payload.get("task"), 300)
    metadata: dict[str, Any] = {
        "receipt_type": "run",
        "run_id": run_id,
        "status": payload.get("status"),
        "cwd": payload.get("cwd"),
        "path": _rel(path, target),
        "digest": receipt_hash,
        "digest_source": digest_source,
        "task": payload.get("task"),
        "orchestrator": payload.get("orchestrator"),
        "dry_run": payload.get("dry_run"),
        "read_only": payload.get("read_only"),
    }
    metadata = _metadata_with_delta(metadata, payload)
    metadata = _metadata_with_code_references(metadata, payload, target)
    metadata = _metadata_with_git(metadata, payload)
    metadata = _metadata_with_digest_signature(metadata, payload)
    text = f"Brigade run {run_id} status={payload.get('status') or 'unknown'}."
    if task:
        text += f" Task: {task}."
    text = _append_delta_text(text, payload)
    artifacts = [_receipt_artifact(item_external_id, path, target, receipt_hash)]
    output_dir = payload.get("artifacts")
    if isinstance(output_dir, str) and output_dir:
        artifacts.append(
            {
                "external_id": f"{item_external_id}:artifacts",
                "kind": "directory",
                "path": _artifact_path(Path(output_dir), target),
                "mime_type": "inode/directory",
            }
        )
    return {
        "schema": MISELEDGER_SCHEMA,
        "source": MISELEDGER_SOURCE,
        "collection": {
            "external_id": "brigade:runs",
            "kind": "brigade_runs",
            "name": "Brigade runs",
        },
        "item": {
            "external_id": item_external_id,
            "kind": "brigade_run_receipt",
            "created_at": str(payload.get("started_at") or ""),
            "updated_at": str(payload.get("finished_at") or ""),
            "text": text,
            "tags": ["brigade", "receipt", "run"],
            "metadata": metadata,
        },
        "actor": MISELEDGER_ACTOR,
        "artifacts": artifacts,
        "links": _git_commit_links(item_external_id, payload, target),
        "relations": [],
        "raw": {
            "format": "json",
            "hash": receipt_hash,
            "path": _rel(path, target),
            "ordinal": ordinal,
        },
    }


def _miseledger_item(receipt: dict[str, Any], target: Path, ordinal: int) -> dict[str, Any]:
    payload = receipt["payload"]
    path = receipt["path"]
    if receipt["receipt_type"] == "work_verify":
        return _verify_miseledger_item(payload, path, target, ordinal)
    return _run_miseledger_item(payload, path, target, ordinal)


def _render_miseledger_jsonl(records: list[dict[str, Any]]) -> str:
    return "".join(json.dumps(record, sort_keys=True, separators=(",", ":"), default=str) + "\n" for record in records)


def _miseledger_jsonl_lines(records: list[dict[str, Any]]) -> list[tuple[str, str]]:
    lines: list[tuple[str, str]] = []
    for record in records:
        raw = record.get("raw")
        raw_hash = raw.get("hash") if isinstance(raw, dict) else None
        if not isinstance(raw_hash, str) or not raw_hash:
            continue
        line = json.dumps(record, sort_keys=True, separators=(",", ":"), default=str) + "\n"
        lines.append((line, raw_hash))
    return lines


def _miseledger_cursor_path(target: Path) -> Path:
    return target / MISELEDGER_CURSOR_REL


def _read_miseledger_cursor_hashes(target: Path) -> set[str]:
    payload = localio.read_json_dict(_miseledger_cursor_path(target)) or {}
    hashes = payload.get("raw_hashes")
    if not isinstance(hashes, list):
        return set()
    return {value for value in hashes if isinstance(value, str) and value}


def _write_miseledger_cursor_hashes(target: Path, hashes: set[str]) -> None:
    localio.write_json(
        _miseledger_cursor_path(target),
        {
            "schema": "brigade.miseledger_export_cursor.v1",
            "source": "brigade",
            "raw_hashes": sorted(hashes),
        },
    )


def _write_miseledger_lines_to_stdout(lines: list[tuple[str, str]]) -> tuple[int, list[str]]:
    written_hashes: list[str] = []
    try:
        for line, raw_hash in lines:
            written = sys.stdout.write(line)
            if written != len(line):
                raise OSError("short write to stdout")
            written_hashes.append(raw_hash)
    except OSError as exc:
        print(f"error: could not write output stdout: {exc}", file=sys.stderr)
        return 1, written_hashes
    return 0, written_hashes


def _write_miseledger_lines_to_path(output_path: Path, lines: list[tuple[str, str]]) -> tuple[int, list[str]]:
    written_hashes: list[str] = []
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open("w", encoding="utf-8") as handle:
            for line, raw_hash in lines:
                written = handle.write(line)
                if written != len(line):
                    raise OSError("short write")
                written_hashes.append(raw_hash)
    except OSError as exc:
        print(f"error: could not write output {output_path}: {exc}", file=sys.stderr)
        return 1, written_hashes
    return 0, written_hashes


def _temporary_miseledger_export_path(target: Path) -> Path:
    work_dir = target / ".brigade" / "work"
    work_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=work_dir,
        prefix="miseledger-export-",
        suffix=".jsonl",
        delete=False,
    ) as handle:
        return Path(handle.name)


def _miseledger_import_summary(payload: object) -> tuple[object, object]:
    if not isinstance(payload, dict):
        return "unknown", "unknown"
    inserted = payload.get("inserted_items", payload.get("inserted", "unknown"))
    known = payload.get("already_known", payload.get("known_items", "unknown"))
    return inserted, known


def _import_miseledger_file(
    path: Path, *, strict: bool = False, quiet: bool = False, failed_on_error: bool | None = None
) -> tuple[bool, bool]:
    """Import a JSONL export with miseledger.

    Returns ``(attempted, failed)``. ``attempted`` is False when the subprocess
    was skipped entirely (for example a zero-row export).
    """
    if failed_on_error is None:
        failed_on_error = strict
    binary = shutil.which("miseledger")
    if binary is None:
        if failed_on_error and strict:
            print(
                f"error: miseledger import failed; export kept at {path}: binary not found",
                file=sys.stderr,
            )
            return True, True
        message = f"warning: miseledger binary not found on PATH; export kept at {path}"
        if failed_on_error:
            print(message, file=sys.stderr)
            return True, True
        print(message, file=sys.stderr)
        return True, False
    try:
        result = subprocess.run(
            [binary, "import", "adapter", str(path), "--source", "brigade", "--json"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        detail = _one_line(str(exc), 500)
        if failed_on_error:
            prefix = "error" if strict else "warning"
            print(f"{prefix}: miseledger import failed; export kept at {path}: {detail}", file=sys.stderr)
            return True, True
        print(f"warning: miseledger import failed; export kept at {path}: {exc}", file=sys.stderr)
        return True, False
    if result.returncode != 0:
        detail = _one_line(result.stderr or result.stdout or f"exit {result.returncode}", 500)
        if failed_on_error:
            prefix = "error" if strict else "warning"
            print(f"{prefix}: miseledger import failed; export kept at {path}: {detail}", file=sys.stderr)
            return True, True
        print(f"warning: miseledger import failed; export kept at {path}: {detail}", file=sys.stderr)
        return True, False
    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        payload = {}
    inserted, known = _miseledger_import_summary(payload)
    if not quiet:
        print(f"miseledger import: inserted_items={inserted} already_known={known}")
    return True, False


def _single_repo_export_status(
    *,
    candidate_count: int,
    exported_count: int,
    skipped_count: int,
    error_count: int,
) -> str:
    if error_count > 0:
        return "failed"
    if exported_count > 0:
        return "exported"
    if skipped_count > 0 and candidate_count > 0:
        return "nothing-new"
    return "empty"


def _fleet_export_status(repo_statuses: list[str]) -> str:
    for status in _FLEET_STATUS_PRECEDENCE:
        if status in repo_statuses:
            return status
    return "empty"


def _fleet_repo_errors(result: dict[str, Any]) -> list[str]:
    errors = result.get("errors")
    if isinstance(errors, list) and errors:
        return [str(error) for error in errors]
    repo_id = str(result.get("repo_id") or "repo")
    error_count = int(result.get("error_count") or 0)
    collection_error_kinds = result.get("collection_error_kinds")
    if isinstance(collection_error_kinds, list):
        summaries: list[str] = []
        for kind in ("malformed", "unreadable"):
            count = collection_error_kinds.count(kind)
            if count:
                summaries.append(f"{repo_id}: skipped {count} {kind} receipt(s)")
        if summaries:
            return summaries
    if error_count > 0:
        return [f"{repo_id}: skipped {error_count} unreadable receipt(s)"]
    return []


def _repository_export_result(
    *,
    target: Path,
    limit: int,
    new_only: bool,
) -> dict[str, Any]:
    receipts, candidate_count, collection_error_kinds = _collect_export_receipts(target)
    collection_error_count = len(collection_error_kinds)
    selected = receipts[:limit] if limit else receipts
    records = [_miseledger_item(receipt, target, ordinal) for ordinal, receipt in enumerate(selected, start=1)]
    cursor_hashes: set[str] = set()
    if new_only:
        cursor_hashes = _read_miseledger_cursor_hashes(target)
    skipped_count = 0
    kept_records: list[dict[str, Any]] = []
    for record in records:
        raw = record.get("raw")
        raw_hash = raw.get("hash") if isinstance(raw, dict) else None
        if new_only and isinstance(raw_hash, str) and raw_hash in cursor_hashes:
            skipped_count += 1
            continue
        kept_records.append(record)
    lines = _miseledger_jsonl_lines(kept_records)
    exported_count = len(lines)
    error_count = collection_error_count
    status = _single_repo_export_status(
        candidate_count=candidate_count,
        exported_count=exported_count,
        skipped_count=skipped_count,
        error_count=error_count,
    )
    return {
        "status": status,
        "candidate_count": candidate_count,
        "exported_count": exported_count,
        "skipped_count": skipped_count,
        "error_count": error_count,
        "collection_error_kinds": collection_error_kinds,
        "lines": lines,
        "cursor_hashes": cursor_hashes,
        "target": target,
    }


def _single_repo_export_payload(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": MISELEDGER_EXPORT_RESULT_SCHEMA,
        "status": result["status"],
        "target_label": "repository",
        "candidate_count": result["candidate_count"],
        "exported_count": result["exported_count"],
        "skipped_count": result["skipped_count"],
        "error_count": result["error_count"],
    }


def _print_export_result(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))


def _finalize_repo_cursors(
    repo_results: list[dict[str, Any]],
    *,
    new_only: bool,
    written_hashes_by_target: dict[Path, list[str]],
) -> int:
    if not new_only:
        return 0
    for result in repo_results:
        target = result.get("target")
        if not isinstance(target, Path):
            continue
        written_hashes = written_hashes_by_target.get(target, [])
        if not written_hashes:
            continue
        cursor_hashes = result.get("cursor_hashes")
        if not isinstance(cursor_hashes, set):
            cursor_hashes = set()
        try:
            _write_miseledger_cursor_hashes(target, cursor_hashes | set(written_hashes))
        except OSError as exc:
            print(f"error: could not write cursor {_miseledger_cursor_path(target)}: {exc}", file=sys.stderr)
            return 1
    return 0


def _write_aggregate_lines(output_path: Path, lines: list[tuple[str, str]]) -> tuple[int, list[str]]:
    return _write_miseledger_lines_to_path(output_path, lines)


def _export_miseledger_fleet(
    *,
    target: Path,
    out: str | Path,
    limit: int,
    new_only: bool,
    import_miseledger: bool,
) -> int:
    from .repos_cmd import fleet as repo_fleet

    entries, config_errors, config_loaded = repo_fleet._load_config(target)
    if not config_loaded:
        print(f"error: no repo fleet config at {target / '.brigade' / 'repos.toml'}", file=sys.stderr)
        return 2
    if config_errors:
        for error in config_errors:
            safe_error = repo_fleet._safe_text(error, target, "repo-fleet", "repo fleet")
            print(f"error: repo fleet config: {safe_error}", file=sys.stderr)
        _print_export_result(_fleet_export_payload([], status="failed"))
        return 1

    enabled_entries = [entry for entry in entries if entry.enabled]
    repo_results: list[dict[str, Any]] = []
    aggregate_lines: list[tuple[str, str]] = []
    written_hashes_by_target: dict[Path, list[str]] = {}

    for entry in enabled_entries:
        if not entry.path.is_dir():
            print(f"error: enabled repo path is missing: {entry.repo_id}", file=sys.stderr)
            repo_results.append(
                {
                    "repo_id": entry.repo_id,
                    "repo_label": entry.label,
                    "status": "failed",
                    "candidate_count": 0,
                    "exported_count": 0,
                    "skipped_count": 0,
                    "error_count": 1,
                    "errors": [f"{entry.repo_id}: enabled repo path is missing"],
                    "lines": [],
                    "cursor_hashes": set(),
                    "target": entry.path,
                }
            )
            continue
        result = _repository_export_result(target=entry.path, limit=limit, new_only=new_only)
        result["repo_id"] = entry.repo_id
        result["repo_label"] = entry.label
        repo_results.append(result)
        aggregate_lines.extend(result["lines"])

    output_path = _temporary_miseledger_export_path(target) if str(out) == "-" else Path(out).expanduser()
    exit_code, written_hashes = _write_aggregate_lines(output_path, aggregate_lines)
    if exit_code != 0:
        payload = _fleet_export_payload(repo_results, status="failed")
        _print_export_result(payload)
        return 1

    offset = 0
    for result in repo_results:
        line_count = len(result["lines"])
        result_written = written_hashes[offset : offset + line_count]
        offset += line_count
        if result_written:
            written_hashes_by_target[result["target"]] = result_written

    cursor_exit = _finalize_repo_cursors(
        repo_results, new_only=new_only, written_hashes_by_target=written_hashes_by_target
    )
    if cursor_exit != 0:
        payload = _fleet_export_payload(repo_results, status="failed")
        _print_export_result(payload)
        return 1

    import_error_count = 0
    if import_miseledger and aggregate_lines:
        attempted, failed = _import_miseledger_file(output_path, strict=True, quiet=True)
        if attempted and failed:
            import_error_count = 1

    status = _fleet_export_status([str(result["status"]) for result in repo_results])
    if import_error_count:
        status = "failed"
    payload = _fleet_export_payload(repo_results, status=status, import_error_count=import_error_count)
    _print_export_result(payload)
    if status == "failed":
        return 1
    return 0


def _fleet_export_payload(
    repo_results: list[dict[str, Any]],
    *,
    status: str,
    import_error_count: int = 0,
) -> dict[str, Any]:
    repos = [
        {
            "repo_id": str(result["repo_id"]),
            "repo_label": str(result.get("repo_label") or result["repo_id"]),
            "status": str(result["status"]),
            "candidate_count": int(result["candidate_count"]),
            "exported_count": int(result["exported_count"]),
            "skipped_count": int(result["skipped_count"]),
            "error_count": int(result["error_count"]),
            "errors": _fleet_repo_errors(result),
        }
        for result in repo_results
    ]
    counts = {
        "empty": 0,
        "nothing-new": 0,
        "exported": 0,
        "failed": 0,
    }
    exported_count = 0
    skipped_count = 0
    for result in repo_results:
        repo_status = str(result["status"])
        if repo_status in counts:
            counts[repo_status] += 1
        exported_count += int(result["exported_count"])
        skipped_count += int(result["skipped_count"])
    if import_error_count:
        status = "failed"
    return {
        "schema": MISELEDGER_FLEET_EXPORT_RESULT_SCHEMA,
        "status": status,
        "target_label": "repository-fleet",
        "config_path_label": ".brigade/repos.toml",
        "repo_count": len(repos),
        "exported_count": exported_count,
        "skipped_count": skipped_count,
        "empty_count": counts["empty"],
        "nothing_new_count": counts["nothing-new"],
        "failed_count": counts["failed"],
        "repos": repos,
    }


def export_miseledger(
    *,
    target: Path,
    out: str | Path = "-",
    limit: int = 0,
    new_only: bool = False,
    import_miseledger: bool = False,
    json_output: bool = False,
    fleet: bool = False,
) -> int:
    if limit < 0:
        print("error: --limit must be zero or a positive integer", file=sys.stderr)
        return 2
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    if fleet:
        if not json_output:
            print("error: --fleet requires --json", file=sys.stderr)
            return 2
        if str(out) == "-" and not import_miseledger:
            print("error: --fleet requires --out to name a file unless --import is set", file=sys.stderr)
            return 2
        return _export_miseledger_fleet(
            target=target,
            out=out,
            limit=limit,
            new_only=new_only,
            import_miseledger=import_miseledger,
        )
    if json_output and str(out) == "-":
        print("error: --json requires --out to name a file", file=sys.stderr)
        return 2

    result = _repository_export_result(target=target, limit=limit, new_only=new_only)
    candidate_count = int(result["candidate_count"])
    lines = result["lines"]
    import_error_count = 0

    if json_output:
        json_output_path = Path(out).expanduser()
        exit_code, written_hashes = _write_miseledger_lines_to_path(json_output_path, lines)
        if new_only and written_hashes:
            cursor_hashes = result["cursor_hashes"]
            if not isinstance(cursor_hashes, set):
                cursor_hashes = set()
            try:
                _write_miseledger_cursor_hashes(target, cursor_hashes | set(written_hashes))
            except OSError as exc:
                print(f"error: could not write cursor {_miseledger_cursor_path(target)}: {exc}", file=sys.stderr)
                failed_result = dict(result)
                failed_result["status"] = "failed"
                failed_result["error_count"] = int(failed_result["error_count"]) + 1
                _print_export_result(_single_repo_export_payload(failed_result))
                return 1
        if exit_code != 0:
            failed_result = dict(result)
            failed_result["status"] = "failed"
            failed_result["error_count"] = int(failed_result["error_count"]) + 1
            _print_export_result(_single_repo_export_payload(failed_result))
            return 1

        if import_miseledger:
            if lines:
                attempted, import_failed = _import_miseledger_file(json_output_path, strict=True, quiet=True)
                if attempted and import_failed:
                    import_error_count = 1

        payload = _single_repo_export_payload(result)
        if import_error_count:
            payload["status"] = "failed"
            payload["error_count"] = int(payload["error_count"]) + import_error_count
        _print_export_result(payload)
        return 1 if payload["status"] == "failed" else 0

    if candidate_count == 0:
        return 0

    if import_miseledger and not lines:
        if str(out) != "-":
            exit_code, _ = _write_miseledger_lines_to_path(Path(out).expanduser(), lines)
            if exit_code != 0:
                return exit_code
        if int(result["error_count"]) > 0:
            return 1
        print("nothing new; import skipped")
        return 0

    output_path: Path | None = None
    if str(out) == "-" and not import_miseledger:
        exit_code, written_hashes = _write_miseledger_lines_to_stdout(lines)
    else:
        output_path = _temporary_miseledger_export_path(target) if str(out) == "-" else Path(out).expanduser()
        exit_code, written_hashes = _write_miseledger_lines_to_path(output_path, lines)
    if new_only and written_hashes:
        cursor_hashes = result["cursor_hashes"]
        if not isinstance(cursor_hashes, set):
            cursor_hashes = set()
        try:
            _write_miseledger_cursor_hashes(target, cursor_hashes | set(written_hashes))
        except OSError as exc:
            print(f"error: could not write cursor {_miseledger_cursor_path(target)}: {exc}", file=sys.stderr)
            return 1
    if exit_code != 0:
        return exit_code
    if import_miseledger and output_path is not None:
        if lines:
            attempted, import_failed = _import_miseledger_file(output_path, failed_on_error=True)
            if attempted and import_failed:
                return 1
        else:
            print("nothing new; import skipped")
            return 0
    if int(result["error_count"]) > 0:
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    from .cli import main as cli_main

    return cli_main(argv)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
