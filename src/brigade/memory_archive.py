"""Move cards out of retrieval, retaining exact bytes and reversible index edits."""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath
from typing import Any

from . import memory_care_hub as hub
from . import memory_care_journal as journal
from . import memory_cmd, memory_hub_sync

ARCHIVE_ROOT = "memory/archive"


class ArchiveRefused(ValueError):
    """A protected, missing or changed card cannot be moved."""


def archive_rel(card_rel: str) -> str:
    journal.safe_path(Path("."), card_rel)
    return f"{ARCHIVE_ROOT}/{card_rel.removeprefix('memory/')}"


def is_archived_path(rel: str) -> bool:
    return rel == ARCHIVE_ROOT or rel.startswith(ARCHIVE_ROOT + "/")


_ARCHIVE_LINE = re.compile(r"""^\s*(['"]?)archive\1\s*:(.*)$""", re.IGNORECASE)


def frontmatter_refusal(text: str) -> str | None:
    """Shared archive safety check for planning and application.

    Brigade reads a YAML subset. Block lists and indented continuations are
    valid after a key. Plain scalars may contain balanced bracketed words.
    """
    normalized = text.removeprefix("\ufeff").replace("\r\n", "\n").lstrip("\n")
    if not normalized.lstrip().startswith("---"):
        return None
    lines = normalized.splitlines()
    end = next((n for n in range(1, len(lines)) if lines[n].strip() == "---"), None)
    if lines[0] != "---" or end is None:
        return "malformed frontmatter"
    have_key = False
    for line in lines[1:end]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = _ARCHIVE_LINE.match(line)
        if match:
            pin = match[2].split("#", 1)[0].strip().strip("\"'").lower()
            # An empty or block-scalar value continues on later lines; treat it as a pin, never as a guess.
            if pin.startswith("never") or pin in ("", "|", ">", "|-", ">-", "|+", ">+"):
                return "archive: never"
        if line[0].isspace() and have_key:
            continue
        key, sep, value = line.partition(":")
        if not sep or not key.strip():
            return "malformed frontmatter"
        have_key = True
        value = value.split("#", 1)[0].strip()
        pairs = {"[": "]", "{": "}", "'": "'", '"': '"'}
        if value and value[0] in pairs and pairs[value[0]] not in value[1:]:
            return "malformed frontmatter"
    return None


@journal.serialized
def archive_card(target: Path, card_rel: str, *, reason: str, config: memory_cmd.MemoryCareConfig) -> journal.Decision:
    try:
        if PurePosixPath(card_rel).as_posix() != card_rel or "." in card_rel.split("/") or "//" in card_rel:
            raise ArchiveRefused(f"{card_rel}: non-canonical path")
        allowed = {p.relative_to(target).as_posix() for p in memory_cmd._iter_cards(target, config)}
        if card_rel not in allowed:
            raise ArchiveRefused(f"{card_rel}: not an enumerated card")
        if card_rel in config.index_paths:
            raise ArchiveRefused(f"{card_rel}: configured index")
        if is_archived_path(card_rel) or memory_cmd._is_read_only(config, card_rel):
            raise ArchiveRefused(f"{card_rel}: archived or read-only")
        before = journal.current(target, card_rel)
        if before is None:
            raise ArchiveRefused(f"{card_rel}: missing card")
        text = before.decode("utf-8")
        refusal = frontmatter_refusal(text)
        if refusal:
            raise ArchiveRefused(f"{card_rel}: {refusal}")
        dest = archive_rel(card_rel)
        if journal.current(target, dest) is not None or memory_cmd._is_read_only(config, dest):
            raise ArchiveRefused(f"{dest}: occupied or read-only")
        card_id = memory_hub_sync.publication_identity(text, card_rel)[0]
        changes = [
            journal.Change(dest, None, before, journal.safe_path(target, card_rel).stat().st_mode & 0o777),
            journal.Change(card_rel, before, None),
        ]
        index_lines: list[dict[str, Any]] = []
        index_warnings: list[dict[str, Any]] = []
        for index in config.index_paths:
            if memory_cmd._is_read_only(config, index):
                continue
            data = journal.current(target, index)
            if data is None:
                continue
            lines = data.decode("utf-8").splitlines(keepends=True)
            link = r"\[[^\]]*\]\(" + re.escape(card_rel) + r"(?:#[^)]*)?\)"
            index_warnings.extend(
                {"path": index, "number": n, "reason": "mixed-content link retained"}
                for n, line in enumerate(lines)
                if re.search(link, line) and not re.fullmatch(r"\s*(?:[-+*] |[0-9]+[.)] )?" + link + r"\s*", line)
            )
            removed = [
                n for n, line in enumerate(lines) if re.fullmatch(r"\s*(?:[-+*] |[0-9]+[.)] )?" + link + r"\s*", line)
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
            hub={"status": "pending", "action": "retire", "path": card_rel},
            prepared_paths=[dest],
            metadata={
                "index_paths": sorted({row["path"] for row in index_lines}),
                "index_lines": index_lines,
                "index_warnings": index_warnings,
            },
        )
        if journal._payload(target, d.decision_id)["phase"] == "abandoned":
            raise ArchiveRefused(f"{card_rel}: decision abandoned after card changed")
        return journal.update_hub(
            target, d.decision_id, hub.retire(target, card_id=card_id, reason=reason, path=card_rel)
        )
    except (
        OSError,
        UnicodeError,
        journal.JournalConflict,
        journal.ReadOnlyPath,
        journal.kernel.ProjectionError,
    ) as exc:
        raise ArchiveRefused(str(exc)) from exc


@journal.serialized
def restore(target: Path, decision: journal.Decision) -> journal.Decision:
    try:
        d = journal.load(target, decision.decision_id)
        metadata = journal._payload(target, d.decision_id)
        if metadata.get("phase") == "abandoned" and not metadata.get("restorable"):
            raise ArchiveRefused("decision abandoned after card changed")
        if d.undone_by or d.kind != "archive":
            raise ArchiveRefused("decision is not an active archive")
        indices = journal.index_paths(metadata)
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
            changes.append(journal.Change(path, live, before, metadata.get("modes", {}).get(path)))
            if before is not None:
                card_rel, original = path, before
        config = memory_cmd._config_or_default(target)
        for index in sorted(indices):
            if memory_cmd._is_read_only(config, index):
                raise ArchiveRefused(f"{index}: read-only")
            live = journal.current(target, index)
            if live is None:
                raise ArchiveRefused(f"{index}: index removed after archive")
            if journal.kernel.content_digest(live) == d.after_sha256[index]:
                import base64

                saved = metadata.get("index_before", {}).get(index)
                exact = base64.b64decode(saved) if saved is not None else journal.before_bytes(target, d, index)
                changes.append(journal.Change(index, live, exact))
                continue
            lines = live.decode("utf-8").splitlines(keepends=True)
            for row in reversed([r for r in metadata["index_lines"] if r["path"] == index]):
                if row["line"] in lines:
                    continue
                neighbor = row["neighbor"]
                anchor = min(row["number"], len(lines))
                position = min(
                    (n for n, line in enumerate(lines) if line == neighbor),
                    key=lambda n: abs(n - anchor),
                    default=anchor,
                )
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
        if journal._payload(target, undo.decision_id).get("phase") == "abandoned":
            return journal.update_hub(target, undo.decision_id, {"status": "skipped", "reason": "undo abandoned"})
        withheld = memory_hub_sync.withheld(original.decode("utf-8"))
        if withheld:
            return journal.update_hub(target, undo.decision_id, {"status": "skipped", "reason": withheld})
        # Flush a pending retire first using its original event id, then revive.
        if (d.hub or {}).get("status") == "pending":
            result = hub._send(
                target,
                action="retire",
                card_id=d.card_id or "",
                reason=d.reason,
                path=str((d.hub or {}).get("path") or card_rel),
                request=(d.hub or {}).get("request"),
                recorded_hub=(d.hub or {}).get("hub"),
            )
            journal.update_hub(target, d.decision_id, result)
            if result["status"] in ("pending", "conflict", "needs_review"):
                return journal.update_hub(
                    target,
                    undo.decision_id,
                    {
                        **(undo.hub or {}),
                        "status": result["status"],
                        "reason": result.get("reason", "retire unresolved"),
                    },
                )
        return journal.update_hub(
            target,
            undo.decision_id,
            hub.revive(target, card_id=d.card_id or "", path=card_rel, text=original.decode("utf-8")),
        )
    except (
        OSError,
        UnicodeError,
        journal.JournalConflict,
        journal.ReadOnlyPath,
        journal.kernel.ProjectionError,
    ) as exc:
        raise ArchiveRefused(str(exc)) from exc
