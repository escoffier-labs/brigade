"""Shared Hermes adapter contract checks."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .selection import WRITER_INBOXES

HERMES_FRAGMENT_FILES = (
    "workspace.harness.json",
    "memory-handoff.harness.json",
    "model-lanes.harness.json",
    "README.md",
)


def inspect_hermes_adapter(target: Path, inbox_rel: str | None = None) -> list[dict[str, Any]]:
    inbox_rel = inbox_rel or WRITER_INBOXES["hermes"]
    fragments_dir = target / ".brigade" / "hermes"
    results: list[dict[str, Any]] = []
    for name in HERMES_FRAGMENT_FILES:
        path = fragments_dir / name
        if path.is_file():
            results.append({"status": "ok", "id": "fragment", "fragment": name, "detail": str(path)})
        else:
            results.append(
                {
                    "status": "warn",
                    "id": "fragment",
                    "fragment": name,
                    "detail": f"missing at {path}; run `brigade hermes-fragments --out .brigade/hermes`",
                }
            )

    processed_rel = f"{inbox_rel}/processed"
    workspace_payload, workspace_error = _read_json_object(fragments_dir / "workspace.harness.json")
    if workspace_payload is not None:
        workspace = workspace_payload.get("workspace", {})
        if not isinstance(workspace, dict):
            workspace = {}
        handoff_inbox = workspace.get("handoff_inbox")
        if handoff_inbox == inbox_rel:
            results.append({"status": "ok", "id": "workspace_handoff_inbox", "detail": inbox_rel})
        else:
            results.append(
                {
                    "status": "fail",
                    "id": "workspace_handoff_inbox",
                    "detail": f"expected {inbox_rel}, found {handoff_inbox!r}",
                }
            )
    elif workspace_error:
        results.append({"status": "fail", "id": "workspace_json", "detail": workspace_error})

    handoff_payload, handoff_error = _read_json_object(fragments_dir / "memory-handoff.harness.json")
    if handoff_payload is not None:
        handoff = handoff_payload.get("memory_handoff", {})
        if not isinstance(handoff, dict):
            handoff = {}
        configured_inbox = handoff.get("inbox_dir")
        configured_processed = handoff.get("processed_dir")
        if configured_inbox == inbox_rel:
            results.append({"status": "ok", "id": "memory_handoff_inbox", "detail": inbox_rel})
        else:
            results.append(
                {
                    "status": "fail",
                    "id": "memory_handoff_inbox",
                    "detail": f"expected {inbox_rel}, found {configured_inbox!r}",
                }
            )
        if configured_processed == processed_rel:
            results.append({"status": "ok", "id": "processed_handoff_inbox", "detail": processed_rel})
        else:
            results.append(
                {
                    "status": "fail",
                    "id": "processed_handoff_inbox",
                    "detail": f"expected {processed_rel}, found {configured_processed!r}",
                }
            )
    elif handoff_error:
        results.append({"status": "fail", "id": "memory_handoff_json", "detail": handoff_error})
    return results


def _read_json_object(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    if not path.is_file():
        return None, None
    try:
        payload = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        return None, f"invalid JSON: {exc}"
    if not isinstance(payload, dict):
        return None, "expected JSON object"
    return payload, None
