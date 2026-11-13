"""Fail-closed, digest-bound automatic metadata repairs with journal undo."""

from __future__ import annotations

import subprocess
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from . import memory_archive, memory_cmd as care, memory_care_journal as journal
from .card_fingerprint import _upsert_frontmatter_fields

# Every scanner kind has an explicit disposition; unknown kinds also fail closed.
FIX_POLICY = {
    "missing-freshness": (True, "Derived from the recorded review date and configured interval."),
    "missing-reviewed": (True, "Records only the last commit date or modification time outside Git."),
    "missing-index-link": (False, "A dead link needs a separate removal decision."),
    "stale": (False, "Requires retirement or evidence judgment."),
    "unconsolidated-evidence": (False, "Requires evidence judgment."),
    "expired": (False, "Requires retirement judgment."),
    "undersourced": (False, "Requires evidence."),
    "contradictory": (False, "Requires conflict judgment."),
    "orphaned-card": (False, "Indexes are curated and must not grow with every card."),
    "oversized-card": (False, "Requires content judgment."),
    "missing-frontmatter": (False, "Cannot infer metadata safely."),
    "missing-evidence-ref": (False, "Cannot invent evidence."),
}
AUTO_FIXES = frozenset(k for k, (apply, _) in FIX_POLICY.items() if apply)
MAX_CARD_BYTES = 65536


class FixRefused(ValueError):
    """Fixed reason words only, suitable for summaries."""


def read(target: Path, rel: str) -> bytes:
    try:
        from .memory_inbox_io import read as bounded_read

        data = bounded_read(target, rel, limit=MAX_CARD_BYTES)
        if data is None:
            raise FixRefused("missing")
        data.decode("utf-8")
        return data
    except (OSError, UnicodeError, ValueError) as exc:
        raise FixRefused("unreadable") from exc


def review_date(target: Path, rel: str, today: date) -> tuple[str, str]:
    try:
        result = subprocess.run(
            ["git", "-C", str(target), "rev-parse", "--is-inside-work-tree"], capture_output=True, timeout=15
        )
        if result.returncode == 0:
            value = care._git_last_commit_date(target, rel)
            source = "git-last-commit"
        else:
            value = datetime.fromtimestamp(journal.safe_path(target, rel).stat().st_mtime, tz=timezone.utc).date()
            source = "file-mtime"
    except (OSError, ValueError, OverflowError, subprocess.TimeoutExpired) as exc:
        raise FixRefused("date-unavailable") from exc
    if value is None:
        raise FixRefused("date-unavailable")
    if value > today:
        raise FixRefused("date-future")
    return value.isoformat(), source


def prepare(target: Path, config: care.MemoryCareConfig, item: dict[str, Any], today: date) -> dict[str, Any]:
    from .memory_care_plan import derived_fresh_until

    rel = str(item["card_file"])
    kind = str(item["issue_type"])
    if kind not in AUTO_FIXES:
        raise FixRefused("reported")
    if care._is_read_only(config, rel) or rel in config.index_paths or memory_archive.is_archived_path(rel):
        raise FixRefused("protected")
    # Only actual enumerated cards can be changed, even for forged saved scans.
    if rel not in {p.relative_to(target).as_posix() for p in care._iter_cards(target, config)}:
        raise FixRefused("not-card")
    before = read(target, rel)
    if len(before) > config.max_card_bytes:
        raise FixRefused("oversized")
    digest = journal.kernel.content_digest(before)
    if item.get("content_digest") and item["content_digest"] != digest:
        raise FixRefused("changed")
    text = before.decode("utf-8")
    if memory_archive.frontmatter_refusal(text):
        raise FixRefused("frontmatter-refused")
    lines = text.removeprefix("\ufeff").replace("\r\n", "\n").lstrip("\n").splitlines()
    keys = (
        [
            line.partition(":")[0].strip().strip("\"'").lower()
            for line in lines[1 : lines.index("---", 1)]
            if line and not line[0].isspace() and ":" in line
        ]
        if lines and lines[0] == "---"
        else []
    )
    aliases = (
        {"last_reviewed", "last_reviewed_at", "reviewed_at"},
        {"fresh_until", "freshness_until", "expires", "expires_at"},
    )
    if len(keys) != len(set(keys)) or any(len(set(keys) & group) > 1 for group in aliases):
        raise FixRefused("ambiguous")
    meta, header = care._parse_frontmatter(text)
    if not header:
        raise FixRefused("frontmatter-refused")
    result: dict[str, Any] = {"content_digest": digest, "candidate_fields": {}, "source": "derived", "before": before}
    if kind == "missing-reviewed":
        if care._frontmatter_value(meta, "last_reviewed", "last_reviewed_at", "reviewed_at"):
            raise FixRefused("already-present")
        value, result["source"] = review_date(target, rel, today)
        result["candidate_fields"] = {"last_reviewed": value}
    elif kind == "missing-freshness":
        if care._frontmatter_value(meta, "fresh_until", "freshness_until", "expires", "expires_at"):
            raise FixRefused("already-present")
        reviewed = care._parse_date(care._frontmatter_value(meta, "last_reviewed", "last_reviewed_at", "reviewed_at"))
        fresh = derived_fresh_until(reviewed, config.stale_after_days) if reviewed else None
        if fresh is None:
            raise FixRefused("freshness-date-not-representable" if reviewed else "requires-operator-freshness-date")
        result["candidate_fields"] = {"fresh_until": fresh}
    return result


@journal.serialized
def apply_fixes(
    target: Path,
    config: care.MemoryCareConfig,
    *,
    today: date,
    dry_run: bool = False,
    plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from .memory_care_plan import plan_payload
    from .memory_care_tend import load_config

    target = target.resolve()
    result: dict[str, Any] = dict(applied=0, skipped=0, decision_ids=[], reasons={})
    if not load_config(target).enabled:
        return result
    if not dry_run:
        journal.resolve_prepared(target)
    if plan is None:
        try:
            plan = plan_payload(target, config, care._scan_payload(target, config))
        except (OSError, ValueError, UnicodeError):
            result.update(skipped=1, reasons={"scan-unreadable": 1})
            return result
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in plan["items"]:
        if item.get("blockers"):
            reason = item["blockers"][0] if item["blockers"][0] in {"date-future", "date-unavailable"} else "blocked"
            result["skipped"] += 1
            result["reasons"][reason] = result["reasons"].get(reason, 0) + 1
            continue
        grouped.setdefault(item["card_file"], []).append(item)
    for rel, items in grouped.items():
        try:
            prepared = []
            for item in items:
                if not isinstance(item.get("content_digest"), str):
                    raise FixRefused("unbound-plan")
                prepared.append((item, prepare(target, config, item, today)))
            before = prepared[0][1]["before"]
            fields = {k: v for _, p in prepared for k, v in p["candidate_fields"].items()}
            after = _upsert_frontmatter_fields(before.decode("utf-8"), fields).encode("utf-8") if fields else before
            changes = [journal.Change(rel, before, after)]
            reason = ",".join(i["issue_type"] + ":" + p["source"] for i, p in prepared)
            if not dry_run:
                d = journal.commit_phased(
                    target,
                    kind="fix",
                    reason=reason,
                    card_id=items[0].get("card_id"),
                    changes=changes,
                    prepared_paths=[],
                    hub={},
                )
                if journal._payload(target, d.decision_id).get("status") != "committed":
                    raise FixRefused("changed")
                result["decision_ids"].append(d.decision_id)
            result["applied"] += 1
        except (FixRefused, OSError, ValueError, journal.kernel.ProjectionError) as exc:
            reason = str(exc) if isinstance(exc, FixRefused) else "conflict"
            result["skipped"] += 1
            result["reasons"][reason] = result["reasons"].get(reason, 0) + 1
    return result


def summary(payload: dict[str, Any]) -> str:
    reasons = ",".join(f"{key}={value}" for key, value in sorted(payload["reasons"].items())) or "none"
    return f"memory care fixes: applied={payload['applied']} skipped={payload['skipped']} reasons={reasons}"
