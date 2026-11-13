"""Fail-closed, digest-bound automatic metadata repairs with journal undo."""

from __future__ import annotations

import re
import subprocess
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from . import memory_archive, memory_cmd as care, memory_care_journal as journal

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


def plain_frontmatter(text: str) -> dict[str, str]:
    """Accept only independent physical fields, without interpreting general YAML."""
    text = text.removeprefix("\ufeff")
    normalized = text.replace("\r\n", "\n")
    if "\r" in normalized or ("\r\n" in text and "\n" in text.replace("\r\n", "")):
        raise FixRefused("frontmatter-refused")
    lines = normalized.split("\n")
    if not lines or lines[0] != "---" or "---" not in lines[1:]:
        raise FixRefused("frontmatter-refused")
    meta: dict[str, str] = {}
    seen: set[str] = set()
    aliases = (
        {"last_reviewed", "last_reviewed_at", "reviewed_at"},
        {"fresh_until", "freshness_until", "expires", "expires_at"},
    )
    for line in lines[1 : lines.index("---", 1)]:
        if "\t" in line:
            raise FixRefused("frontmatter-refused")
        if not line.strip() or line.startswith("#"):
            continue
        match = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_-]*):(?: +(.*)|$)", line)
        if not match:
            raise FixRefused("frontmatter-refused")
        key, value = match[1], match[2] or ""
        lowered = key.lower()
        if lowered in seen or any(lowered in group and seen & group for group in aliases):
            raise FixRefused("frontmatter-refused")
        seen.add(lowered)
        stack: list[str] = []
        quote = ""
        index = 0
        while index < len(value):
            char = value[index]
            if quote:
                if char == "\\" and quote == '"':
                    index += 2
                    continue
                if char == quote:
                    if quote == "'" and value[index : index + 2] == "''":
                        index += 2
                        continue
                    quote = ""
            elif char == "#" and (index == 0 or value[index - 1].isspace()):
                value = value[:index].rstrip()
                break
            elif char in "\"'":
                quote = char
            elif char in "[{":
                stack.append({"[": "]", "{": "}"}[char])
            elif char in "]}":
                if not stack or stack.pop() != char:
                    raise FixRefused("frontmatter-refused")
            elif char in "&*!|>" and (index == 0 or value[index - 1] in " [,{"):
                raise FixRefused("frontmatter-refused")
            elif char == ":" and not stack and (index + 1 == len(value) or value[index + 1].isspace()):
                raise FixRefused("frontmatter-refused")
            index += 1
        if quote or stack or value.startswith(("- ", "? ", "%", "@", "`")):
            raise FixRefused("frontmatter-refused")
        meta[lowered] = value.strip().strip("'\"")
    return meta


def plan_context(target: Path, config: care.MemoryCareConfig) -> tuple[set[str], bool | None]:
    """Enumerated cards, and whether the target is in a Git work tree (None when Git cannot be asked)."""
    cards = {p.relative_to(target).as_posix() for p in care._iter_cards(target, config)}
    inside_git: bool | None
    try:
        inside_git = (
            subprocess.run(
                ["git", "-C", str(target), "rev-parse", "--is-inside-work-tree"], capture_output=True, timeout=15
            ).returncode
            == 0
        )
    except (OSError, subprocess.TimeoutExpired):
        # Only a fix that needs a review date depends on this, so the failure stays with that fix.
        inside_git = None
    return cards, inside_git


def review_date(target: Path, rel: str, today: date, *, inside_git: bool | None = None) -> tuple[str, str]:
    try:
        if inside_git is None:
            inside_git = plan_context(target, care._config_or_default(target))[1]
        if inside_git is None:
            raise FixRefused("date-unavailable")
        if inside_git:
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
    if source == "file-mtime" and value == today:
        # A file written today, by an undo or anything else, says nothing about a review.
        raise FixRefused("date-too-recent")
    return value.isoformat(), source


def prepare(
    target: Path,
    config: care.MemoryCareConfig,
    item: dict[str, Any],
    today: date,
    *,
    context: tuple[set[str], bool | None] | None = None,
) -> dict[str, Any]:
    from .memory_care_plan import derived_fresh_until

    rel = str(item["card_file"])
    kind = str(item["issue_type"])
    if kind not in AUTO_FIXES:
        raise FixRefused("reported")
    if care._is_read_only(config, rel) or rel in config.index_paths or memory_archive.is_archived_path(rel):
        raise FixRefused("protected")
    # Only actual enumerated cards can be changed, even for forged saved scans.
    cards, inside_git = context if context is not None else plan_context(target, config)
    if rel not in cards:
        raise FixRefused("not-card")
    before = read(target, rel)
    if len(before) > config.max_card_bytes:
        raise FixRefused("oversized")
    digest = journal.kernel.content_digest(before)
    if item.get("content_digest") and item["content_digest"] != digest:
        raise FixRefused("changed")
    # An undone fix stays undone while the card is still as the undo left it.
    for decision in journal.entries(target):
        if decision.kind == "fix" and decision.undone_by and rel in decision.paths and f"{kind}:" in decision.reason:
            try:
                restored = journal.before_bytes(target, decision, rel)
            except (OSError, ValueError):
                restored = None
            if restored is None or restored == before:
                raise FixRefused("undone")
    text = before.decode("utf-8")
    meta = plain_frontmatter(text)
    if memory_archive.frontmatter_refusal(text):
        raise FixRefused("frontmatter-refused")
    result: dict[str, Any] = {"content_digest": digest, "candidate_fields": {}, "source": "derived", "before": before}
    if kind == "missing-reviewed":
        if any(key in meta for key in ("last_reviewed", "last_reviewed_at", "reviewed_at")):
            raise FixRefused("already-present")
        value, result["source"] = review_date(target, rel, today, inside_git=inside_git)
        result["candidate_fields"] = {"last_reviewed": value}
    elif kind == "missing-freshness":
        if any(key in meta for key in ("fresh_until", "freshness_until", "expires", "expires_at")):
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
    context = plan_context(target, config)
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in plan["items"]:
        if item.get("blockers"):
            reason = (
                item["blockers"][0]
                if item["blockers"][0] in {"date-future", "date-unavailable", "date-too-recent", "undone"}
                else "blocked"
            )
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
                prepared.append((item, prepare(target, config, item, today, context=context)))
            before = prepared[0][1]["before"]
            fields = {k: v for _, p in prepared for k, v in p["candidate_fields"].items()}
            # Insert only: preserve the BOM, every existing byte, and the physical newline style.
            newline = b"\r\n" if b"\r\n" in before else b"\n"
            lines = before.split(newline)
            closing = lines.index(b"---", 1)
            additions = [f"{key}: {value}".encode("utf-8") for key, value in fields.items()]
            after = newline.join(lines[:closing] + additions + lines[closing:])
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
