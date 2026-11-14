"""`brigade ingest` - route writer memory handoffs into canonical memory.

Conservative by design:
  - auto-promote handoffs with safe card filenames + YAML frontmatter
  - append-only routing for TOOLS.md, USER.md, rules/*.md, .learnings/*.md
  - everything ambiguous lands in memory/handoff-inbox/ for manual review
  - processed files move to the writer inbox's processed/ directory
"""

from __future__ import annotations

from contextlib import contextmanager
import threading

import hashlib
import os
import re
import shutil
import stat
import subprocess
import sys
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List

from . import dirfd, handoff_worktrees, instruction_overflow
from .card_fingerprint import (
    CardMatch,
    ensure_card_id_frontmatter,
    ensure_fingerprint_frontmatter,
    find_card_match,
    index_cards,
    read_text_nofollow,
    reinforce_existing_card,
)
from .handoff_content import normalize_suggested_card_content
from .handoff_ingest_rules import (
    CARDS_RELATIVE,
    EXPECTED_SHA_SECTION,
    SHA256_RE,
    CardIdIndex,
    card_frontmatter_structure_problems,
    card_id_collision_problem,
    document_destination_problem,
    safe_card_name,
    read_card_bytes,
    safe_document_target,
    sha256_hex,
    sha_matches,
)
from .bounded_read import read_regular_file
from .instruction_inventory import INSPECTION_CEILING
from .selection import WRITER_INBOXES
from .untrusted import scan_untrusted

SECTION_RE = re.compile(r"^##\s+(?P<name>.+?)\s*$", re.MULTILINE)
_IDENTITY_MIGRATION_FIELDS = frozenset(
    {"aliases", "merged_from", "supersedes", "memory_proposal", "memory_proposal_sources"}
)

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
    EXPECTED_SHA_SECTION.lower(),
    "suggested card content",
    "target document",
    "suggested document content",
}

# ingest_into status codes. Fleet maps these to skip reasons; do not collapse
# a broken handoff-sources.json into the missing-inbox path.
NO_HANDOFF_INBOX = 2
INVALID_SOURCE_CONFIG = 3
SKIP_NO_HANDOFF_INBOX = "no handoff inbox"
SKIP_INVALID_SOURCE_CONFIG = "invalid handoff source config"

# Git state that would point `git -C <inbox>` at some other repository or
# stop discovery short of the one that owns the inbox.
_GIT_LOCATION_ENV = handoff_worktrees.GIT_LOCATION_ENV
_TRACKED_SKIP_CODE = "tracked-or-unverifiable"
_MAX_DRAFT_SUFFIX = 1000
_HANDLED_KINDS = frozenset({"promoted", "reinforced", "routed", "inboxed"})


class StaleCardError(OSError):
    """The card no longer matches the expected SHA a handoff was written against."""


class ChangedHandoffError(OSError):
    """The handoff's bytes differ from the snapshot that was delivered."""


class TrackedHandoffError(RuntimeError):
    """A handoff git tracks (or may track) must never be moved out of place."""


def skip_reason(rc: int) -> str | None:
    """Return the fleet skip reason for a non-success `ingest_into` status."""
    if rc == NO_HANDOFF_INBOX:
        return SKIP_NO_HANDOFF_INBOX
    if rc == INVALID_SOURCE_CONFIG:
        return SKIP_INVALID_SOURCE_CONFIG
    return None


def has_salvageable_structure(sections: Dict[str, str]) -> bool:
    """Return whether parsed Markdown has enough structure for manual recovery.

    Ingest preserves notes with multiple populated sections in the review inbox,
    even when their headings are not Brigade headings.  Keep this deliberately
    conservative so a single accidental heading is not treated like a structured
    handoff.
    """
    return sum(bool(body.strip()) for body in sections.values()) >= 2


@dataclass
class IngestStats:
    processed: int = 0
    promoted: int = 0
    reinforced: int = 0
    routed: int = 0
    inboxed: int = 0
    skipped: int = 0
    failed: int = 0
    actions: List[str] = field(default_factory=list)
    # sha256 of every handoff handled this run, so one handoff copied into
    # several worktree inboxes is ingested once.
    seen_digests: set[str] = field(default_factory=set)
    # Overflow cards that hold review drafts: [{"path", "problem", "held_drafts"}], also written to
    # `.brigade/memory-care/inbox/blocked-cards.json` for the report to read.
    blocked_cards: List[dict] = field(default_factory=list)

    @property
    def status(self) -> str:
        """ok, or partial when some handoffs failed and the rest still ran."""
        return "partial" if self.failed else "ok"


def _resolve_inbox_paths(target: Path) -> list[Path]:
    """Return handoff inbox directories for `target` in deterministic order.

    Reads `.brigade/config.json` when present and returns one inbox per
    writer harness in the selection (alphabetical by harness id). Falls back
    to the legacy `.claude/memory-handoffs/` path for pre-v0.3.0 installs.
    Also includes every existing inbox declared in `.brigade/handoff-sources.json`,
    including roots outside `target`, so a second configured root is ingested.
    """
    from .config import load_config

    paths: list[Path] = []
    seen: set[str] = set()
    declared: dict[Path, list[str]] = {}

    def add(path: Path, root: Path | None = None, rel: str | None = None) -> None:
        if root is not None and rel is not None:
            declared.setdefault(root, []).append(rel)
        if not path.is_dir():
            return
        resolved = path.resolve()
        key = str(resolved)
        if key in seen:
            return
        seen.add(key)
        paths.append(resolved)

    cfg = load_config(target)
    if cfg is None:
        add(target / ".claude" / "memory-handoffs", target, ".claude/memory-handoffs")
    else:
        for h in sorted(cfg.selection.harnesses):
            rel = WRITER_INBOXES.get(h)
            if rel:
                add(target / rel, target, rel)
    for root, rel in _handoff_source_inboxes(target):
        add(root / rel, root, rel)
    # Handoffs written in a linked worktree of a watched root are found
    # through `git worktree list`, never a directory walk.
    for root, rels in declared.items():
        # A root below its checkout's top level maps to the same subdirectory
        # in each linked checkout, walked without following links.
        prefix = handoff_worktrees.checkout_prefix(root)
        if prefix is None:
            continue
        for worktree in handoff_worktrees.linked_worktrees(root):
            for rel in dict.fromkeys(rels):
                inbox = handoff_worktrees.contained_inbox(worktree, "/".join((*prefix, rel)))
                if inbox is not None:
                    add(inbox)
    return paths


def _handoff_source_inbox_paths(target: Path) -> list[Path]:
    return [root / rel for root, rel in _handoff_source_inboxes(target)]


def _handoff_source_inboxes(target: Path) -> list[tuple[Path, str]]:
    """(root, inbox) pairs declared in `.brigade/handoff-sources.json`.

    Raises ValueError when the file exists but cannot be read or parsed, so
    ingest does not go green while omitting configured roots.
    """
    from .handoff_cmd.models import default_sources_path
    from .handoff_cmd.sources import _load_sources

    sources_path = default_sources_path(target)
    if not sources_path.is_file():
        # A dangling symlink or a non-file (e.g. a directory) at the config path
        # is a broken configuration, not an absent one: fail closed so ingest
        # does not go green while silently omitting configured roots.
        if sources_path.is_symlink() or sources_path.exists():
            raise ValueError(f"invalid handoff source config {sources_path}: not a regular file")
        return []
    try:
        config = _load_sources(target, sources_path)
    except (OSError, ValueError) as exc:
        raise ValueError(f"invalid handoff source config {sources_path}: {exc}") from exc
    return [(watched.root, watched.inbox) for watched in config.watched]


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

    Returns 0 on success, `NO_HANDOFF_INBOX` (2) if `source` has no handoff
    inbox, and `INVALID_SOURCE_CONFIG` (3) if `.brigade/handoff-sources.json`
    exists but cannot be loaded. The fleet driver maps those statuses through
    `skip_reason` so a broken config is not reported as a missing inbox.
    """
    inbox_dir = owner / "memory" / "handoff-inbox"
    try:
        handoff_dirs = _resolve_inbox_paths(source)
    except ValueError as exc:
        print(f"brigade ingest: {exc}", file=sys.stderr)
        return INVALID_SOURCE_CONFIG
    if not handoff_dirs:
        legacy = source / ".claude" / "memory-handoffs"
        print(f"brigade ingest: no handoff inbox at {legacy}", file=sys.stderr)
        return NO_HANDOFF_INBOX

    # Read the cards' ids once for the whole run. Each promoted card is added to it,
    # so two handoffs in one run cannot both claim an id.
    card_ids = CardIdIndex(owner)
    # Discovery reads several files and resolves every instruction path: once per run, not per handoff.
    routing = instruction_overflow.RouteContext(owner)
    for handoffs_dir in handoff_dirs:
        processed_dir = handoffs_dir / "processed"
        try:
            handoffs, tracked = _list_handoffs(handoffs_dir)
        except OSError:
            _record_failure(stats, handoffs_dir, "inbox-unreadable")
            continue
        for path in tracked:
            stats.skipped += 1
            stats.actions.append(_summary("skip", path.name, _TRACKED_SKIP_CODE))
        for path in handoffs:
            # One unreadable or unarchivable handoff is recorded and skipped;
            # it must not abort the rest of the batch.
            try:
                # One read. The digest, the parse and every delivered copy
                # come from these bytes, so the digest names what was handled.
                snapshot = path.read_bytes()
                sha = hashlib.sha256(snapshot).hexdigest()
                if sha in stats.seen_digests:
                    if not dry_run:
                        _archive(path, processed_dir, sha)
                    stats.skipped += 1
                    stats.actions.append(_summary("skip", path.name, "duplicate-content"))
                    continue
                # Bad bytes or malformed structure belong to this handoff
                # only. Guard and invariant failures further on stay fatal.
                try:
                    sections = parse(path, snapshot)
                except UnicodeDecodeError:
                    _record_failure(stats, path, "invalid-encoding")
                    continue
                except ValueError:
                    _record_failure(stats, path, "invalid-content")
                    continue
                outcome = decide(
                    sections,
                    target=owner,
                    promote_cards=promote_cards,
                    route_documents=route_documents,
                    card_ids=card_ids,
                    routing=routing,
                )
                action = _execute(
                    outcome,
                    handoff_path=path,
                    target=owner,
                    sections=sections,
                    inbox_dir=inbox_dir,
                    processed_dir=processed_dir,
                    dry_run=dry_run,
                    snapshot=snapshot,
                    card_ids=card_ids,
                    promote_cards=promote_cards,
                    route_documents=route_documents,
                    routing=routing,
                )
            except TrackedHandoffError:
                action = Action("skipped", _summary("skip", path.name, _TRACKED_SKIP_CODE))
            except ChangedHandoffError:
                # Changed since it was read: what was delivered is not what
                # sits here, so leave it pending and do not count the digest.
                _record_failure(stats, path, "handoff-changed")
                continue
            except OSError:
                _record_failure(stats, path, "io-error")
                continue
            # Only content that was published, appended or archived counts as
            # handled. A retryable skip leaves the handoff pending, so a later
            # identical copy must not be archived as its duplicate.
            if action.kind in _HANDLED_KINDS:
                stats.seen_digests.add(sha)
            stats.processed += 1
            stats.actions.append(action.summary)
            if action.kind == "promoted":
                stats.promoted += 1
            elif action.kind == "reinforced":
                stats.reinforced += 1
            elif action.kind == "routed":
                stats.routed += 1
            elif action.kind == "inboxed":
                stats.inboxed += 1
            elif action.kind == "skipped":
                stats.skipped += 1
    if not dry_run:
        from . import instruction_overflow_blocked

        # Level-triggered: recomputed from the review inbox every run, so a repaired card drops out.
        stats.blocked_cards = instruction_overflow_blocked.refresh(owner, routing=routing)
    return 0


def parse(path: Path, data: bytes | None = None) -> Dict[str, str]:
    """Split a handoff into sections keyed by lowercased ## heading."""
    body = data.decode("utf-8") if data is not None else path.read_text(encoding="utf-8")
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
    if "suggested card content" in sections:
        sections["suggested card content"], _ = normalize_suggested_card_content(sections["suggested card content"])
    return sections


@dataclass
class Outcome:
    kind: str  # promoted | reinforced | routed | routed-card | inboxed | skipped
    dest: Path | None = None
    reason: str = ""
    match: CardMatch | None = None
    # Stable reason code. `reason` can quote handoff fields and is never
    # written to logs, reports or review receipts; `code` is.
    code: str = ""
    # Set for `routed-card`, and for a hold on an overflow card: the card the append goes to and why.
    overflow: instruction_overflow.OverflowRoute | None = None


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
    card_ids: CardIdIndex | None = None,
    routing: instruction_overflow.RouteContext | None = None,
) -> Outcome:
    if "memory proposal notification" in sections:
        return Outcome(
            "inboxed",
            code="proposal-notification",
            reason="memory proposal notification requires explicit digest-bound review/apply",
        )
    action = sections.get("recommended memory action", "").strip().lower()

    stray = [s for s in sections if s not in KNOWN_SECTIONS]
    if stray:
        return Outcome(
            "inboxed",
            code="unknown-sections",
            reason=f"unknown sections present ({len(stray)}); the parser may have split content",
        )

    if action in ("create-card", "update-card") and promote_cards:
        card = sections.get("target card", "").strip()
        content = sections.get("suggested card content", "")
        content, fence_error = normalize_suggested_card_content(content)
        if not safe_card_name(card):
            return Outcome("inboxed", code="unsafe-card-name", reason="target card name unsafe")
        if fence_error:
            return Outcome("inboxed", code="card-fence-error", reason=fence_error)
        # The same parser and structure rules lint and draft use, so the three agree.
        structure = card_frontmatter_structure_problems(content)
        if structure:
            return Outcome("inboxed", code="card-frontmatter-invalid", reason="; ".join(structure))
        reserved = _identity_migration_fields(content.lstrip())
        if reserved:
            return Outcome(
                "inboxed",
                code="identity-migration-fields",
                reason=f"identity migration metadata requires review: {', '.join(reserved)}",
            )
        dest = target / "memory" / "cards" / card
        # Both actions can replace the named path. Do not let an ordinary
        # handoff discard reviewed migration keys, even under create-card.
        if any(path.is_symlink() for path in (target, target / "memory", dest.parent, dest)):
            return Outcome(
                "inboxed", code="card-destination-symlinked", reason="card destination is symlinked; requires review"
            )
        if dest.exists():
            if not dest.is_file():
                return Outcome(
                    "inboxed",
                    code="card-destination-not-regular",
                    reason="card destination is not a regular file; requires review",
                )
            try:
                governed = _identity_migration_fields(read_text_nofollow(dest))
            except OSError:
                return Outcome(
                    "inboxed",
                    code="card-destination-unreadable",
                    reason="cannot safely inspect card destination; requires review",
                )
            if governed:
                return Outcome(
                    "inboxed",
                    code="governed-card-migration",
                    reason=f"governed card identity migration requires review: {', '.join(governed)}",
                )
        # A stale base must not silently replace a card that changed after the
        # writer read it. The section is optional here, `handoff lint` requires it.
        expected = sections.get(EXPECTED_SHA_SECTION.lower(), "").strip().lower()
        if action == "update-card" and expected:
            current = read_card_bytes(target, card)
            if not SHA256_RE.fullmatch(expected):
                return Outcome(
                    "inboxed", code="expected-sha-invalid", reason=f"{EXPECTED_SHA_SECTION} is not 64 hex characters"
                )
            if current is not None and not sha_matches(current, expected):
                return Outcome(
                    "inboxed",
                    code="expected-sha-mismatch",
                    reason=f"update-card {EXPECTED_SHA_SECTION} does not match the current card",
                )
        sig = scan_untrusted(content)
        if sig.flagged:
            return Outcome(
                "inboxed",
                code="card-injection-signal",
                reason=f"injection signal in card content ({sig.count} marker(s))",
            )
        # create-card only: reinforce known facts instead of filing near-duplicates.
        # update-card keeps the wholesale-replace semantic for intentional edits.
        if action == "create-card":
            match_outcome = _decide_create_card_match(content, target=target)
            if match_outcome is not None:
                return match_outcome
            if not dest.exists():
                # The card is written with the id it holds, or a freshly minted one, so judge
                # it by that. An existing destination keeps its own id and is not a new card.
                collision = card_id_collision_problem(
                    card_ids if card_ids is not None else CardIdIndex(target),
                    ensure_card_id_frontmatter(content.strip() + "\n"),
                    card,
                )
                if collision:
                    return Outcome("inboxed", code="card-id-collision", reason=collision)
        return Outcome("promoted", code="card-promoted", dest=dest)

    if action == "no-card" and route_documents:
        document = sections.get("target document", "").strip()
        content = sections.get("suggested document content", "")
        if not document:
            return Outcome("inboxed", code="missing-target-document", reason="no-card handoff missing target document")
        if not safe_document_target(document):
            return Outcome("inboxed", code="unsafe-target-document", reason="target document not in safe list")
        # Read-only: a known instruction file past its act-at, behind a link, or with an unreadable
        # `[instructions]` table sends the append to its overflow card instead of a person.
        overflow = instruction_overflow.route(
            target, document, content, instruction_overflow.handoff_type(sections), context=routing
        )
        destination_problem = document_destination_problem(document, target)
        if destination_problem and overflow is None:
            return Outcome("inboxed", code="document-destination-unsafe", reason=destination_problem)
        if not content.strip():
            return Outcome("inboxed", code="empty-document-content", reason="empty document content")
        if "\n## " in ("\n" + content):
            return Outcome(
                "inboxed",
                code="document-has-headings",
                reason="document content contains `##` headings (would parse as new section)",
            )
        dest = target / document
        # Dedupe: never re-append content that is already present in the target.
        # Without this, a re-routed handoff duplicates its content on every run. A routed
        # append also looks in every overflow card of the file and type, so a block already
        # published to card 1 is found when card 2 would be chosen.
        # A known file's overflow cards are searched on both paths: a person may have trimmed the file
        # after a block was published to a card and before a replay.
        if overflow is None:
            existing = dest.read_text(encoding="utf-8") if dest.is_file() else ""
            held = [
                existing,
                *instruction_overflow.direct_card_text(
                    target, document, instruction_overflow.handoff_type(sections), context=routing
                ),
            ]
        else:
            # The file may be reached through a link: read it bounded and never write to it.
            read = read_regular_file(dest, max_bytes=INSPECTION_CEILING)
            existing = read.data.decode("utf-8", "replace") if read.data is not None else ""
            held = [existing, *instruction_overflow.existing_card_text(target, overflow)]
        normalized_suggested = instruction_overflow.normalize_for_dedupe(content)
        if normalized_suggested and any(
            normalized_suggested in instruction_overflow.normalize_for_dedupe(text) for text in held
        ):
            return Outcome(
                "inboxed",
                code="document-content-present",
                reason="document content already present in the target document",
            )
        # The anchor is a line match, and cards only grow: a line of an unrelated card must not hold
        # a new handoff forever, so only the file itself is searched.
        anchor = _first_meaningful_line(content)
        if anchor and anchor in existing:
            return Outcome(
                "inboxed",
                code="document-anchor-present",
                reason="document anchor already present in the target document",
            )
        sig = scan_untrusted(content)
        if sig.flagged:
            return Outcome(
                "inboxed",
                code="document-injection-signal",
                reason=f"injection signal in document content ({sig.count} marker(s))",
            )
        if overflow is not None:
            return _routed_card_outcome(target, overflow)
        return Outcome("routed", code="document-appended", dest=dest)

    if action == "":
        return Outcome("inboxed", code="missing-action", reason="missing `Recommended memory action`")
    return Outcome("skipped", code="action-not-auto-handled", reason="action not auto-handled")


def _decide_create_card_match(content: str, *, target: Path) -> Outcome | None:
    """Return reinforce / review Outcome when create-card matches an existing card."""
    indexed = index_cards(target / "memory" / "cards")
    match = find_card_match(content, indexed)
    if match is None:
        return None
    rel = match.path.relative_to(target).as_posix()
    if match.kind == "exact":
        return Outcome(
            "reinforced",
            code="card-reinforced",
            dest=match.path,
            reason=f"exact fingerprint match for {rel}",
            match=match,
        )
    similarity = f"{match.similarity:.2f}"
    if match.opposite_polarity:
        return Outcome(
            "inboxed",
            code="contradiction-candidate",
            reason=(f"contradiction candidate vs {rel} (similarity={similarity}, opposite polarity); do not reinforce"),
            match=match,
        )
    return Outcome(
        "inboxed",
        code="near-match-proposal",
        reason=(f"near-match reinforce proposal: {rel} (similarity={similarity}); reviewer veto before merging"),
        match=match,
    )


@dataclass
class Action:
    kind: str
    summary: str
    destination: str | None = None


def _read_card_child(parent: int, name: str) -> tuple[bytes, int] | None:
    """Exact bytes and mode of a card read relative to a held parent, or None when absent."""
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    if dirfd.posix_available():
        # Non-blocking, so a card swapped for a FIFO fails the type check instead of hanging the open.
        flags = dirfd.file_flags(flags | getattr(os, "O_NONBLOCK", 0))
    try:
        descriptor = dirfd.open_child_file(parent, name, flags, 0)
    except FileNotFoundError:
        return None
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise OSError("publication must be a single-link regular file")
        if dirfd.posix_available():
            os.set_blocking(descriptor, True)
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            return handle.read(), metadata.st_mode
    finally:
        if descriptor != -1:
            os.close(descriptor)


def _directory_identity(descriptor: int) -> tuple[int, int]:
    """Volume and file index of an open directory: `st_dev` and `st_ino` on POSIX and Windows."""
    metadata = os.fstat(descriptor)
    return metadata.st_dev, metadata.st_ino


def _assert_parent_unchanged(parent: int, parent_path: Path) -> None:
    """The held directory is still the one `parent_path` names.

    Windows directory handles allow delete sharing, so another process can rename the
    directory and put a different one at the path. Reopen the path and compare stable
    identities on every platform, so a publish into a moved directory is never reported
    as an update of the named card.
    """
    current = dirfd.open_directory_chain(parent_path)
    try:
        same = _directory_identity(parent) == _directory_identity(current)
    finally:
        os.close(current)
    if not same:
        raise OSError("publication parent changed")


def _write_card_descriptor_bound(
    dest: Path,
    target: Path,
    check_state: Callable[[bytes | None], str | None],
    build_content: Callable[[str | None], str],
) -> None:
    """Inspect and replace a card through one held parent directory, on POSIX and Windows.

    The parent is opened once without following links. The leaf is read, the staged copy
    is written and the staged copy is renamed over the leaf, all relative to that held
    handle. If no descriptor-relative backend exists this raises and nothing is written.
    """
    if not dirfd.available():
        raise OSError("no-follow descriptor operations are unavailable; refusing a path-based publish")
    root = target.resolve()
    parent_path = root / dest.parent.relative_to(target)
    name = dest.name
    parent = dirfd.open_directory_chain(parent_path, create=True)
    staged: str | None = None
    try:
        found = _read_card_child(parent, name)
        data, mode = found if found is not None else (None, None)
        content = build_content(check_state(data)).encode("utf-8")
        staged = f".{name}.{uuid.uuid4().hex[:12]}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        if dirfd.posix_available():
            flags = dirfd.file_flags(flags)
        descriptor = dirfd.open_child_file(parent, staged, flags, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            if mode is not None and hasattr(os, "fchmod"):
                os.fchmod(handle.fileno(), stat.S_IMODE(mode))
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        # Publication boundary: same directory, and the leaf is still the revision checked.
        _assert_parent_unchanged(parent, parent_path)
        again = _read_card_child(parent, name)
        if (again[0] if again is not None else None) != data:
            raise OSError("publication revision changed")
        dirfd.replace_children(parent, staged, name)
        staged = None
        dirfd.fsync_directory(parent)
    finally:
        try:
            if staged is not None:
                dirfd.unlink_child(parent, staged)
        except OSError:
            pass
        os.close(parent)


def _publish_promoted_card(
    dest: Path,
    target: Path,
    sections: Dict[str, str],
    *,
    dry_run: bool,
    expected_sha: str = "",
    card_ids: CardIdIndex | None = None,
) -> str:
    """Publish (or, in a dry run, simulate) one card and record its id in `card_ids`.

    The card is recorded as soon as the write returns, before the caller archives the
    handoff, so a failed archive cannot leave the index without a card that is on disk.
    """
    from contextlib import nullcontext

    from .card_identity import valid_card_id
    from .inbox_lock import held_file_lock

    def parent_fence(path: Path) -> None:
        if any(parent.is_symlink() for parent in (path.parent, *path.parents)):
            raise OSError("publication parent is symlinked")

    def check_state(data: bytes | None) -> str | None:
        """Decode the exact bytes, refuse a governed card or a stale SHA, return the text."""
        text = None if data is None else data.decode("utf-8", errors="replace")
        if text is not None and _identity_migration_fields(text):
            raise OSError("governed destination requires review")
        # decide() compared the SHA without the lock. Hash the exact bytes read here, the
        # same bytes the boundary probe compares, so a card edited since then is never
        # replaced by a stale handoff.
        if expected_sha and (data is None or sha256_hex(data) != expected_sha):
            raise StaleCardError("expected card SHA256 no longer matches the card")
        return text

    built = ""

    def build_content(raw: str | None) -> str:
        nonlocal built
        content = ensure_fingerprint_frontmatter(sections.get("suggested card content", "").strip() + "\n")
        existing, _ = _frontmatter(raw or "")
        existing_id = valid_card_id(existing.get("id")) or valid_card_id(existing.get("card_id"))
        built = ensure_card_id_frontmatter(content, preserve_id=existing_id)
        return built

    lock_path = target / ".brigade/memory/proposals/proposal.lock"
    if not dry_run:
        parent_fence(lock_path)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
    # The shared helper has the established Windows fallback. Ordinary ingest
    # does not require proposal IO's stricter directory-descriptor capability.
    with nullcontext() if dry_run else held_file_lock(lock_path, deadline_seconds=1):
        if dry_run:
            # Read-only preview: report what a real run would refuse, write nothing.
            parent_fence(dest)
            if dest.is_symlink() or (dest.exists() and not dest.is_file()):
                raise OSError("publication must be a regular file")
            build_content(check_state(dest.read_bytes() if dest.exists() else None))
        else:
            _write_card_descriptor_bound(dest, target, check_state, build_content)
    if card_ids is not None:
        # `built` is the text written, or in a dry run the text the same builder would
        # write, so an update-card that omits an id keeps the destination's id in both.
        card_ids.register((CARDS_RELATIVE / dest.name).as_posix(), built)

    return built


_DOCUMENT_HELD = threading.local()


@contextmanager
def _document_lock(target: Path):
    from .inbox_lock import held_file_lock
    from .memory_care_journal import safe_path

    target = target.resolve()
    held: set[Path] = getattr(_DOCUMENT_HELD, "targets", set())
    if target in held:
        yield
        return
    lock = safe_path(target, ".brigade/memory-care/document.lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    with held_file_lock(lock, deadline_seconds=1):
        _DOCUMENT_HELD.targets = held | {target}
        try:
            yield
        finally:
            _DOCUMENT_HELD.targets = held


class AppendRefused(OSError):
    """The opened destination is not what the append was decided against. Nothing was written."""

    def __init__(self, code: str, reason: str, *, full: bool = False, over_limit: bool = False) -> None:
        super().__init__(reason)
        self.code, self.reason, self.full, self.over_limit = code, reason, full, over_limit


@dataclass(frozen=True)
class AppendExpect:
    """What the destination must be when it is opened, checked on the opened descriptor under the lock.

    `created` True opens it exclusively (it must not exist), False refuses a missing file, None accepts
    either. `identity` is the (st_dev, st_ino) the opened file must have; a pinned file may already have
    several hard links, an unpinned one must have exactly one. `max_size` caps the file after the append.
    `check` sees the exact bytes of the opened file (None for a file this open created).
    """

    created: bool | None = None
    identity: tuple[int, int] | None = None
    max_size: int | None = None
    check: Callable[[bytes | None], instruction_overflow.WriteProblem | None] | None = None


def _append_document_nofollow(target: Path, dest: Path, text: str, *, expect: AppendExpect | None = None) -> None:
    from .inbox_lock import InboxLockTimeout

    try:
        with _document_lock(target):
            _append_document_locked(target, dest, text, expect=expect)
    except InboxLockTimeout as exc:
        # Plain ingest catches OSError and preserves the source in its review inbox.
        raise OSError("document publication busy") from exc


def _opened_bytes(descriptor: int, size: int) -> bytes:
    os.lseek(descriptor, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    while size > 0 and (chunk := os.read(descriptor, min(size, 1 << 16))):
        chunks.append(chunk)
        size -= len(chunk)
    return b"".join(chunks)


def _append_document_locked(target: Path, dest: Path, text: str, *, expect: AppendExpect | None = None) -> None:
    """Append to a routed document without following a link anywhere below the workspace.

    The parent directory is opened by descriptor from the resolved workspace root, and
    the leaf is opened relative to it with no-follow, through the POSIX or the Windows
    handle-relative backend. When neither backend is available this raises OSError and
    nothing is written: a path-based append cannot close the check-then-write race.

    Every invariant in `expect` is enforced on the opened descriptor, so the bytes checked are the
    bytes appended to. A mismatch raises `AppendRefused` and writes nothing.
    """
    if not dirfd.available():
        raise OSError("no-follow descriptor operations are unavailable; refusing a path-based append")
    root = target.resolve()
    relative = dest.relative_to(target).as_posix()
    problem = document_destination_problem(relative, root)
    if problem:
        raise OSError(problem)
    resolved = root / relative
    data = text.encode("utf-8")
    created = None if expect is None else expect.created
    flags = os.O_WRONLY | os.O_APPEND | getattr(os, "O_BINARY", 0)
    if expect is not None and expect.check is not None:
        flags = (flags & ~os.O_WRONLY) | os.O_RDWR  # the opened file is read back before it is written
    if created is not False:
        flags |= os.O_CREAT
    if created is True:
        flags |= os.O_EXCL
    if dirfd.posix_available():
        # O_NOFOLLOW; the Windows backend refuses reparse points itself. O_NONBLOCK makes a FIFO
        # fail the open (no reader) or the type check below instead of hanging.
        flags = dirfd.file_flags(flags | getattr(os, "O_NONBLOCK", 0))
    parent = dirfd.open_directory_chain(resolved.parent, create=True)
    made = False
    try:
        try:
            descriptor = dirfd.open_child_file(parent, resolved.name, flags, 0o644)
        except FileExistsError as exc:
            raise AppendRefused(
                "document-destination-unwritable", "the destination appeared since it was decided"
            ) from exc
        except FileNotFoundError as exc:
            if created is False:
                raise AppendRefused(
                    "document-destination-unwritable", "the destination disappeared since it was decided"
                ) from exc
            raise
        made = created is True
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode):
                raise OSError("target document is not a regular file")
            pinned = expect is not None and expect.identity is not None
            if opened.st_nlink != 1 and not pinned:
                # A hard link swapped in after the destination check would send this append to another file.
                raise OSError("target document has more than one hard link")
            if pinned:
                assert expect is not None and expect.identity is not None
                if opened.st_ino and expect.identity[1] and (opened.st_dev, opened.st_ino) != expect.identity:
                    raise AppendRefused("document-destination-unwritable", "the destination was replaced")
            if expect is not None:
                if expect.max_size is not None and opened.st_size + len(data) > expect.max_size:
                    raise AppendRefused(
                        "document-destination-unwritable",
                        "the append would pass the size limit of the destination",
                        over_limit=True,
                    )
                if expect.check is not None:
                    if opened.st_size > INSPECTION_CEILING:
                        raise AppendRefused("overflow-card-untrusted", "the card is too large to be checked")
                    refused = expect.check(None if made else _opened_bytes(descriptor, opened.st_size))
                    if refused is not None:
                        raise AppendRefused(refused.code, refused.reason, full=refused.full)
            if dirfd.posix_available():
                os.set_blocking(descriptor, True)
            made = False  # the file is ours to keep once the append starts
            with os.fdopen(descriptor, "ab") as handle:
                descriptor = -1
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            if made:
                # This open created the file and nothing was written: do not leave an empty card behind.
                closing, descriptor = descriptor, -1
                try:
                    os.close(closing)
                    dirfd.unlink_child(parent, resolved.name)
                except OSError:
                    pass
            raise
        finally:
            if descriptor != -1:
                os.close(descriptor)
    finally:
        os.close(parent)


def _execute(
    outcome: Outcome,
    handoff_path: Path,
    target: Path,
    sections: Dict[str, str],
    inbox_dir: Path,
    processed_dir: Path,
    dry_run: bool,
    snapshot: bytes | None = None,
    card_ids: CardIdIndex | None = None,
    promote_cards: bool | None = None,
    route_documents: bool | None = None,
    routing: instruction_overflow.RouteContext | None = None,
) -> Action:
    from .inbox_lock import InboxLockTimeout
    from .memory_evidence_log import log_match

    name = handoff_path.name
    snapshot_sha = hashlib.sha256(snapshot).hexdigest() if snapshot is not None else None
    # Re-check before destination writes as well as at the final archive boundary.
    if not dry_run and _is_tracked(handoff_path, _git_tracked_files(handoff_path.parent)):
        raise TrackedHandoffError(f"refusing to ingest git-tracked handoff {handoff_path}")

    if outcome.kind == "promoted":
        dest = outcome.dest
        assert dest is not None
        expected_sha = ""
        if sections.get("recommended memory action", "").strip().lower() == "update-card":
            expected_sha = sections.get(EXPECTED_SHA_SECTION.lower(), "").strip().lower()
        try:
            _publish_promoted_card(
                dest, target, sections, dry_run=dry_run, expected_sha=expected_sha, card_ids=card_ids
            )
        except InboxLockTimeout:
            return Action("skipped", _summary("skip", name, "card-publication-busy"))
        except (OSError, ValueError) as exc:
            # A reviewed migration may have arrived after decide(). Route the
            # original handoff for review without replacing that revision.
            stale = isinstance(exc, StaleCardError)
            code = "expected-sha-stale" if stale else "card-publication-changed"
            reason = (
                f"update-card {EXPECTED_SHA_SECTION} no longer matches the card; requires review"
                if stale
                else "card publication changed or became governed; requires review"
            )
            return _execute(
                Outcome("inboxed", code=code, reason=reason),
                handoff_path,
                target,
                sections,
                inbox_dir,
                processed_dir,
                dry_run,
                snapshot,
                promote_cards=promote_cards,
                route_documents=route_documents,
            )
        summary = _summary("promote", name, outcome.code)
        if not dry_run:
            _archive(handoff_path, processed_dir, snapshot_sha)
        return Action("promoted", summary)

    if outcome.kind == "reinforced":
        dest = outcome.dest
        assert dest is not None
        summary = _summary("reinforce", name, outcome.code, _match_meta(outcome, target))
        if not dry_run:
            evidence_pointer = _evidence_pointer(handoff_path, sections=sections, target=target)
            reinforce_existing_card(
                dest,
                today=date.today(),
                evidence_pointer=evidence_pointer,
            )
            log_match(target, outcome.match, _archive(handoff_path, processed_dir, snapshot_sha))
        return Action("reinforced", summary)

    if outcome.kind == "routed-card":
        return _execute_routed_card(
            outcome,
            handoff_path,
            target,
            sections,
            inbox_dir,
            processed_dir,
            dry_run,
            snapshot,
            card_ids,
            promote_cards,
            route_documents,
        )

    if outcome.kind == "routed":
        dest = outcome.dest  # type: ignore[assignment]
        assert dest is not None
        content = sections.get("suggested document content", "").strip()
        summary = _summary("append", name, outcome.code)
        if not dry_run:
            try:
                rerouted = _append_direct(target, dest, content, sections, routing)
            except OSError:
                return _execute(
                    Outcome(
                        "inboxed",
                        code="document-destination-unwritable",
                        reason="document destination is linked or unwritable; requires review",
                    ),
                    handoff_path,
                    target,
                    sections,
                    inbox_dir,
                    processed_dir,
                    dry_run,
                    snapshot,
                    promote_cards=promote_cards,
                    route_documents=route_documents,
                )
            if rerouted is not None:
                # The file reached its limit between the decision and the locked write: same run, same handoff.
                return _execute(
                    rerouted,
                    handoff_path,
                    target,
                    sections,
                    inbox_dir,
                    processed_dir,
                    dry_run,
                    snapshot,
                    card_ids,
                    promote_cards,
                    route_documents,
                    routing,
                )
            _archive(handoff_path, processed_dir, snapshot_sha)
        return Action("routed", summary)

    if outcome.kind in ("inboxed", "skipped"):
        slug = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        # Ordinary names pass through unchanged; anything else is replaced.
        stem = re.sub(r"[^A-Za-z0-9._-]", "_", handoff_path.stem)[:120]
        verb = "inbox" if outcome.kind == "inboxed" else "skip-inbox"
        summary = _summary(verb, name, outcome.code, _match_meta(outcome, target))
        if not dry_run:
            inbox_dir.mkdir(parents=True, exist_ok=True)
            # Copy the original file verbatim so reviewers see what the
            # harness actually wrote, not a reconstruction.
            original = snapshot.decode("utf-8") if snapshot is not None else handoff_path.read_text(encoding="utf-8")
            header = (
                f"<!-- routed from {handoff_worktrees.safe_text(handoff_path.name)}\n"
                f"     reason: {outcome.code}{_match_meta(outcome, target)}\n     routed-at: {slug} -->\n\n"
            )
            if promote_cards is not None and route_documents is not None:
                flags = f"promote={str(promote_cards).lower()} route={str(route_documents).lower()}"
                header = header.replace(f"routed-at: {slug} -->", f"routed-at: {slug}\n     retry-flags: {flags} -->")
            dest = _write_review_draft(inbox_dir, f"{slug}-{stem}", header + original)
            log_match(target, outcome.match, dest)
            _archive(handoff_path, processed_dir, snapshot_sha)
        # Both inboxed and skipped are now archived; classify them uniformly.
        return Action("inboxed", summary)

    return Action("skipped", _summary("skip", name, outcome.code))


def _append_direct(
    target: Path,
    dest: Path,
    content: str,
    sections: Dict[str, str],
    routing: instruction_overflow.RouteContext | None,
) -> Outcome | None:
    """Append to the document itself, or return the overflow route when it no longer fits.

    Holding the document lock it measures the file again (two ingest runs can both have decided
    that a block fits), pins the file's identity, and appends with the limit enforced on the
    opened file, so the file never passes act-at and a swapped-in link is never written to.
    """
    document = dest.relative_to(target).as_posix()
    kind = instruction_overflow.handoff_type(sections)
    with _document_lock(target):
        overflow = instruction_overflow.route(target, document, content, kind, context=routing)
        if overflow is not None:
            return _routed_card_outcome(target, overflow)
        pin = instruction_overflow.direct_pin(target, document, context=routing)
        try:
            _append_document_nofollow(
                target,
                dest,
                instruction_overflow.append_block(content).decode("utf-8"),
                expect=AppendExpect(created=pin.identity is None, identity=pin.identity, max_size=pin.act_at),
            )
        except AppendRefused as exc:
            if not exc.over_limit:
                raise
            overflow = instruction_overflow.route(target, document, content, kind, context=routing)
            if overflow is None:
                raise
            return _routed_card_outcome(target, overflow)
    return None


def _routed_card_outcome(target: Path, overflow: instruction_overflow.OverflowRoute) -> Outcome:
    held = overflow.hold()
    if held is not None:
        return Outcome("inboxed", code=held.code, reason=held.reason, overflow=overflow)
    return Outcome(
        "routed-card",
        code="document-routed-to-card",
        dest=target / overflow.card_rel,
        reason="append goes to the file's private overflow card",
        overflow=overflow,
    )


def _execute_routed_card(
    outcome: Outcome,
    handoff_path: Path,
    target: Path,
    sections: Dict[str, str],
    inbox_dir: Path,
    processed_dir: Path,
    dry_run: bool,
    snapshot: bytes | None,
    card_ids: CardIdIndex | None,
    promote_cards: bool | None,
    route_documents: bool | None,
) -> Action:
    """Append the handoff's content to its file's overflow card through the journal."""
    from .inbox_lock import InboxLockTimeout
    from .memory_care_journal import JournalConflict, ReadOnlyPath
    from .memory_inbox_append import CardFull, commit_card_append

    overflow = outcome.overflow
    assert overflow is not None and outcome.dest is not None
    name = handoff_path.name
    snapshot_sha = hashlib.sha256(snapshot).hexdigest() if snapshot is not None else None
    summary = _summary("card-append", name, outcome.code)
    if dry_run:
        return Action("routed", summary)
    content = sections.get("suggested document content", "")
    kind = instruction_overflow.handoff_type(sections)
    duplicate = False
    held: Outcome | None = None
    try:
        # A card can fill between choosing it and writing to it (another run, or a person): the commit says so
        # and the next pass chooses the next card. Three passes is a bound, not an expected count.
        for _pass in range(3):
            # The document lock comes first, as retry and revert take it, then the journal.
            with _document_lock(target):
                # decide() ran without the lock: read the cards again, so the block matches the card now,
                # the card is still one this feature created, and a concurrent run that already
                # published this content is not written over.
                overflow = instruction_overflow.refresh(target, overflow, content, kind)
                problem = overflow.hold()
                if problem is not None:
                    held = Outcome("inboxed", code=problem.code, reason=problem.reason, overflow=overflow)
                    break
                if instruction_overflow.contains_block(target, overflow, content):
                    duplicate = True
                    break
                block, header_bytes = instruction_overflow.card_block(
                    target, overflow, content, kind, today=date.today()
                )
                try:
                    commit_card_append(
                        target,
                        overflow.card_rel,
                        block,
                        header_bytes=header_bytes,
                        reason="ingest:document-routed-to-card",
                        card_limit=overflow.card_limit,
                        limit_requested=overflow.limit_requested,
                    )
                except CardFull:
                    continue
                except ValueError as exc:
                    if isinstance(exc, JournalConflict):
                        raise
                    # A protected card path, or a care configuration that does not parse. Both are
                    # retry codes: the drain delivers the handoff once the cause is fixed.
                    cause = (
                        "the card path is protected"
                        if isinstance(exc, ReadOnlyPath)
                        else "the memory care configuration could not be parsed"
                    )
                    held = Outcome(
                        "inboxed",
                        code="document-destination-unwritable",
                        reason=f"{cause}; the handoff is retried automatically once that is fixed",
                    )
                break
        else:
            held = Outcome(
                "inboxed",
                code="document-destination-unwritable",
                reason="overflow cards kept filling while the handoff was written; it is retried automatically",
            )
    except AppendRefused as exc:
        # The card was not what the decision recorded when the bytes were about to be written: nothing was.
        held = Outcome("inboxed", code=exc.code, reason=exc.reason)
    except (OSError, InboxLockTimeout, JournalConflict):
        held = Outcome(
            "inboxed",
            code="document-destination-unwritable",
            reason="overflow card is linked, protected or unwritable; requires review",
        )
    if held is not None:
        return _execute(
            held,
            handoff_path,
            target,
            sections,
            inbox_dir,
            processed_dir,
            dry_run,
            snapshot,
            promote_cards=promote_cards,
            route_documents=route_documents,
        )
    if duplicate:
        return _execute(
            Outcome("inboxed", code="document-content-present", reason="content already in an overflow card"),
            handoff_path,
            target,
            sections,
            inbox_dir,
            processed_dir,
            dry_run,
            snapshot,
            promote_cards=promote_cards,
            route_documents=route_documents,
        )
    if card_ids is not None:
        try:
            card_ids.register(overflow.card_rel, (target / overflow.card_rel).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            pass  # the id index only guards later promotions in this run
    # Outside the try: an archive failure after publication is an io-error and the handoff
    # stays, so the duplicate check finds the block in the card on the next run.
    _archive(handoff_path, processed_dir, snapshot_sha)
    return Action("routed", summary)


def _frontmatter(text: str) -> tuple[dict[str, str], bool]:
    """Small local reader for preservation of a pre-existing stable ID."""
    if not text.startswith("---"):
        return {}, False
    fields: dict[str, str] = {}
    lines = text.splitlines()
    for line in lines[1:]:
        if line.strip() == "---":
            return fields, True
        if ":" in line:
            key, value = line.split(":", 1)
            fields[key.strip()] = value.strip().strip("'\"")
    return {}, False


def _identity_migration_fields(text: str) -> list[str]:
    fields, _ = _frontmatter(text)
    # Quoted YAML keys carry the same authority, including empty values.
    keys = {key.strip("'\"") for key in fields}
    return sorted(keys & _IDENTITY_MIGRATION_FIELDS)


def _evidence_pointer(handoff_path: Path, *, sections: Dict[str, str], target: Path) -> str:
    """Prefer an on-disk handoff path; fall back to the first evidence bullet."""
    try:
        return handoff_path.resolve().relative_to(target.resolve()).as_posix()
    except ValueError:
        pass
    evidence = sections.get("evidence", "")
    for line in evidence.splitlines():
        cleaned = line.strip().lstrip("-").strip()
        if cleaned:
            return cleaned
    return handoff_path.name


def _warn_tracking_check_failed(directory: Path, detail: str) -> None:
    # Otherwise a persistent git error reads like ordinary tracked-file skips.
    print(
        f"brigade ingest: git tracking check failed for {directory} ({detail}); leaving its handoffs in place",
        file=sys.stderr,
    )


def _git_tracked_names(directory: Path) -> set[str] | None:
    """Return names tracked by any work tree containing `directory`.

    An absent git binary or a directory outside any work tree yields an empty
    set. Any other failure returns None so callers can fail closed.
    """
    # Rescue relies on this check too: no injected config or location.
    env = handoff_worktrees.sanitized_git_env()
    env["GIT_DISCOVERY_ACROSS_FILESYSTEM"] = "true"
    try:
        directory = directory.resolve()
        try:
            discovery = subprocess.run(
                ["git", "-C", str(directory), "rev-parse", "--show-cdup"],
                capture_output=True,
                env=env,
                timeout=30,
                check=False,
            )
        except FileNotFoundError:
            return set()
        worktrees: list[Path] = []
        # Discovery can skip incomplete metadata and find an outer repository.
        # lstat also keeps dangling .git symlinks and unreadable markers visible.
        for probe in (directory, *directory.parents):
            try:
                (probe / ".git").lstat()
            except FileNotFoundError:
                continue
            worktrees.append(probe)
        if discovery.returncode != 0:
            if not worktrees and b"not a git repository" in discovery.stderr:
                return set()
            detail = discovery.stderr.decode("utf-8", "replace").strip().splitlines()
            _warn_tracking_check_failed(directory, detail[0] if detail else f"git exited {discovery.returncode}")
            return None
        if not worktrees:
            _warn_tracking_check_failed(directory, "git discovered a repository without an accessible .git marker")
            return None
        names: set[str] = set()
        # A nested repository's index can hide paths still in an outer index.
        for marker_root in worktrees:
            discovery = subprocess.run(
                ["git", "-C", str(marker_root), "--git-dir", str(marker_root / ".git"), "rev-parse", "--show-cdup"],
                capture_output=True,
                env=env,
                timeout=30,
                check=False,
            )
            if discovery.returncode != 0:
                detail = discovery.stderr.decode("utf-8", "replace").strip().splitlines()
                _warn_tracking_check_failed(directory, detail[0] if detail else f"git exited {discovery.returncode}")
                return None
            # --show-cdup can return an absolute configured-worktree path.
            worktree = (marker_root / os.fsdecode(discovery.stdout.removesuffix(b"\n"))).resolve()
            result = subprocess.run(
                [
                    "git",
                    "-C",
                    str(worktree),
                    "--git-dir",
                    str(marker_root / ".git"),
                    "--work-tree",
                    str(worktree),
                    "-c",
                    "core.fsmonitor=false",
                    "ls-files",
                    "-z",
                ],
                capture_output=True,
                env=env,
                timeout=30,
                check=False,
            )
            if result.returncode != 0:
                detail = result.stderr.decode("utf-8", "replace").strip().splitlines()
                _warn_tracking_check_failed(directory, detail[0] if detail else f"git exited {result.returncode}")
                return None
            for entry in result.stdout.decode("utf-8", "surrogateescape").split("\0"):
                if not entry:
                    continue
                try:
                    # Git's case matching can miss Unicode directory aliases.
                    if (worktree / entry).parent.samefile(directory):
                        names.add(Path(entry).name)
                except FileNotFoundError:
                    # A deleted parent cannot contain the current handoff.
                    continue
        return names
    except (OSError, subprocess.SubprocessError) as exc:
        _warn_tracking_check_failed(directory, str(exc))
        return None


def _git_tracked_files(directory: Path) -> tuple[set[str], set[tuple[int, int]]] | None:
    """Return indexed names and file identities directly in `directory`.

    Names protect deleted and recreated files. Identities also protect case
    aliases on case-insensitive filesystems. None means the check failed.
    """
    names = _git_tracked_names(directory)
    if names is None:
        return None
    identities: set[tuple[int, int]] = set()
    for name in names:
        try:
            st = (directory / name).stat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            _warn_tracking_check_failed(directory, str(exc))
            return None
        identities.add((st.st_dev, st.st_ino))
    return names, identities


def _is_tracked(path: Path, tracked: tuple[set[str], set[tuple[int, int]]] | None) -> bool:
    if tracked is None:
        return True
    names, identities = tracked
    # Protect case aliases even when their identities could not be read.
    if path.name.casefold() in {name.casefold() for name in names}:
        return True
    try:
        st = path.stat()
    except OSError:
        return True
    return (st.st_dev, st.st_ino) in identities


def _list_handoffs(handoffs_dir: Path) -> tuple[List[Path], List[Path]]:
    """Split the inbox's handoffs into (ingestable, git-tracked)."""
    tracked_files = _git_tracked_files(handoffs_dir)
    out: List[Path] = []
    tracked: List[Path] = []
    for p in sorted(handoffs_dir.iterdir()):
        if p.is_file() and handoff_worktrees.is_handoff_filename(p.name):
            if _is_tracked(p, tracked_files):
                tracked.append(p)
            else:
                out.append(p)
    return out, tracked


def _archive(src: Path, processed_dir: Path, expected_sha: str | None = None) -> Path:
    # Re-check right before the move: a file tracked after listing must stay.
    if _is_tracked(src, _git_tracked_files(src.parent)):
        raise TrackedHandoffError(f"refusing to archive git-tracked handoff {src}")
    if expected_sha is not None and hashlib.sha256(src.read_bytes()).hexdigest() != expected_sha:
        raise ChangedHandoffError(f"handoff changed since it was read: {src.name}")
    processed_dir.mkdir(parents=True, exist_ok=True)
    dest = processed_dir / src.name
    if dest.exists():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        dest = processed_dir / f"{src.stem}-{stamp}{src.suffix}"
    shutil.move(str(src), dest)
    return dest


def _write_review_draft(inbox_dir: Path, base: str, text: str) -> Path:
    """Create a new review draft, never replacing an existing one.

    Sanitizing and truncating handoff names can map two handoffs to one
    stem within the same second, so a taken name gets a numbered suffix.
    """
    for index in range(_MAX_DRAFT_SUFFIX):
        dest = inbox_dir / (f"{base}.md" if index == 0 else f"{base}-{index}.md")
        try:
            with dest.open("xb") as handle:
                handle.write(text.encode("utf-8"))
                handle.flush()
                os.fsync(handle.fileno())
        except FileExistsError:
            continue
        return dest
    raise OSError(f"no free review draft name for {base}")


def _summary(verb: str, name: str, code: str, meta: str = "") -> str:
    """One action line: verb, safely encoded handoff filename, reason code.

    Never carries handoff fields, content-derived text or exception text.
    """
    return f"{verb:<10} {handoff_worktrees.safe_text(name)}  [{code or 'unclassified'}]{meta}"


def _match_meta(outcome: Outcome, target: Path) -> str:
    """Bounded metadata about a matched existing card (from the card index)."""
    match = outcome.match
    if match is None:
        return ""
    try:
        rel = match.path.relative_to(target).as_posix()
    except ValueError:
        rel = match.path.name
    meta = f"  match={handoff_worktrees.safe_text(rel)}"
    similarity = getattr(match, "similarity", None)
    if isinstance(similarity, (int, float)):
        meta += f" similarity={similarity:.2f}"
    if getattr(match, "opposite_polarity", False):
        meta += " polarity=opposite"
    return meta


def _record_failure(stats: IngestStats, path: Path, code: str) -> None:
    stats.failed += 1
    stats.actions.append(_summary("fail", path.name, code) + f"  in={handoff_worktrees.safe_text(path.parent)}")


def _report(stats: IngestStats, dry_run: bool) -> None:
    tag = "[dry-run] " if dry_run else ""
    for line in stats.actions:
        print(f"  {tag}{line}")
    for card in stats.blocked_cards:
        print(f"  {tag}blocked overflow card {card['path']}: {card['held_drafts']} draft(s) held ({card['problem']})")
    print()
    print(
        f"{tag}Processed {stats.processed}  Promoted {stats.promoted}  "
        f"Reinforced {stats.reinforced}  Routed {stats.routed}  "
        f"Inboxed {stats.inboxed}  Skipped {stats.skipped}  Failed {stats.failed}"
    )
    if stats.failed:
        print(f"{tag}PARTIAL: {stats.failed} handoff(s) failed and were left in place; the rest were ingested")
    elif stats.processed == 0:
        print(f"{tag}NO_UPDATES")
    print(f"{tag}Status {stats.status}")


def reprocess_review_draft(owner: Path, draft: Path, *, card_ids: CardIdIndex | None = None) -> Action:
    """Retry a review draft through durable prepare-then-finish publication."""
    from .memory_inbox_retry import reprocess

    return reprocess(owner, draft, card_ids=card_ids)
