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

from . import memory_hub_client as hub_client
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


# The hub's metadata list limits (engines/hub/src/memory/proposal_metadata.rs `list`).
# Every alias up to them is indexed, because a dropped alias could make an unrelated
# card look like the only match for a link that already names this card.
MAX_ALIASES = 256
MAX_ALIAS_BYTES = 1024


def _aliases(value: str) -> list[str]:
    """A JSON list of alias strings within the hub's limits, else ValueError."""
    try:
        items = json.loads(value)
    except ValueError:
        raise ValueError("malformed aliases") from None
    if not isinstance(items, list) or not all(isinstance(item, str) for item in items):
        raise ValueError("malformed aliases")
    try:
        oversized = any(len(item.encode()) > MAX_ALIAS_BYTES for item in items)
    except UnicodeEncodeError:
        # An unpaired surrogate is not valid UTF-8, so the hub would refuse it too.
        raise ValueError("malformed aliases") from None
    if len(items) > MAX_ALIASES or oversized:
        raise ValueError("malformed aliases")
    return [item.strip() for item in items]


def _names(text: str) -> tuple[list[str], list[str], bool]:
    """Identity names (aliases, topic), the title, and whether all metadata is well formed.

    Metadata is never parsed loosely. Each field is read on its own: an ambiguous
    field, or an alias value that is not a JSON list of strings within the hub's
    limits, contributes no names and marks the card malformed. Names from the
    fields that do parse are still returned, so a malformed card still collides
    with another card of the same name. A malformed card is never a repair target.

    Retirement fields decide whether a card is live. `near._superseded` treats an
    unparseable field as not retired, so an ambiguous one marks the card malformed
    here, or a retired card could be admitted as a live repair target.
    """
    identities: list[str] = []
    titles: list[str] = []
    well_formed = True
    for key in ("status", "superseded_by"):
        try:
            near._field(text, key)
        except ValueError:
            well_formed = False
    for key, into in (("aliases", identities), ("topic", identities), ("title", titles)):
        try:
            value = near._field(text, key)
            if not value:
                continue
            into.extend(_aliases(value) if key == "aliases" else [value.strip("\"'")])
        except ValueError:
            well_formed = False
    return [n for n in identities if n.strip()], [n for n in titles if n.strip()], well_formed


def _live(head: dict[str, Any]) -> bool:
    return head.get("state", "live") == "live" and not near._superseded(head["text"])


def _target(raw: str) -> str:
    """The card name a link target points at, without a folder prefix or `.md`."""
    name = raw.strip()
    for prefix in PREFIXES:
        if name.startswith(prefix):
            name = name[len(prefix) :]
    return name.removesuffix(".md")


def _scoped(scope: str, name: str) -> str | None:
    """A name as this scope's links would write it, or None when it belongs to another scope.

    An exact merge keeps the retired card's identity on the keeper as a scope
    qualified alias such as `repo:links/old-guide`. Links in the same scope write
    `[[old-guide]]`, so the alias is indexed in that short form. A name
    qualified for another scope never resolves a link here.

    A name is scope qualified only when some `/` splits it into a scope the hub
    accepts and a card path that `_target` reduces to a valid card id. The
    card's own scope is tried first as an exact prefix, so a scope such as
    `repo:a/cards` keeps `repo:a/cards/c`. Other splits are then tried from the
    left, because a repo scope contains a `/` itself and the card path can
    carry a `cards/` prefix or `.md`. So
    `repo:links/cards/old-guide.md` is `old-guide` in `repo:links`. Any other
    alias, such as `docs:v1/guide`, is kept as written.
    """
    if name.startswith(scope + "/"):
        own = _target(name[len(scope) + 1 :])
        if hub_client.valid_card_id(own):
            return own
    at = name.find("/")
    while at > 0:
        owner, card_id = name[:at], _target(name[at + 1 :])
        if hub_client.valid_scope(owner) and hub_client.valid_card_id(card_id):
            return card_id if owner == scope else None
        at = name.find("/", at + 1)
    return name


class Index:
    """Names of every card the run read, so a link resolves within its own scope."""

    def __init__(self, heads: list[dict[str, Any]]) -> None:
        self.live: dict[tuple[str, str], set[str]] = {}  # (scope, folded name) -> live card ids
        self.loose: dict[tuple[str, str], set[str]] = {}  # (scope, normalized name) -> live card ids
        self.retired: dict[tuple[str, str], set[str]] = {}  # (scope, normalized name) -> retired card ids
        self.ids: dict[str, set[str]] = {}  # scope -> live card ids
        self.successor: dict[tuple[str, str], str] = {}  # retired card -> raw superseded_by
        self.scopes: dict[str, set[str]] = {}  # normalized name -> scopes with a live card
        # (scope, card id) -> (scan state, trust class, metadata is well formed)
        self.info: dict[tuple[str, str], tuple[Any, Any, bool]] = {}
        for head in heads:
            scope, card_id = head["scope"], head["card_id"]
            identities, titles, well_formed = _names(head["text"])
            identities = [n for n in (_scoped(scope, name) for name in identities) if n]
            # Index names the way `_target` reads a link, so a path-form alias such as
            # `cards/old-guide.md` matches `[[cards/old-guide.md]]` and keeps its owner.
            identities = [_target(n) for n in identities]
            titles = [_target(n) for n in titles]
            self.info[(scope, card_id)] = (head.get("scan_state"), head.get("trust"), well_formed)
            if _live(head):
                self.ids.setdefault(scope, set()).add(card_id)
                for name in (card_id, *identities, *titles):
                    self.live.setdefault((scope, _fold(name)), set()).add(card_id)
                    self.loose.setdefault((scope, _loose(name)), set()).add(card_id)
                    self.scopes.setdefault(_loose(name), set()).add(scope)
                continue
            for name in (card_id, *identities, *titles):
                self.retired.setdefault((scope, _loose(name)), set()).add(card_id)
            try:
                value = near._field(head["text"], "superseded_by")
            except ValueError:
                value = None
            if value:
                self.successor[(scope, card_id)] = value

    def _admit(self, scope: str, card_id: str, trust: Any) -> str | None:
        """The skip reason for a target the hub would never scan or show, or None.

        A proposal names only the source card, so a repair writes a name the
        reviewer cannot see the destination of. The target must be scan clean,
        share the source's trust class and have well formed metadata.
        """
        scan, target_trust, well_formed = self.info[(scope, card_id)]
        if not well_formed:
            return "link_invalid_metadata"
        if scan != "clean" or target_trust != trust:
            return "link_untrusted_target"
        return None

    def valid(self, scope: str, name: str) -> bool:
        """True when a link already reaches a live card without needing repair.

        An exact live card id always counts. A live title, topic or alias counts
        only while no retired card shares the name. Otherwise a live card could
        claim a retired card's name and skip the collision check in `_pick`.
        """
        if name.strip().lower() in self.ids.get(scope, set()):
            return True
        if not self.live.get((scope, _fold(name))):
            return False
        return not self.retired.get((scope, _loose(name)))

    def resolve(self, scope: str, raw: str, trust: Any) -> tuple[str | None, str | None]:
        """Return the live card a broken link should name, or the reason to skip it."""
        target, reason = self._pick(scope, raw)
        if target is not None:
            reason = self._admit(scope, target, trust)
            if reason:
                return None, reason
        return target, reason

    def _pick(self, scope: str, raw: str) -> tuple[str | None, str | None]:
        """Name the one live card a broken link should reach. Retired names use the successor rule only."""
        key = (scope, _loose(_target(raw)))
        retired, matches = self.retired.get(key, set()), self.loose.get(key, set())
        if retired:
            # A retired name shared with another retired or live card has no clear owner.
            if len(retired) > 1 or matches:
                return None, "link_ambiguous_target"
            value = self.successor.get((scope, next(iter(retired))))
            if value is None:
                return None, "link_no_target"
            successor = _target(value.strip("\"'"))
            if successor.startswith(scope + "/"):
                successor = successor[len(scope) + 1 :]
            if "/" in successor:
                return None, "link_cross_scope_target"
            if successor.lower() in self.ids.get(scope, set()):
                return successor.lower(), None
            return None, "link_no_target"
        if len(matches) > 1:
            return None, "link_ambiguous_target"
        if matches:
            return next(iter(matches)), None
        if any(other != scope for other in self.scopes.get(key[1], ())):
            return None, "link_cross_scope_target"
        return None, "link_no_target"


def _code_spans(text: str) -> list[tuple[int, int]]:
    """Inline code spans, opened and closed by backtick runs of equal length.

    A backtick escaped by an odd run of backslashes is literal and cannot open a
    span, as in CommonMark. Escapes do not apply inside a span, so a closer is
    matched by length alone.
    """
    spans: list[tuple[int, int]] = []
    size, at = len(text), 0
    while at < size:
        if text[at] != "`":
            at += 1
            continue
        slashes = 0
        while at - slashes - 1 >= 0 and text[at - slashes - 1] == "\\":
            slashes += 1
        if slashes % 2:
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
                if index.valid(head["scope"], name):
                    continue
                stats["broken"] += 1
                if reason or not block.block_id:
                    _bump(skipped, reason or "link_missing_pin")
                    continue
                target, why = index.resolve(head["scope"], raw, head.get("trust"))
                if target is None:
                    _bump(skipped, why or "link_no_target")
                    continue
                # Keep a folder prefix, extension and surrounding space as written.
                lead = raw[: len(raw) - len(raw.lstrip())]
                tail = raw[len(raw.rstrip()) :]
                bare = raw.strip()
                prefix = next((p for p in PREFIXES if bare.startswith(p)), "")
                suffix = ".md" if bare.endswith(".md") else ""
                written = prefix + target + suffix
                # The new link must read back, the way any link is read, as exactly the
                # chosen card. A card id such as `guide.md` would otherwise be written as
                # a link that `_target` reads as `guide`.
                if index.live.get((head["scope"], _fold(_target(written)))) != {target}:
                    _bump(skipped, "link_unrepresentable_target")
                    continue
                edits.append((start, end, lead + written + tail))
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
