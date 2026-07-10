"""Handoff health checks shared by CLI doctors."""
# ruff: noqa: E402,F401,F403,F811,F821

from __future__ import annotations

import json
import hashlib
import re
import sys
import time
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import scrub
from ..budgets import HANDOFF_BACKLOG_STALE_SECONDS
from ..config import load_config as load_brigade_config
from ..localio import write_json as _write_json
from ..selection import WRITER_INBOXES as _WRITER_INBOX_MAP

from . import models as _family_base

globals().update({name: value for name, value in vars(_family_base).items() if not name.startswith("__")})


def migrate(*, target: Path, inbox: str | None = None, apply: bool = False, json_output: bool = False) -> int:
    """Convert near-miss homegrown handoff notes into the Brigade template.

    Pending notes that fail lint are parsed leniently (loose `- Type:` style
    metadata merged with any proper sections). Convertible notes are re-rendered
    through the standard draft template; originals are preserved under
    `migrated-originals/`. Injection-flagged notes are never converted. Dry-run
    by default.
    """
    from ..untrusted import scan_untrusted

    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    if inbox is not None:
        inbox_paths = [_draft_inbox_path(target, inbox)[0]]
    else:
        inbox_paths = [target / rel for rel in WRITER_INBOXES if (target / rel).is_dir()]
    items: list[dict[str, Any]] = []
    migrated = 0
    for inbox_path in inbox_paths:
        for path in sorted(inbox_path.glob("*.md")):
            if path.name == "TEMPLATE.md":
                continue
            if lint_file(path).valid:
                continue
            rel = str(path.relative_to(target))
            text = path.read_text(errors="replace")
            item: dict[str, Any] = {"file": rel}
            if scan_untrusted(text).flagged:
                item["status"] = "blocked-injection"
                item["detail"] = "carries prompt-injection signals; review manually before any conversion"
                items.append(item)
                continue
            extracted, missing = _migrate_extract(text)
            if missing:
                item["status"] = "unmigratable"
                item["missing"] = missing
                items.append(item)
                continue
            action = extracted["action"]
            rendered = _render_handoff_draft(
                handoff_type=extracted["type"],
                title=extracted["title"],
                summary=extracted["summary"],
                facts=[],
                evidence=[],
                action=action,
                target_card=extracted["target_card"] or None,
                target_document=extracted["target_document"] or None,
                suggested_content=extracted["card_content"]
                if action in CARD_ACTIONS
                else extracted["document_content"],
            )
            item["status"] = "migratable"
            item["action"] = action
            if apply:
                originals = inbox_path / "migrated-originals"
                originals.mkdir(parents=True, exist_ok=True)
                (originals / path.name).write_text(text)
                path.write_text(rendered)
                converted = lint_file(path)
                if not converted.valid:
                    path.write_text(text)
                    (originals / path.name).unlink()
                    item["status"] = "unmigratable"
                    item["missing"] = list(converted.errors)
                else:
                    item["status"] = "migrated"
                    migrated += 1
            items.append(item)
    receipt_path: Path | None = None
    if apply and migrated:
        from ..localio import utc_now, write_json

        migrations_dir = _handoff_state_root(target) / "migrations"
        migrations_dir.mkdir(parents=True, exist_ok=True)
        receipt_path = migrations_dir / f"{utc_now().strftime('%Y%m%dT%H%M%S')}.json"
        write_json(receipt_path, {"target": str(target), "migrated_count": migrated, "items": items})
    payload = {
        "target": str(target),
        "apply": apply,
        "item_count": len(items),
        "migratable_count": len([i for i in items if i["status"] in {"migratable", "migrated"}]),
        "migrated_count": migrated,
        "blocked_count": len([i for i in items if i["status"] == "blocked-injection"]),
        "unmigratable_count": len([i for i in items if i["status"] == "unmigratable"]),
        "receipt_path": str(receipt_path) if receipt_path else None,
        "items": items,
        "next_command": "brigade handoff migrate --apply"
        if not apply and any(i["status"] == "migratable" for i in items)
        else "brigade handoff lint",
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"handoff migrate: {target}")
    print(f"apply: {apply}")
    print(
        f"items: {len(items)} (migratable={payload['migratable_count']}, blocked={payload['blocked_count']}, unmigratable={payload['unmigratable_count']})"
    )
    for item in items[:15]:
        extra = f" missing: {', '.join(item['missing'][:4])}" if item.get("missing") else ""
        print(f"- {item['file']} [{item['status']}]{extra}")
    if receipt_path:
        print(f"receipt: {receipt_path}")
    print(f"next_command: {payload['next_command']}")
    return 0
