"""Digest-addressed withdrawals for append-only machine logs."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any


def retraction(row: object) -> bool:
    return (
        isinstance(row, dict)
        and set(row) == {"retract_sha256"}
        and isinstance(row["retract_sha256"], str)
        and re.fullmatch(r"[0-9a-f]{64}", row["retract_sha256"]) is not None
    )


def withdrawn(data: bytes) -> bytes:
    return (json.dumps({"retract_sha256": hashlib.sha256(data).hexdigest()}, sort_keys=True) + "\n").encode()


def active(rows: list[tuple[dict[str, Any], bytes]]) -> list[dict[str, Any]]:
    removed = {row["retract_sha256"] for row, _ in rows if retraction(row)}
    return [row for row, raw in rows if not retraction(row) and hashlib.sha256(raw).hexdigest() not in removed]
