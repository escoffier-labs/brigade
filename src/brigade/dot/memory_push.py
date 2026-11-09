"""Bounded memory hits pushed into responses hosted agents already call.

Hosted agents read memory only when nudged, so ``inbox_list`` and ``fleet_digest``
carry one ``memory_hits`` object for the item they show: at most 3 hits on
``inbox_list`` and 5 on ``fleet_digest``, and at most 4 KiB as encoded JSON. Card
bodies never ride along.

Where the hits come from depends on the workspace's ``[hub]`` mode (S23):

- ``off``: local cards, through bounded recall in a killable child with the text on
  stdin. The search log records only an HMAC of the query (``memory_recall_log``).
  Each hit is the card path plus its title and tags, wrapped as untrusted text.
- ``read`` or ``sync``: hub memory only, as the connector's own principal, through
  ``search`` with ``query_source: "push"``. The hub applies that principal's grants,
  the trust filter (reviewed, policy-promoted and imported cards, plus unreviewed
  ones only to their writer), head revalidation and its own egress scan. Each hit
  carries ``scope``, ``card_id``, a short content hash, ``trust``, ``score`` and
  ``source`` beside the wrapped title, summary and tags. A local card is never
  pushed in these modes, because it has no trust state, and an unavailable or
  refusing hub leaves the field out rather than falling back.

Every candidate is also scanned here before it is wrapped, with the prompt-load scan
from ``prompt_scan`` (the guard's default rules, the invisible and bidi character
check, the instruction-override heuristics and the private class). A hit that fails
is dropped, never redacted into the response, and so is a hub placeholder whose
text the hub withheld. Drops are counted on a ``PushScan`` the caller passes in
(rule ids and a count, never the matched text). A scan that raises fails closed: no
hits at all, and the error class goes on the same report. Push is fail-open
otherwise: any problem omits the field.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from ..untrusted import wrap_untrusted
from .contracts import encoded_size
from .prompt_scan import blocked_rule

MAX_PUSH_HITS = 3
MAX_DIGEST_HITS = 5
# The whole memory_hits object, as encoded JSON. Trailing hits are dropped to fit.
MAX_HITS_BYTES = 4096
QUERY_SOURCE = "push"
MAX_CARD_TEXT_CHARS = 300
MAX_TAGS = 8
SHORT_HASH_CHARS = 12
CARD_PATH_RE = re.compile(r"memory/cards/[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.md")
HASH_RE = re.compile(r"[0-9a-f]{64}")
RULE_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,47}")
# The trust values the hub's push path may answer with. Flagged and pending never leave it.
PUSHED_TRUST = frozenset({"reviewed", "policy-promoted", "imported", "agent-unreviewed"})
HUB_BLOCKED_RULE = "hub-blocked"
NEXT = "Read a card with memory_card(name=<path basename without .md>)."
HUB_NEXT = "Read a card with memory_card(name=<card_id>, scope=<scope>)."

MAX_REPORTED_RULES = MAX_DIGEST_HITS
MAX_RULE_CHARS = 48

Recall = Callable[..., dict[str, Any]]
Scan = Callable[[Iterable[str]], str | None]
# (query, limit) -> the hub's `search` answer for `query_source: "push"`.
HubSearch = Callable[[str, int], Any]


@dataclass
class PushScan:
    """What the prompt-load scan did to one push, safe to log: counts and ids only."""

    dropped: int = 0
    rules: list[str] = field(default_factory=list)
    error: str | None = None

    def drop(self, rule: str) -> None:
        self.dropped += 1
        label = rule[:MAX_RULE_CHARS]
        if label not in self.rules and len(self.rules) < MAX_REPORTED_RULES:
            self.rules.append(label)

    def summary(self) -> str:
        return f"dropped={self.dropped} rules={','.join(self.rules) or '-'}"


def _default_recall(**kwargs: Any) -> dict[str, Any]:
    from ..memory_hooks import recall_cards_payload

    return recall_cards_payload(**kwargs)


def _default_hub_mode(workspace: Path) -> str:
    from ..memory_hub_client import hub_mode

    return hub_mode(workspace)


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _tags(value: object) -> list[str]:
    return [tag for tag in value if isinstance(tag, str)][:MAX_TAGS] if isinstance(value, list) else []


def hub_query(query: str) -> str:
    """One line of at most 256 characters, as the recall hooks and the hub normalize it."""
    from ..memory_hooks import _normalize_query

    return _normalize_query(query)


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


def _card_text(title: str, tags: Iterable[str], summary: str = "") -> str:
    lines = [f"title: {title}"]
    if summary:
        lines.append(f"summary: {summary}")
    lines.append(f"tags: {', '.join(tags)}")
    return "\n".join(lines)


def _wrapped(title: str, tags: Iterable[str], summary: str = "") -> str:
    # One fence per hit keeps five hits inside the byte bound; the cap also bounds the summary.
    return wrap_untrusted(_card_text(title, tags, summary), source_kind="memory", max_chars=MAX_CARD_TEXT_CHARS)


def _fit(hits: list[dict[str, Any]], next_text: str) -> dict[str, Any] | None:
    payload = {"content_trust": "untrusted", "hits": hits, "next": next_text}
    while hits and encoded_size(payload) > MAX_HITS_BYTES:
        hits.pop()
    return payload if hits else None


def _score(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return round(float(value), 4)


def _hub_fields(match: dict[str, Any]) -> tuple[str, str, str, str, str, list[str], float | None] | None:
    """The labels of one well-formed hub push match, or None for anything else."""
    from ..memory_hub_client import valid_card_id, valid_scope

    scope, card_id, digest, trust = (match.get(key) for key in ("scope", "card_id", "content_hash", "trust"))
    if not (valid_scope(scope) and valid_card_id(card_id)):
        return None
    if not (isinstance(digest, str) and HASH_RE.fullmatch(digest)) or trust not in PUSHED_TRUST:
        return None
    raw_tags = match.get("tags")
    if raw_tags is not None and not isinstance(raw_tags, list):
        return None
    return (
        str(scope),
        str(card_id),
        digest,
        str(trust),
        _text(match.get("title")),
        _tags(raw_tags),
        _score(match.get("score")),
    )


class MemoryPush:
    def __init__(
        self,
        workspace: Path,
        *,
        enabled: bool = True,
        recall: Recall = _default_recall,
        scan: Scan = blocked_rule,
        hub: HubSearch | None = None,
        mode: Callable[[Path], str] = _default_hub_mode,
    ) -> None:
        self.workspace, self.enabled, self.recall, self.scan = workspace, enabled, recall, scan
        self.hub, self.mode = hub, mode

    def hits(self, query: str, report: PushScan | None = None, *, limit: int = MAX_PUSH_HITS) -> dict[str, Any] | None:
        """Up to ``limit`` scanned hits for ``query``, or None when push is off, empty or unavailable.

        ``report`` collects the scan's drops and error class for the caller to log.
        """
        report = report if report is not None else PushScan()
        limit = max(1, min(limit, MAX_DIGEST_HITS))
        if not self.enabled or not query.strip():
            return None
        try:
            mode = self.mode(self.workspace)
        except Exception:
            # An unreadable [hub] table never falls back to local cards.
            return None
        if mode != "off":
            return self._hub_hits(query, report, limit)
        return self._local_hits(query, report, limit)

    def _local_hits(self, query: str, report: PushScan, limit: int) -> dict[str, Any] | None:
        try:
            if not (self.workspace / "memory" / "cards").is_dir():
                return None
            payload = self.recall(
                target=self.workspace,
                cwd=self.workspace,
                limit=limit,
                query=query,
                query_source=QUERY_SOURCE,
            )
        except Exception:
            return None
        if not isinstance(payload, dict) or payload.get("status") != "ok":
            return None
        hits: list[dict[str, Any]] = []
        for match in payload.get("matches") or []:
            if len(hits) == limit:
                break
            if not isinstance(match, dict):
                continue
            path = match.get("path")
            if not isinstance(path, str) or CARD_PATH_RE.fullmatch(path) is None:
                continue
            tags = _tags(match.get("tags"))
            title = _text(match.get("title"))
            try:
                # The full title and tags, before the 300-character cap, so no cut can hide a hit.
                rule = self.scan([path, title, *tags])
            except Exception as error:
                report.error = type(error).__name__
                return None
            if rule is not None:
                report.drop(rule)
                continue
            hits.append({"path": path, "card": _wrapped(title, tags)})
        return _fit(hits, NEXT)

    def _hub_hits(self, query: str, report: PushScan, limit: int) -> dict[str, Any] | None:
        normalized = hub_query(query)
        if self.hub is None or not normalized:
            return None
        try:
            body = self.hub(normalized, limit)
        except Exception:
            return None
        matches = body.get("matches") if isinstance(body, dict) else None
        if not isinstance(matches, list):
            return None
        hits: list[dict[str, Any]] = []
        for match in matches:
            if len(hits) == limit:
                break
            if not isinstance(match, dict):
                continue
            if "blocked" in match:
                # The hub's own egress scan withheld this card's text: drop it, never show a placeholder.
                rule = match.get("blocked")
                report.drop(rule if isinstance(rule, str) and RULE_RE.fullmatch(rule) else HUB_BLOCKED_RULE)
                continue
            fields = _hub_fields(match)
            if fields is None:
                continue
            scope, card_id, digest, trust, title, tags, score = fields
            summary = _text(match.get("summary"))
            try:
                # Every text that leaves, in full: the location labels too, since a scope or id is free text.
                rule = self.scan([scope, card_id, title, summary, *tags])
            except Exception as error:
                report.error = type(error).__name__
                return None
            if rule is not None:
                report.drop(rule)
                continue
            hits.append(
                {
                    "source": "hub",
                    "scope": scope,
                    "card_id": card_id,
                    "hash": digest[:SHORT_HASH_CHARS],
                    "trust": trust,
                    "score": score,
                    "card": _wrapped(title, tags, summary),
                }
            )
        return _fit(hits, HUB_NEXT)
