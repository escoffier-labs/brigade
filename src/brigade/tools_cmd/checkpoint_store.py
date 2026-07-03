"""Checkpoint storage helpers for the tools command family."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from . import helpers, paths, safety


def _checkpoint_paths(target: Path) -> list[Path]:
    path = paths.checkpoints_path(target)
    if not path.is_dir():
        return []
    return sorted(item for item in path.glob("*.json") if item.is_file())


def _read_checkpoint(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        payload = json.loads(path.read_text())
    except OSError as exc:
        return None, str(exc)
    except json.JSONDecodeError as exc:
        return None, f"invalid checkpoint JSON: {exc.msg}"
    if not isinstance(payload, dict):
        return None, "checkpoint must be a JSON object"
    payload.setdefault("checkpoint_path", str(path))
    return payload, None


def _write_checkpoint(target: Path, checkpoint: dict[str, Any]) -> Path:
    checkpoint_id = str(checkpoint.get("id") or "")
    path = paths.checkpoints_path(target) / f"{checkpoint_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    checkpoint["checkpoint_path"] = str(path)
    path.write_text(json.dumps(checkpoint, indent=2, sort_keys=True) + "\n")
    return path


def _checkpoint_public_summary(checkpoint: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": checkpoint.get("id"),
        "status": checkpoint.get("status"),
        "tool_id": checkpoint.get("tool_id"),
        "call_id": checkpoint.get("call_id"),
        "run_id": checkpoint.get("run_id"),
        "reason": checkpoint.get("reason"),
        "requested_action": checkpoint.get("requested_action"),
        "prompt": checkpoint.get("prompt"),
        "context": checkpoint.get("context", {}),
        "choices": checkpoint.get("choices", []),
        "selected_choice": checkpoint.get("selected_choice"),
        "created_at": checkpoint.get("created_at"),
        "expires_at": checkpoint.get("expires_at"),
        "reviewed_at": checkpoint.get("reviewed_at"),
        "review_reason": checkpoint.get("review_reason"),
        "resume_run_id": checkpoint.get("resume_run_id"),
        "checkpoint_path": checkpoint.get("checkpoint_path"),
    }


def _resolve_checkpoint(target: Path, checkpoint_id: str) -> tuple[dict[str, Any] | None, str | None]:
    matches: list[dict[str, Any]] = []
    for path in _checkpoint_paths(target):
        checkpoint, error = _read_checkpoint(path)
        if error is not None or checkpoint is None:
            continue
        candidate_id = str(checkpoint.get("id") or path.stem)
        if candidate_id.startswith(checkpoint_id) or path.stem.startswith(checkpoint_id):
            matches.append(checkpoint)
    if not matches:
        return None, f"checkpoint not found: {checkpoint_id}"
    if len(matches) > 1:
        return None, f"checkpoint id is ambiguous: {checkpoint_id}"
    return matches[0], None


def _normalize_checkpoint(
    target: Path,
    path: Path,
    *,
    call: dict[str, Any],
    run_id: str,
    fallback_created_at: str,
) -> tuple[dict[str, Any] | None, str | None]:
    raw, error = _read_checkpoint(path)
    if error is not None or raw is None:
        return None, error or "invalid checkpoint"
    checkpoint_id = raw.get("id")
    if not isinstance(checkpoint_id, str) or not checkpoint_id.strip():
        checkpoint_id = (
            f"checkpoint-{helpers._stable_hash({'path': str(path), 'call_id': call.get('id'), 'run_id': run_id})}"
        )
    choices = raw.get("choices", raw.get("allowed_resume_choices", []))
    if isinstance(choices, str):
        choices = [choices]
    if not isinstance(choices, list):
        choices = []
    context = raw.get("context", {})
    if not isinstance(context, (dict, list)):
        context = {"value": context}
    checkpoint = {
        "id": checkpoint_id.strip(),
        "status": "pending",
        "tool_id": call.get("tool_id"),
        "call_id": call.get("id"),
        "run_id": run_id,
        "reason": safety._redact_text(raw.get("reason") or "tool requested operator checkpoint", 240),
        "requested_action": safety._redact_text(raw.get("requested_action") or raw.get("action") or "review", 240),
        "prompt": safety._redact_text(raw.get("prompt") or raw.get("operator_prompt") or "", 1000),
        "context": safety._redact_payload(context),
        "choices": [safety._redact_text(choice, 160) for choice in choices],
        "created_at": str(raw.get("created_at") or fallback_created_at),
        "expires_at": raw.get("expires_at"),
        "reviewed_at": None,
        "review_reason": None,
        "selected_choice": None,
        "resume_run_id": None,
        "contract_fingerprint": call.get("contract_fingerprint"),
        "source_fingerprint": call.get("source_fingerprint"),
        "call_fingerprint": call.get("call_fingerprint"),
        "approval_fingerprint": call.get("approval_fingerprint"),
        "projection_summary": call.get("projection_summary", {}),
    }
    _write_checkpoint(target, checkpoint)
    if path.name != f"{checkpoint['id']}.json":
        try:
            path.unlink()
        except OSError:
            pass
    return checkpoint, None


def _collect_run_checkpoints(
    target: Path,
    *,
    call: dict[str, Any],
    run_id: str,
    fallback_created_at: str,
    started_epoch: float,
) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for path in _checkpoint_paths(target):
        try:
            if path.stat().st_mtime < started_epoch - 1:
                continue
        except OSError:
            continue
        raw, error = _read_checkpoint(path)
        if error is not None or raw is None:
            continue
        raw_call = raw.get("call_id")
        raw_run = raw.get("run_id")
        if raw_call not in (None, "", call.get("id")):
            continue
        if raw_run not in (None, "", run_id):
            continue
        if raw.get("status") not in (None, "", "pending"):
            continue
        checkpoint, normalize_error = _normalize_checkpoint(
            target,
            path,
            call=call,
            run_id=run_id,
            fallback_created_at=fallback_created_at,
        )
        if normalize_error is None and checkpoint is not None:
            found.append(checkpoint)
    found.sort(key=lambda item: str(item.get("created_at") or ""))
    return found


def _checkpoint_expired(checkpoint: dict[str, Any], *, now: datetime | None = None) -> bool:
    expires = helpers._parse_iso_datetime(checkpoint.get("expires_at"))
    if expires is None:
        return False
    return (now or helpers._now()) > expires
