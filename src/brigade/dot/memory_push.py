"""Bounded memory hits pushed into responses hosted agents already call.

Hosted agents read memory only when nudged, so ``inbox_list`` and ``fleet_digest``
carry up to three card hits for the item they show. Each hit is the card path plus
its title and tags wrapped as untrusted memory text; card bodies never ride along.

The query is derived from the shown item (message subject and body, or the digest's
inbox subjects and next work title). It runs through bounded recall in a killable
child with the text on stdin, and the search log records only an HMAC of it
(``memory_recall_log``). Push is fail-open: any problem omits the field.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable, Iterable

from ..untrusted import wrap_untrusted

MAX_PUSH_HITS = 3
QUERY_SOURCE = "push"
MAX_CARD_TEXT_CHARS = 300
MAX_TAGS = 8
CARD_PATH_RE = re.compile(r"memory/cards/[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.md")
NEXT = "Read a card with memory_card(name=<path basename without .md>)."

Recall = Callable[..., dict[str, Any]]


def _default_recall(**kwargs: Any) -> dict[str, Any]:
    from ..memory_hooks import recall_cards_payload

    return recall_cards_payload(**kwargs)


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def inbox_query(listing: dict[str, Any]) -> str:
    """The first listed message is the one the agent handles next."""
    messages = listing.get("messages")
    if not isinstance(messages, list) or not messages or not isinstance(messages[0], dict):
        return ""
    first = messages[0]
    return " ".join(part for part in (_text(first.get("subject")), _text(first.get("body"))) if part)


def digest_query(digest: dict[str, Any]) -> str:
    """The digest topic: pending inbox subjects, then the next work item title."""
    parts: list[str] = []
    inbox = digest.get("inbox")
    if isinstance(inbox, dict) and isinstance(inbox.get("oldest"), list):
        parts.extend(_text(row.get("subject")) for row in inbox["oldest"] if isinstance(row, dict))
    work = digest.get("fleet_work_next")
    item = work.get("item") if isinstance(work, dict) else None
    if isinstance(item, dict):
        parts.append(_text(item.get("title")))
    return " ".join(part for part in parts if part)


def _card_text(title: str, tags: Iterable[str]) -> str:
    return f"title: {title}\ntags: {', '.join(tags)}"


class MemoryPush:
    def __init__(self, workspace: Path, *, enabled: bool = True, recall: Recall = _default_recall) -> None:
        self.workspace, self.enabled, self.recall = workspace, enabled, recall

    def hits(self, query: str) -> dict[str, Any] | None:
        """Up to three hits for ``query``, or None when push is off, empty or unavailable."""
        if not self.enabled or not query.strip():
            return None
        try:
            if not (self.workspace / "memory" / "cards").is_dir():
                return None
            payload = self.recall(
                target=self.workspace,
                cwd=self.workspace,
                limit=MAX_PUSH_HITS,
                query=query,
                query_source=QUERY_SOURCE,
            )
        except Exception:
            return None
        if not isinstance(payload, dict) or payload.get("status") != "ok":
            return None
        hits: list[dict[str, str]] = []
        for match in payload.get("matches") or []:
            if len(hits) == MAX_PUSH_HITS:
                break
            if not isinstance(match, dict):
                continue
            path = match.get("path")
            if not isinstance(path, str) or CARD_PATH_RE.fullmatch(path) is None:
                continue
            raw_tags = match.get("tags")
            tags = [tag for tag in raw_tags if isinstance(tag, str)][:MAX_TAGS] if isinstance(raw_tags, list) else []
            card = wrap_untrusted(
                _card_text(_text(match.get("title")), tags), source_kind="memory", max_chars=MAX_CARD_TEXT_CHARS
            )
            hits.append({"path": path, "card": card})
        if not hits:
            return None
        return {"content_trust": "untrusted", "hits": hits, "next": NEXT}
