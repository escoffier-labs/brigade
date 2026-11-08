"""Deterministic broken-link repair candidates for scheduled hub reflection.

A `[[link]]` in an anchored claim block is broken when it names no live card in
the card's own scope. When exactly one live card in that scope is the clear
target, the pass proposes a `replace_block` that changes only the link target.
The hub classifies a replacement that touches anything beyond ASCII spaces in
plain prose as Tier 2, so these proposals always wait for a reviewer. Nothing
here sends or reports card text beyond the replacement the hub must review.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from . import memory_proposal_io as io
from . import memory_proposal_v2_ops as delta
from . import memory_reflect_near as near

DEFAULT_MAX_PROPOSALS = 8
MAX_PROPOSALS_LIMIT = 32
# One op per block and at most seven cards per submission, below the hub's eight-card limit.
CHUNK_OPS = min(near.MAX_OPS, near.MAX_CARDS - 1)
LINK = re.compile(r"\[\[([^\[\]\n]+)\]\]")
SEPARATORS = re.compile(r"[\s\-]+")
PREFIXES = ("memory/cards/", "cards/")


def settings(target: Path) -> int:
    """Read the `[reflect] link_repair_max_proposals` per-run cap from memory-care.toml."""
    cap = near.reflect_table(target).get("link_repair_max_proposals", DEFAULT_MAX_PROPOSALS)
    if isinstance(cap, bool) or not isinstance(cap, int) or not 0 <= cap <= MAX_PROPOSALS_LIMIT:
        raise near.ThresholdError(
            f"[reflect] link_repair_max_proposals must be an integer from 0 to {MAX_PROPOSALS_LIMIT}"
        )
    return cap


def _fold(name: str) -> str:
    return name.strip().casefold()


def _loose(name: str) -> str:
    """Case, whitespace and hyphen normalization, so `Some Name` finds `some-name`."""
    return SEPARATORS.sub(" ", name.casefold()).strip()


def _names(text: str) -> tuple[list[str], list[str]]:
    """Identity names (aliases, topic) and the title, read as the hub's scalar metadata."""
    identities: list[str] = []
    titles: list[str] = []
    for key, into in (("aliases", identities), ("topic", identities), ("title", titles)):
        try:
            value = near._field(text, key)
        except ValueError:
            continue  # An ambiguous field grants no name. The hub refuses such cards anyway.
        if not value:
            continue
        if key == "aliases":
            try:
                items = json.loads(value) if value.startswith("[") else [value]
            except ValueError:
                items = [part for part in value.strip("[]").split(",")]
            into.extend(str(item).strip("\"' ") for item in items[:64] if isinstance(item, str))
        else:
            into.append(value.strip("\"'"))
    return [n for n in identities if n.strip()], [n for n in titles if n.strip()]


def _live(head: dict[str, Any]) -> bool:
    return head.get("state", "live") == "live" and not near._superseded(head["text"])


def _target(raw: str) -> str:
    """The card name a link target points at, without a folder prefix or `.md`."""
    name = raw.strip()
    for prefix in PREFIXES:
        if name.startswith(prefix):
            name = name[len(prefix) :]
    return name.removesuffix(".md")


class Index:
    """Names of every card the run read, so a link resolves within its own scope."""

    def __init__(self, heads: list[dict[str, Any]]) -> None:
        self.live: dict[tuple[str, str], set[str]] = {}  # (scope, folded name) -> live card ids
        self.loose: dict[tuple[str, str], set[str]] = {}  # (scope, normalized name) -> live card ids
        self.retired: dict[tuple[str, str], set[str]] = {}  # (scope, folded identity) -> retired card ids
        self.ids: dict[str, set[str]] = {}  # scope -> live card ids
        self.successor: dict[tuple[str, str], str] = {}  # retired card -> raw superseded_by
        self.scopes: dict[str, set[str]] = {}  # normalized name -> scopes with a live card
        for head in heads:
            scope, card_id = head["scope"], head["card_id"]
            identities, titles = _names(head["text"])
            if _live(head):
                self.ids.setdefault(scope, set()).add(card_id)
                for name in (card_id, *identities, *titles):
                    self.live.setdefault((scope, _fold(name)), set()).add(card_id)
                    self.loose.setdefault((scope, _loose(name)), set()).add(card_id)
                    self.scopes.setdefault(_loose(name), set()).add(scope)
                continue
            for name in (card_id, *identities):
                self.retired.setdefault((scope, _fold(name)), set()).add(card_id)
            try:
                value = near._field(head["text"], "superseded_by")
            except ValueError:
                value = None
            if value:
                self.successor[(scope, card_id)] = value

    def resolve(self, scope: str, raw: str) -> tuple[str | None, str | None]:
        """Return the live card a broken link should name, or the reason to skip it."""
        name = _target(raw)
        retired = self.retired.get((scope, _fold(name)), set())
        successors: set[str] = set()
        elsewhere = False
        for card_id in retired:
            value = self.successor.get((scope, card_id))
            if value is None:
                continue
            target = _target(value.strip("\"'"))
            if target.startswith(scope + "/"):
                target = target[len(scope) + 1 :]
            if "/" in target:
                elsewhere = True
            elif target.lower() in self.ids.get(scope, set()):
                successors.add(target.lower())
        if len(successors) == 1:
            return next(iter(successors)), None
        matches = self.loose.get((scope, _loose(name)), set())
        if len(successors) > 1 or len(matches) > 1:
            return None, "link_ambiguous_target"
        if matches:
            return next(iter(matches)), None
        if elsewhere or any(other != scope for other in self.scopes.get(_loose(name), ())):
            return None, "link_cross_scope_target"
        return None, "link_no_target"


def _code_spans(text: str) -> list[tuple[int, int]]:
    """Inline code spans, opened and closed by backtick runs of equal length."""
    spans: list[tuple[int, int]] = []
    size, at = len(text), 0
    while at < size:
        if text[at] != "`":
            at += 1
            continue
        end = at
        while end < size and text[end] == "`":
            end += 1
        close, cursor = -1, end
        while cursor < size:
            if text[cursor] != "`":
                cursor += 1
                continue
            stop = cursor
            while stop < size and text[stop] == "`":
                stop += 1
            if stop - cursor == end - at:
                close = stop
                break
            cursor = stop
        if close < 0:
            at = end
            continue
        spans.append((at, close))
        at = close
    return spans


def _links(block: str) -> list[tuple[int, int, str]]:
    """Link target spans outside code spans, as (start, end, raw target).

    The target ends at the first `#` or `|`, so an alias and a heading stay out of it.
    """
    code = _code_spans(block) if "`" in block else []
    found = []
    for match in LINK.finditer(block):
        start = match.start()
        if (start and block[start - 1] in "\\!") or any(a <= start < b for a, b in code):
            continue
        inner = match.start(1)
        raw = match.group(1)
        stop = next((i for i, char in enumerate(raw) if char in "#|"), len(raw))
        if raw[:stop].strip():
            found.append((inner, inner + stop, raw[:stop]))
    return found


def _wording(block: delta.Block, edits: list[tuple[int, int, str]]) -> str | None:
    """The block without its anchor and with each link target replaced, or None when the hub would refuse it."""
    text = block.text
    for start, end, value in sorted(edits, reverse=True):
        text = text[:start] + value + text[end:]
    wording = delta.ANCHOR.sub("", text)
    if not wording.strip() or len(wording.encode()) > io.MAX_REASON_BYTES or any(char in wording for char in "\n\r^"):
        return None
    try:
        parsed = delta.blocks(wording)
    except ValueError:
        return None
    return wording if len(parsed) == 1 and parsed[0].text == wording else None


def _bump(counts: dict[str, int], reason: str, amount: int = 1) -> None:
    counts[reason] = counts.get(reason, 0) + amount


def ops(
    heads: list[dict[str, Any]], *, index: Index, stats: dict[str, int]
) -> tuple[list[list[dict[str, Any]]], dict[str, int]]:
    """Return replace_block submissions for one scope's heads, and counted skips.

    Each broken link ends in one repair or one counted skip. A block with
    several repairable links gets one op. Cards go in card id order and a
    submission holds whole cards, up to seven ops.
    """
    skipped: dict[str, int] = {}
    by_card: list[list[dict[str, Any]]] = []
    for head in sorted(heads, key=lambda h: h["card_id"]):
        if "[[" not in head["text"]:
            continue
        try:
            blocks = delta.blocks(head["text"])
        except ValueError:
            _bump(skipped, "link_invalid_blocks")
            continue
        reason = None
        if not _live(head):
            reason = "link_inactive_card"
        elif head.get("scan_state") != "clean":
            reason = "link_non_clean"
        elif head.get("trust") not in near.KNOWN_TRUST:
            reason = "link_unknown_trust"
        card_ops = []
        for block in blocks:
            edits: list[tuple[int, int, str]] = []
            for start, end, raw in _links(block.text):
                stats["links"] += 1
                name = _target(raw)
                if index.live.get((head["scope"], _fold(name))):
                    continue
                stats["broken"] += 1
                if reason or not block.block_id:
                    _bump(skipped, reason or "link_missing_pin")
                    continue
                target, why = index.resolve(head["scope"], raw)
                if target is None:
                    _bump(skipped, why or "link_no_target")
                    continue
                # Keep a folder prefix, extension and surrounding space as written.
                lead = raw[: len(raw) - len(raw.lstrip())]
                tail = raw[len(raw.rstrip()) :]
                bare = raw.strip()
                prefix = next((p for p in PREFIXES if bare.startswith(p)), "")
                suffix = ".md" if bare.endswith(".md") else ""
                edits.append((start, end, lead + prefix + target + suffix + tail))
            if not edits:
                continue
            wording = _wording(block, edits)
            if wording is None:
                _bump(skipped, "link_unreplaceable_block", len(edits))
                continue
            assert block.block_id is not None
            stats["repairs"] += len(edits)
            card_ops.append(
                {
                    "type": "replace_block",
                    "scope": head["scope"],
                    "card_id": head["card_id"],
                    "base_revision": head["revision"],
                    "block_id": block.block_id,
                    "expected_block_hash": block.sha256,
                    "text": wording,
                }
            )
        if card_ops:
            by_card.append(card_ops)
    submissions: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for card_ops in by_card:
        for at in range(0, len(card_ops), CHUNK_OPS):
            part = card_ops[at : at + CHUNK_OPS]
            if len(current) + len(part) > CHUNK_OPS:
                submissions.append(current)
                current = []
            current.extend(part)
    if current:
        submissions.append(current)
    return submissions, skipped
