"""`brigade ingest` - route writer memory handoffs into canonical memory.

Conservative by design:
  - auto-promote handoffs with safe card filenames + YAML frontmatter
  - append-only routing for TOOLS.md, USER.md, rules/*.md, .learnings/*.md
  - everything ambiguous lands in memory/handoff-inbox/ for manual review
  - processed files move to the writer inbox's processed/ directory
"""
from __future__ import annotations

import re
import shutil
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List

from . import budgets
from .untrusted import scan_untrusted

SECTION_RE = re.compile(r"^##\s+(?P<name>.+?)\s*$", re.MULTILINE)
SAFE_CARD_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+\.md$")
SAFE_RULE_PATH_RE = re.compile(r"^rules/[A-Za-z0-9._-]+\.md$")
SAFE_SPECIAL_TARGETS = {
    "TOOLS.md",
    "USER.md",
    ".learnings/LEARNINGS.md",
    ".learnings/ERRORS.md",
    ".learnings/FEATURE_REQUESTS.md",
}

# Writer harness id -> inbox dir (mirror of install._WRITER_INBOX).
_WRITER_INBOXES = {
    "claude": ".claude/memory-handoffs",
    "codex": ".codex/memory-handoffs",
}

# Recognized handoff sections. Any section name outside this set is a signal
# that the parser split content at an internal `##` heading; route to inbox.
KNOWN_SECTIONS = {
    "type",
    "title",
    "summary",
    "durable facts",
    "evidence",
    "recommended memory action",
    "target card",
    "suggested card content",
    "target document",
    "suggested document content",
}


@dataclass
class IngestStats:
    processed: int = 0
    promoted: int = 0
    routed: int = 0
    inboxed: int = 0
    skipped: int = 0
    actions: List[str] = field(default_factory=list)


def _resolve_inbox_paths(target: Path) -> list[Path]:
    """Return handoff inbox directories for `target` in deterministic order.

    Reads `.brigade/config.json` when present and returns one inbox per
    writer harness in the selection (alphabetical by harness id). Falls back
    to the legacy `.claude/memory-handoffs/` path for pre-v0.3.0 installs.
    """
    from .config import load_config

    cfg = load_config(target)
    if cfg is None:
        legacy = target / ".claude" / "memory-handoffs"
        return [legacy] if legacy.is_dir() else []
    paths: list[Path] = []
    for h in sorted(cfg.selection.harnesses):
        rel = _WRITER_INBOXES.get(h)
        if rel and (target / rel).is_dir():
            paths.append(target / rel)
    return paths


def run(
    target: Path,
    dry_run: bool = False,
    promote_cards: bool = False,
    route_documents: bool = False,
    owner: Path | None = None,
) -> int:
    """Process handoffs.

    Reads handoffs from `target`'s writer inboxes. By default the resulting
    cards/documents and review drafts are written back into `target` itself.
    Pass `owner` to write them into a different canonical memory owner instead
    (the fleet model: many writer repos, one owner); processed handoffs are
    still archived in the source repo they came from.

    `promote_cards` and `route_documents` are opt-in. With neither flag,
    every handoff routes to the review inbox so a human picks the action.
    Match the cookbook wrapper by passing both flags explicitly.
    """
    source = target.expanduser().resolve()
    owner = source if owner is None else owner.expanduser().resolve()
    stats = IngestStats()
    rc = ingest_into(
        source=source,
        owner=owner,
        stats=stats,
        dry_run=dry_run,
        promote_cards=promote_cards,
        route_documents=route_documents,
    )
    if rc != 0:
        return rc
    _report(stats, dry_run=dry_run)
    return 0


def ingest_into(
    *,
    source: Path,
    owner: Path,
    stats: IngestStats,
    dry_run: bool = False,
    promote_cards: bool = False,
    route_documents: bool = False,
) -> int:
    """Ingest `source`'s handoffs into `owner`'s memory, accumulating `stats`.

    Returns 0 on success, 2 if `source` has no handoff inbox. Used directly by
    the fleet driver so it can sweep many sources into one owner and report once.
    """
    inbox_dir = owner / "memory" / "handoff-inbox"
    handoff_dirs = _resolve_inbox_paths(source)
    if not handoff_dirs:
        legacy = source / ".claude" / "memory-handoffs"
        print(f"brigade ingest: no handoff inbox at {legacy}", file=sys.stderr)
        return 2

    for handoffs_dir in handoff_dirs:
        processed_dir = handoffs_dir / "processed"
        for path in _list_handoffs(handoffs_dir):
            stats.processed += 1
            sections = parse(path)
            outcome = decide(
                sections,
                target=owner,
                promote_cards=promote_cards,
                route_documents=route_documents,
            )
            action = _execute(
                outcome,
                handoff_path=path,
                target=owner,
                sections=sections,
                inbox_dir=inbox_dir,
                processed_dir=processed_dir,
                dry_run=dry_run,
            )
            stats.actions.append(action.summary)
            if action.kind == "promoted":
                stats.promoted += 1
            elif action.kind == "routed":
                stats.routed += 1
            elif action.kind == "inboxed":
                stats.inboxed += 1
            elif action.kind == "skipped":
                stats.skipped += 1
    return 0


def parse(path: Path) -> Dict[str, str]:
    """Split a handoff into sections keyed by lowercased ## heading."""
    body = path.read_text()
    sections: Dict[str, str] = {}
    last_name = None
    last_pos = 0
    for m in SECTION_RE.finditer(body):
        if last_name is not None:
            sections[last_name.lower()] = body[last_pos : m.start()].strip()
        last_name = m.group("name")
        last_pos = m.end()
    if last_name is not None:
        sections[last_name.lower()] = body[last_pos:].strip()
    return sections


@dataclass
class Outcome:
    kind: str  # promoted | routed | inboxed | skipped
    dest: Path | None = None
    reason: str = ""


def _normalize_for_dedupe(text: str) -> str:
    """Collapse whitespace and drop blank lines for content-equality checks."""
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def _first_meaningful_line(text: str) -> str:
    """First non-blank, stripped line of `text`, or '' if there is none."""
    for line in text.splitlines():
        cleaned = line.strip()
        if cleaned:
            return cleaned
    return ""


def decide(
    sections: Dict[str, str],
    target: Path,
    promote_cards: bool,
    route_documents: bool,
) -> Outcome:
    action = sections.get("recommended memory action", "").strip().lower()

    stray = [s for s in sections if s not in KNOWN_SECTIONS]
    if stray:
        return Outcome(
            "inboxed",
            reason=f"unknown sections present (parser may have split content): {stray}",
        )

    if action in ("create-card", "update-card") and promote_cards:
        card = sections.get("target card", "").strip()
        content = sections.get("suggested card content", "")
        if not SAFE_CARD_NAME_RE.match(card):
            return Outcome("inboxed", reason=f"target card name unsafe: {card!r}")
        if not content.lstrip().startswith("---"):
            return Outcome("inboxed", reason="card content missing YAML frontmatter")
        sig = scan_untrusted(content)
        if sig.flagged:
            return Outcome(
                "inboxed",
                reason=f"injection signal in card content ({sig.count} marker(s)): {sig.markers[0]}",
            )
        return Outcome("promoted", dest=target / "memory" / "cards" / card)

    if action == "no-card" and route_documents:
        document = sections.get("target document", "").strip()
        content = sections.get("suggested document content", "")
        if not document:
            return Outcome("inboxed", reason="no-card handoff missing target document")
        if not (document in SAFE_SPECIAL_TARGETS or SAFE_RULE_PATH_RE.match(document)):
            return Outcome("inboxed", reason=f"target document not in safe list: {document!r}")
        if not content.strip():
            return Outcome("inboxed", reason="empty document content")
        if "\n## " in ("\n" + content):
            return Outcome(
                "inboxed",
                reason="document content contains `##` headings (would parse as new section)",
            )
        dest = target / document
        # Dedupe: never re-append content that is already present in the target.
        # Without this, a re-routed handoff duplicates its content on every run.
        existing = dest.read_text() if dest.is_file() else ""
        normalized_existing = _normalize_for_dedupe(existing)
        normalized_suggested = _normalize_for_dedupe(content)
        if normalized_suggested and normalized_suggested in normalized_existing:
            return Outcome("inboxed", reason=f"document content already present in {document}")
        anchor = _first_meaningful_line(content)
        if anchor and anchor in existing:
            return Outcome("inboxed", reason=f"anchor already present in {document}: {anchor}")
        # Bootstrap files load into the session prefix every turn and silently
        # truncate past ~12k chars. Refuse an append that would push the file
        # over its budget; route to the inbox so a human trims or re-homes it.
        addition = "\n\n" + content.strip() + "\n"
        would_exceed, budget = budgets.route_would_exceed_budget(dest, addition)
        if would_exceed:
            return Outcome(
                "inboxed",
                reason=(
                    f"bootstrap budget guard: appending to {document} would exceed "
                    f"its {budget}B budget; trim the file or promote to a memory card"
                ),
            )
        sig = scan_untrusted(content)
        if sig.flagged:
            return Outcome(
                "inboxed",
                reason=f"injection signal in document content ({sig.count} marker(s)): {sig.markers[0]}",
            )
        return Outcome("routed", dest=dest)

    if action == "":
        return Outcome("inboxed", reason="missing `Recommended memory action`")
    return Outcome("skipped", reason=f"action {action!r} not auto-handled")


@dataclass
class Action:
    kind: str
    summary: str


def _execute(
    outcome: Outcome,
    handoff_path: Path,
    target: Path,
    sections: Dict[str, str],
    inbox_dir: Path,
    processed_dir: Path,
    dry_run: bool,
) -> Action:
    name = handoff_path.name

    if outcome.kind == "promoted":
        dest = outcome.dest  # type: ignore[assignment]
        assert dest is not None
        content = sections.get("suggested card content", "").strip() + "\n"
        summary = f"promote → {dest.relative_to(target)}  ({name})"
        if not dry_run:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content)
            _archive(handoff_path, processed_dir)
        return Action("promoted", summary)

    if outcome.kind == "routed":
        dest = outcome.dest  # type: ignore[assignment]
        assert dest is not None
        content = sections.get("suggested document content", "").strip()
        summary = f"append → {dest.relative_to(target)}  ({name})"
        if not dry_run:
            dest.parent.mkdir(parents=True, exist_ok=True)
            with dest.open("a") as f:
                f.write("\n\n" + content + "\n")
            _archive(handoff_path, processed_dir)
        return Action("routed", summary)

    if outcome.kind in ("inboxed", "skipped"):
        slug = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        dest = inbox_dir / f"{slug}-{handoff_path.stem}.md"
        verb = "inbox" if outcome.kind == "inboxed" else "skip-inbox"
        summary = f"{verb} → {dest.relative_to(target)}  ({name}; {outcome.reason})"
        if not dry_run:
            inbox_dir.mkdir(parents=True, exist_ok=True)
            # Copy the original file verbatim so reviewers see what the
            # harness actually wrote, not a reconstruction.
            original = handoff_path.read_text()
            header = (
                f"<!-- routed from {handoff_path.name}\n"
                f"     reason: {outcome.reason}\n"
                f"     routed-at: {slug} -->\n\n"
            )
            dest.write_text(header + original)
            _archive(handoff_path, processed_dir)
        # Both inboxed and skipped are now archived; classify them uniformly.
        return Action("inboxed", summary)

    return Action("skipped", f"skip   {name}  ({outcome.reason})")


def _list_handoffs(handoffs_dir: Path) -> List[Path]:
    out: List[Path] = []
    for p in sorted(handoffs_dir.iterdir()):
        if p.is_file() and p.suffix == ".md" and p.name != "TEMPLATE.md":
            out.append(p)
    return out


def _archive(src: Path, processed_dir: Path) -> None:
    processed_dir.mkdir(parents=True, exist_ok=True)
    dest = processed_dir / src.name
    if dest.exists():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        dest = processed_dir / f"{src.stem}-{stamp}{src.suffix}"
    shutil.move(str(src), dest)


def _report(stats: IngestStats, dry_run: bool) -> None:
    tag = "[dry-run] " if dry_run else ""
    for line in stats.actions:
        print(f"  {tag}{line}")
    print()
    print(
        f"{tag}Processed {stats.processed}  Promoted {stats.promoted}  "
        f"Routed {stats.routed}  Inboxed {stats.inboxed}  Skipped {stats.skipped}"
    )
    if stats.processed == 0:
        print(f"{tag}NO_UPDATES")
