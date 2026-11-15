"""Policy-driven, reversible retirement of unused agent-authored cards."""

from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path
import re
import tomllib
from typing import Any

from . import memory_archive, memory_card_reads, memory_cmd, memory_review
from . import memory_care_journal as journal
from .memory_log_retractions import retraction


def policy(target: Path) -> dict[str, Any]:
    defaults = Path(__file__).parent / "templates/memory/lifecycle-policy.json"
    values = json.loads(defaults.read_text(encoding="utf-8"))
    path = target / ".brigade/memory-care.toml"
    table = tomllib.loads(path.read_text(encoding="utf-8-sig")).get("lifecycle", {}) if path.exists() else {}
    if not isinstance(table, dict) or set(table) - set(values):
        raise ValueError("invalid lifecycle policy")
    for key, default in values.items():
        value = table.get(key, default)
        if type(value) is not type(default) or (type(value) is int and value < 1):
            raise ValueError(f"invalid lifecycle.{key}")
        values[key] = value
    if values["archive_after_days"] <= values["stale_after_days"]:
        raise ValueError("lifecycle archive window must exceed stale window")
    return values


def _mentioned(text: str, rel: str, card_id: str) -> bool:
    # Covers Markdown links, wikilinks, command arguments and JSON lists.
    names = (rel, Path(rel).name, Path(rel).stem, card_id)
    return any(re.search(r"(?<![\w-])" + re.escape(name) + r"(?![\w-])", text) for name in names if name)


def _references(target: Path, cards: dict[str, str]) -> dict[str, str]:
    refs = dict(cards)
    roots = (
        ".brigade/care",
        ".brigade/runbooks",
        ".brigade/memory-care/runbooks",
        ".brigade/schedules",
        ".brigade/jobs",
        "runbooks",
        "docs/runbooks",
        "memory/runbooks",
    )
    paths = [p for root in roots for p in (target / root).rglob("*") if p.is_file()]
    paths.extend(
        p
        for p in (target / "memory").rglob("*")
        if p.is_file() and any("pin" in part.lower() for part in p.relative_to(target / "memory").parts)
    )
    for path in paths:
        rel = path.relative_to(target).as_posix()
        if memory_archive.is_archived_path(rel):
            continue
        safe = journal.safe_path(target, rel)
        refs[rel] = safe.read_text(encoding="utf-8")
    return refs


def _signals(target: Path) -> dict[str, date]:
    """Use rows only protect cards, even if a row's evidence is not scoreable."""
    result: dict[str, date] = {}
    for rel in (
        "memory/outcome/records.jsonl",
        ".brigade/memory/reflect/evidence-log.jsonl",
        ".brigade/memory/search-log.jsonl",
    ):
        path = journal.safe_path(target, rel)
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if retraction(row):
                continue
            if not isinstance(row, dict):
                raise ValueError(f"invalid use row in {rel}")
            stamp = row.get("ts") or row.get("timestamp") or row.get("recorded_at")
            day = memory_cmd._parse_date(str(stamp)[:10]) if stamp else None
            if not day:
                raise ValueError(f"undated use row in {rel}")
            keys = [row.get("artifact_id"), row.get("card_id"), row.get("card_path")]
            keys.extend(row.get("card_ids", []))
            keys.extend(row.get("card_aliases", []))
            for key in keys:
                if isinstance(key, str) and key:
                    result[key] = max(day, result.get(key, day))
    return result


@journal.serialized
def run(target: Path, *, today: date, dry_run: bool = False) -> dict[str, Any]:
    rules = policy(target)
    payload: dict[str, Any] = {"enabled": rules["enabled"], "transitions": [], "skipped": []}
    if not rules["enabled"]:
        return payload
    config = memory_cmd._config_or_default(target)
    cards = {
        p.relative_to(target).as_posix(): journal.safe_path(target, p.relative_to(target).as_posix())
        .read_bytes()
        .decode("utf-8")
        for p in memory_cmd._iter_cards(target, config)
    }
    refs = _references(target, cards)
    signals = _signals(target)
    if cards and not dry_run:
        # Start an observation window even when no reader has ever opened a card.
        memory_card_reads.record(target, [], today=today)
    ledger = memory_card_reads.load(target)
    opens = memory_review._opens(target)
    hub_reads = memory_card_reads.hub_last_reads(target, unused_after_days=rules["archive_after_days"])
    if hub_reads is None:
        return {**payload, "blocked": "hub read coverage unavailable"}
    digest = hashlib.sha256(json.dumps(rules, sort_keys=True).encode()).hexdigest()
    for rel, text in sorted(cards.items()):
        fm, _ = memory_cmd._parse_frontmatter(text.replace("\r\n", "\n"))
        identity = memory_cmd.card_identity_from_text(text, rel)
        reason = None
        if fm.get("trust") != "agent-unreviewed" or fm.get("author_type") == "human" or fm.get("author") == "human":
            reason = "human or unknown authorship"
        elif (
            fm.get("pinned")
            or memory_archive.frontmatter_refusal(text)
            or memory_cmd._is_read_only(config, rel)
            or rel in config.index_paths
        ):
            reason = "pinned or protected"
        elif any(other != rel and _mentioned(body, rel, identity) for other, body in refs.items()):
            reason = "referenced"
        if reason:
            payload["skipped"].append({"path": rel, "reason": reason})
            continue
        from .memory_hub_sync import publication_identity

        keys = (identity, rel, Path(rel).stem, publication_identity(text, rel)[0])
        days = [ledger.tracking_since, date.fromtimestamp((target / rel).stat().st_mtime)]
        for key in ("created", "created_at", "updated", "updated_at", "last_reviewed"):
            if day := memory_cmd._parse_date(fm.get(key)):
                days.append(day)
        for key in keys:
            days.extend(signals[k] for k in (key,) if k in signals)
            days.extend(hub_reads[k] for k in (key,) if k in hub_reads)
            days.extend(opens.get(key, (set(), set()))[1])
        if read := memory_card_reads.last_read(ledger, keys):
            days.append(read)
        age = (today - max(days)).days
        state_rel = f".brigade/memory/lifecycle/{hashlib.sha256(rel.encode()).hexdigest()}.json"
        before = journal.current(target, state_rel)
        previous = json.loads(before)["state"] if before else "active"
        desired = (
            "archived"
            if age >= rules["archive_after_days"]
            else "stale"
            if age >= rules["stale_after_days"]
            else "active"
        )
        # Record both lifecycle edges for a first run after the archive window.
        states = ["stale", "archived"] if previous == "active" and desired == "archived" else [desired]
        for state in states:
            if state == previous or len(payload["transitions"]) >= rules["max_transitions_per_run"]:
                continue
            metadata = {
                "actor": "policy",
                "policy_digest": digest,
                "from_state": previous,
                "to_state": state,
                "last_use": max(days).isoformat(),
                "card_path": rel,
            }
            after = (json.dumps({"state": state, "path": rel}, sort_keys=True) + "\n").encode()
            change = journal.Change(state_rel, before, after)
            entry = {**metadata, "decision_id": None}
            if not dry_run:
                if state == "archived":
                    decision = memory_archive.archive_card(
                        target,
                        rel,
                        reason="unused-card",
                        config=config,
                        policy_metadata=metadata,
                        state_change=change,
                        expected_before=text.encode("utf-8"),
                    )
                else:
                    decision = journal.commit_phased(
                        target,
                        kind="lifecycle",
                        reason="unused-card",
                        card_id=identity,
                        changes=[change],
                        prepared_paths=[state_rel],
                        hub={"status": "skipped"},
                        metadata=metadata,
                    )
                entry["decision_id"] = decision.decision_id
            payload["transitions"].append(entry)
            previous, before = state, after
    return payload
