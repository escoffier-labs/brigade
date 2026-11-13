"""Bounded automatic memory care and its CLI entry points."""

from __future__ import annotations

import json
import sys
import tomllib
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any, Sequence

from . import memory_archive, memory_care_hub, memory_care_journal as journal, memory_care_retire, memory_cmd
from . import memory_hub_store as store

STAGES = ("hub", "retire", "inbox")
STATE_REL = ".brigade/memory-care/tend/latest.json"


@dataclass(frozen=True)
class TendConfig:
    enabled: bool = True
    unused_after_days: int = 120
    deadline_days: int = 14
    max_archives_per_run: int = 20


def load_config(target: Path) -> TendConfig:
    path = target / ".brigade/memory-care.toml"
    table = tomllib.loads(path.read_text()).get("tend", {}) if path.exists() else {}
    if not isinstance(table, dict):
        raise ValueError("tend must be a table")
    values = asdict(TendConfig())
    for key, default in values.items():
        value = table.get(key, default)
        if type(value) is not type(default) or (type(value) is int and value < 1):
            raise ValueError(f"invalid tend.{key}")
        values[key] = value
    return TendConfig(**values)


@journal.serialized
def run(
    target: Path, *, stages: Sequence[str] | None = None, dry_run: bool = False, today: date | None = None
) -> dict[str, Any]:
    target = target.resolve()
    recovery = [] if dry_run else journal.resolve_prepared(target)
    tend = load_config(target)
    selected = tuple(stages) if stages is not None else STAGES
    if any(stage not in STAGES for stage in selected):
        raise ValueError("unknown tend stage")
    payload: dict[str, Any] = {"enabled": tend.enabled, "dry_run": dry_run, "stages": {}, "recovery": recovery}
    if not tend.enabled:
        return payload
    for stage in STAGES:
        if stage not in selected:
            continue
        if stage == "hub":
            payload["stages"][stage] = {} if dry_run else memory_care_hub.retry_pending(target)
        elif stage == "inbox":
            from .memory_inbox_drain import drain

            payload["stages"][stage] = drain(
                target, today=today or date.today(), deadline_days=tend.deadline_days, dry_run=dry_run
            )
        else:
            payload["stages"][stage] = memory_care_retire.apply(
                target, memory_cmd._config_or_default(target), tend, today=today or date.today(), dry_run=dry_run
            )
    if not dry_run:
        store.write_json(target / STATE_REL, payload)
    return payload


@journal.serialized
def undo(target: Path, decision_id: str) -> journal.Decision:
    recovery = journal.resolve_prepared(target)
    for item in recovery:
        if item["phase"] == "abandoned":
            print(f"memory care: abandoned {item['decision_id']}: {item['reason']}", file=sys.stderr)
    d = journal.load(target, decision_id)
    if d.kind == "undo":
        raise journal.JournalConflict("cannot undo an undo decision")
    if d.undone_by:
        return journal.load(target, d.undone_by)
    return (
        memory_archive.restore(target, d) if d.kind == "archive" else journal.revert(target, decision_id, reason="undo")
    )


def after_command(target: Path, *, stages: Sequence[str] | None = None) -> None:
    try:
        run(target, stages=stages)
    except (ValueError, OSError, journal.kernel.ProjectionError) as exc:
        print(f"memory care tend: {exc}", file=sys.stderr)


@journal.serialized
def archive_manifest(target: Path, path: Path, *, limit: int | None = None) -> dict[str, Any]:
    recovery = journal.resolve_prepared(target)
    if limit is not None and limit < 1:
        raise ValueError("limit must be positive")
    config = memory_cmd._config_or_default(target)
    decisions: list[str] = []
    warnings: list[dict[str, Any]] = []
    hub_results: dict[str, Any] = {}
    refused = []
    failed = 0
    seen: set[str] = set()
    with path.open(encoding="utf-8") as handle:
        for n, line in enumerate(handle, 1):
            if limit is not None and len(decisions) >= limit:
                break
            try:
                row = json.loads(line)
                card = row.get("card") or row.get("file")
                reason = row.get("reason")
                if not isinstance(card, str) or not isinstance(reason, str) or not reason.strip():
                    raise ValueError("each line needs card and reason")
                if card in seen:
                    raise ValueError("duplicate card")
                seen.add(card)
                d = memory_archive.archive_card(target, card, reason=reason, config=config)
                decisions.append(d.decision_id)
                warnings.extend(journal._payload(target, d.decision_id).get("index_warnings", []))
                hub_results[d.decision_id] = d.hub
            except (ValueError, OSError, AttributeError, journal.kernel.ProjectionError) as exc:
                if isinstance(exc, journal.kernel.ProjectionError) or isinstance(
                    exc.__cause__, journal.kernel.ProjectionError
                ):
                    failed += 1
                refused.append({"line": n, "reason": str(exc)})
    return {
        "archived": len(decisions),
        "decision_ids": decisions,
        "refused": refused,
        "recovery": recovery,
        "failed": failed,
        "warnings": warnings,
        "hub": hub_results,
    }


def dispatch(args: Any) -> int:
    try:
        if args.memory_care_command == "undo":
            payload = asdict(undo(args.target, args.decision_id))
        elif args.memory_care_command == "archive":
            payload = archive_manifest(args.target, args.from_file, limit=args.limit)
        else:
            payload = run(args.target, stages=args.stage, dry_run=args.dry_run)
        print(json.dumps(payload, sort_keys=True) if args.json else json.dumps(payload, indent=2))
        return 1 if payload.get("failed") else 0
    except (ValueError, OSError, journal.kernel.ProjectionError) as exc:
        print(f"memory care: {exc}", file=sys.stderr)
        return 1
