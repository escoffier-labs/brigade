"""Attempt provenance for local verification receipts."""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .. import localio, receipt_json, receipt_schema
from . import helpers


def stamp_producer_attempt(receipt: dict[str, Any]) -> None:
    """Include an optional batch attempt digest before the receipt is sealed."""
    attempt_id = os.environ.get(receipt_schema.BRIGADE_ATTEMPT_ID_ENV)
    if attempt_id is not None and re.fullmatch(r"[0-9a-f]{64}", attempt_id):
        receipt["producer_attempt_id"] = attempt_id


def read_receipt(path: Path) -> dict[str, Any] | None:
    receipt = path / "receipt.json" if path.is_dir() else path
    if not receipt.is_file():
        return None
    try:
        data = receipt_json.read(receipt)
    except (OSError, ValueError, RecursionError):
        return None
    if not isinstance(data, dict):
        return None
    data.setdefault("path", str(receipt.parent))
    return data


def read_receipts(
    target: Path, *, reader: Callable[[Path], dict[str, Any] | None] = read_receipt
) -> list[dict[str, Any]]:
    root = helpers._verify_runs_root(target)
    if not root.is_dir():
        return []
    receipts = [reader(path) for path in root.iterdir() if path.is_dir()]
    valid = [item for item in receipts if isinstance(item, dict)]
    valid.sort(key=lambda item: str(item.get("started_at") or item.get("run_id") or ""), reverse=True)
    return valid


def task_verification(target: Path, *, task_id: str, attempt_id: str) -> dict[str, str] | None:
    """Project one sealed verifier receipt for this attempt and exact checkout tree."""
    tree: str | None = None
    for receipt in read_receipts(target):
        status = receipt.get("status")
        if (
            receipt.get("producer_attempt_id") != attempt_id
            or receipt.get("target") != str(target)
            or status not in ("completed", "failed", "rejected")
            or (
                receipt.get("tree_stable") is not True
                and not (status == "completed" and isinstance(receipt.get("reused_from"), str))
            )
        ):
            continue
        if tree is None:
            tree = localio.tree_fingerprint(target)
            if tree is None:
                return None
        if receipt.get("tree_fingerprint") != tree or receipt.get("tree_fingerprint_after") != tree:
            continue
        digests = receipt.get("digests")
        digest = digests.get("receipt_sha256") if isinstance(digests, dict) else None
        if digest == localio.canonical_json_digest(receipt, exclude_keys={"digests"}):
            return {"task_id": task_id, "attempt_id": attempt_id, "receipt_digest": digest, "status": status}
    return None
