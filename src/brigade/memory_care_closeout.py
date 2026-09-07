"""Write and dry-run preview for memory-care closeout receipts."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from . import work_cmd
from .localio import utc_now_iso_z as _utc_iso

_PREVIEW_FIELDS = ("card_id", "file", "issue_type", "safe_summary", "source_fingerprint")


def _block_message(*, defer: bool, reason_text: str, cards: list[Any]) -> str | None:
    if defer and not reason_text:
        return "error: deferred memory-care closeout requires a nonblank --reason"
    if not defer and cards:
        return (
            f"error: memory-care refresh queue has {len(cards)} unresolved candidate(s); "
            "closeout cannot mark them reviewed. Use --defer with a nonblank --reason to "
            "defer explicitly, or resolve the queue first."
        )
    return None


def _preview_cards(cards: list[Any]) -> tuple[dict[str, int], list[dict[str, Any]]]:
    grouped: dict[str, int] = {}
    preview: list[dict[str, Any]] = []
    for card in cards:
        if not isinstance(card, dict):
            continue
        kind = str(card.get("issue_type") or "unknown")
        grouped[kind] = grouped.get(kind, 0) + 1
        preview.append({key: card.get(key) for key in _PREVIEW_FIELDS})
    preview.sort(key=lambda item: (str(item.get("issue_type") or ""), str(item.get("file") or "")))
    return dict(sorted(grouped.items())), preview


def _print_blocked(message: str, *, json_output: bool, candidate_count: int) -> int:
    if json_output:
        print(
            json.dumps(
                {"status": "blocked", "error": message, "candidate_count": candidate_count},
                indent=2,
                sort_keys=True,
            )
        )
    else:
        print(message, file=sys.stderr)
    return 1


def _print_dry_run(payload: dict[str, Any]) -> None:
    print("memory_care_closeout: dry-run")
    print(f"intended_status: {payload['intended_status']}")
    print(f"would_write: {str(payload['would_write']).lower()}")
    print(f"would_block: {str(payload['would_block']).lower()}")
    print(f"candidates: {payload['candidate_count']}")
    if payload.get("block_reason"):
        print(f"block_reason: {payload['block_reason']}")
    by_kind: dict[str, list[dict[str, Any]]] = {}
    for card in payload.get("candidates") or []:
        by_kind.setdefault(str(card.get("issue_type") or "unknown"), []).append(card)
    for kind in sorted(by_kind):
        items = by_kind[kind]
        print(f"{kind}: {len(items)}")
        for card in items:
            print(f"  {card.get('file') or '-'} {card.get('card_id') or '-'}")


def closeout(
    *,
    target: Path,
    reason: str | None = None,
    defer: bool = False,
    dry_run: bool = False,
    json_output: bool = False,
) -> int:
    from . import memory_cmd

    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    try:
        config = memory_cmd._config_or_default(target)
        queue_path = memory_cmd._queue_path(target, config)
        queue = memory_cmd._load_json_file(queue_path)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"error: cannot read memory-care queue: {exc}", file=sys.stderr)
        return 2
    if not isinstance(queue, dict):
        return _print_blocked(
            f"error: memory-care queue is missing or not a JSON object at {queue_path}; closeout refused",
            json_output=json_output,
            candidate_count=0,
        )
    errors = memory_cmd._validate_queue(queue, path=queue_path)
    if errors:
        cards_value = queue.get("cards")
        n = len(cards_value) if isinstance(cards_value, list) else 0
        summary = "; ".join(errors[:5])
        return _print_blocked(
            f"error: memory-care queue failed validation; closeout refused. {summary}",
            json_output=json_output,
            candidate_count=n,
        )
    cards_value = queue.get("cards")
    cards = cards_value if isinstance(cards_value, list) else []
    reason_text = (reason or "").strip()
    block_message = _block_message(defer=defer, reason_text=reason_text, cards=cards)
    if dry_run:
        by_issue_type, candidates = _preview_cards(cards)
        preview = {
            "status": "dry-run",
            "dry_run": True,
            "would_write": False,
            "intended_status": "deferred" if defer else "reviewed",
            "would_block": block_message is not None,
            "block_reason": block_message,
            "reason": reason_text,
            "candidate_count": len(cards),
            "by_issue_type": by_issue_type,
            "candidates": candidates,
        }
        if json_output:
            print(json.dumps(preview, indent=2, sort_keys=True))
        else:
            _print_dry_run(preview)
        return 0
    if block_message:
        return _print_blocked(block_message, json_output=json_output, candidate_count=len(cards))
    fingerprints = [
        str(card.get("source_fingerprint"))
        for card in cards
        if isinstance(card, dict) and isinstance(card.get("source_fingerprint"), str)
    ]
    closeout_id = f"{_utc_iso().replace(':', '').replace('+', 'Z')}-memory-care-closeout"
    payload = {
        "closeout_id": closeout_id,
        "created_at": _utc_iso(),
        "status": "deferred" if defer else "reviewed",
        "reason": reason_text,
        "candidate_count": len(cards),
        "source_fingerprints": fingerprints,
        "safe_summary": f"{len(cards)} memory-care candidate(s) {'deferred' if defer else 'reviewed'}",
    }
    work_cmd._write_json(memory_cmd._closeouts_root(target) / closeout_id / "closeout.json", payload)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"memory_care_closeout: {closeout_id}")
    print(f"status: {payload['status']}")
    print(f"candidates: {payload['candidate_count']}")
    return 0
