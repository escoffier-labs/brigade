"""Parse Git pre-push updates without loading the verification runtime."""

from __future__ import annotations

import re

INPUT_BYTES = 256 * 1024
_OBJECT_ID = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


def branch_updates(data: str) -> list[tuple[str, str]]:
    if len(data) > INPUT_BYTES:
        raise ValueError("Pre-push input exceeds the size limit.")
    updates = []
    for line in data.splitlines():
        fields = line.split()
        if len(fields) != 4:
            raise ValueError("Expected four fields per pre-push line.")
        _, local, remote_ref, remote = fields
        if not (_OBJECT_ID.fullmatch(local) and _OBJECT_ID.fullmatch(remote) and len(local) == len(remote)):
            raise ValueError("Expected full Git object IDs in pre-push input.")
        if remote_ref.startswith("refs/heads/") and local != remote and local.strip("0"):
            updates.append((remote_ref, local))
    return updates
