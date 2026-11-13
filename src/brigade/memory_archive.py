"""Move cards out of retrieval, retaining exact bytes and reversible index edits."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from . import memory_care_hub as hub
from . import memory_care_journal as journal
from . import memory_cmd

ARCHIVE_ROOT = "memory/archive"


class ArchiveRefused(ValueError):
    """A protected, missing or changed card cannot be moved."""


def archive_rel(card_rel: str) -> str:
    journal.safe_path(Path("."), card_rel)
    return f"{ARCHIVE_ROOT}/{card_rel.removeprefix('memory/')}"


def is_archived_path(rel: str) -> bool:
    return rel == ARCHIVE_ROOT or rel.startswith(ARCHIVE_ROOT + "/")


def archive_card(target: Path, card_rel: str, *, reason: str, config: memory_cmd.MemoryCareConfig) -> journal.Decision:
    try:
        if is_archived_path(card_rel) or memory_cmd._is_read_only(config, card_rel):
            raise ArchiveRefused(f"{card_rel}: archived or read-only")
        before = journal.current(target, card_rel)
        if before is None:
            raise ArchiveRefused(f"{card_rel}: missing card")
        text = before.decode("utf-8")
        front, _ = memory_cmd._parse_frontmatter(text)
        if str(front.get("archive", "")).lower() == "never":
            raise ArchiveRefused(f"{card_rel}: archive: never")
        dest = archive_rel(card_rel)
        if journal.current(target, dest) is not None or memory_cmd._is_read_only(config, dest):
            raise ArchiveRefused(f"{dest}: occupied or read-only")
        card_id = memory_cmd.card_identity_from_text(text, card_rel)
        changes = [journal.Change(dest, None, before), journal.Change(card_rel, before, None)]
        index_lines: list[dict[str, Any]] = []
        for index in config.index_paths:
            if memory_cmd._is_read_only(config, index):
                continue
            data = journal.current(target, index)
            if data is None:
                continue
            lines = data.decode("utf-8").splitlines(keepends=True)
            removed = [
                n
                for n, line in enumerate(lines)
                if re.search(r"\[[^\]]*\]\(" + re.escape(card_rel) + r"(?:#[^)]*)?\)", line)
            ]
            if not removed:
                continue
            index_lines.extend(
                {"path": index, "line": lines[n], "number": n, "neighbor": lines[n + 1] if n + 1 < len(lines) else None}
                for n in removed
            )
            changes.append(
                journal.Change(
                    index, data, "".join(line for n, line in enumerate(lines) if n not in removed).encode("utf-8")
                )
            )
        d = journal.commit_phased(
            target,
            kind="archive",
            reason=reason,
            card_id=card_id,
            changes=changes,
            hub={"status": "pending", "action": "retire"},
            prepared_paths=[dest],
            metadata={"index_lines": index_lines},
        )
        if journal._payload(target, d.decision_id)["phase"] == "abandoned":
            raise ArchiveRefused(f"{card_rel}: decision abandoned after card changed")
        return journal.update_hub(target, d.decision_id, hub.retire(target, card_id=card_id, reason=reason))
    except (OSError, UnicodeError, journal.JournalConflict, journal.ReadOnlyPath) as exc:
        raise ArchiveRefused(str(exc)) from exc


def restore(target: Path, decision: journal.Decision) -> journal.Decision:
    try:
        d = journal.load(target, decision.decision_id)
        metadata = journal._payload(target, d.decision_id)
        if metadata.get("phase") == "abandoned":
            raise ArchiveRefused("decision abandoned after card changed")
        if d.undone_by or d.kind != "archive":
            raise ArchiveRefused("decision is not an active archive")
        indices = {row["path"] for row in metadata.get("index_lines", [])}
        changes = []
        card_rel = ""
        original = b""
        for path in d.paths:
            if path in indices:
                continue
            live = journal.current(target, path)
            digest = None if live is None else journal.kernel.content_digest(live)
            if digest != d.after_sha256[path]:
                raise ArchiveRefused(f"{path}: changed after archive")
            before = journal.before_bytes(target, d, path)
            changes.append(journal.Change(path, live, before))
            if before is not None:
                card_rel, original = path, before
        config = memory_cmd._config_or_default(target)
        for index in sorted(indices):
            if memory_cmd._is_read_only(config, index):
                raise ArchiveRefused(f"{index}: read-only")
            live = journal.current(target, index)
            if live is None:
                raise ArchiveRefused(f"{index}: index removed after archive")
            lines = live.decode("utf-8").splitlines(keepends=True)
            for row in reversed([r for r in metadata["index_lines"] if r["path"] == index]):
                if row["line"] in lines:
                    continue
                neighbor = row["neighbor"]
                position = lines.index(neighbor) if neighbor in lines else len(lines)
                lines.insert(position, row["line"])
            changes.append(journal.Change(index, live, "".join(lines).encode("utf-8")))
        undo = journal.commit_phased(
            target,
            kind="undo",
            reason=f"restore {d.decision_id}",
            card_id=d.card_id,
            changes=changes,
            undoes=d.decision_id,
            hub={"status": "pending", "action": "put", "path": card_rel},
            prepared_paths=[c.path for c in changes if c.after is not None],
        )
        # Flush a pending retire first using its original event id, then revive.
        if (d.hub or {}).get("status") == "pending":
            result = hub._send(
                target, action="retire", card_id=d.card_id or "", reason=d.reason, request=(d.hub or {}).get("request")
            )
            journal.update_hub(target, d.decision_id, result)
            if result["status"] in ("pending", "conflict"):
                return journal.update_hub(target, undo.decision_id, {**(undo.hub or {}), "status": result["status"]})
        return journal.update_hub(
            target,
            undo.decision_id,
            hub.revive(target, card_id=d.card_id or "", path=card_rel, text=original.decode("utf-8")),
        )
    except (OSError, UnicodeError, journal.JournalConflict, journal.ReadOnlyPath) as exc:
        raise ArchiveRefused(str(exc)) from exc
