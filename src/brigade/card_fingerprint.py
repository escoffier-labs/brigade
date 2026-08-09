"""Content fingerprints and near-match helpers for memory cards.

Used by ingest reinforcement (#724) and memory-care fingerprint backfill.
Normalization is stable and punctuation-agnostic so reworded restatements can
still share a high Jaccard score while exact restatements share a hash.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

# Near-match threshold from issue #724 (~0.9 token Jaccard).
NEAR_MATCH_THRESHOLD = 0.9

_PUNCT_RE = re.compile(r"[^\w\s]+", re.UNICODE)
_WS_RE = re.compile(r"\s+")
_TOKEN_RE = re.compile(r"[a-z0-9_]+", re.UNICODE)

# Closed antonym pairs for opposite-polarity detection on near-matches.
_POLARITY_PAIRS: tuple[tuple[str, str], ...] = (
    ("works", "fails"),
    ("work", "fail"),
    ("working", "failing"),
    ("succeeds", "fails"),
    ("success", "failure"),
    ("passes", "fails"),
    ("pass", "fail"),
    ("passed", "failed"),
    ("enabled", "disabled"),
    ("true", "false"),
    ("yes", "no"),
    ("allows", "blocks"),
    ("allow", "block"),
    ("allowed", "blocked"),
    ("safe", "unsafe"),
    ("valid", "invalid"),
    ("correct", "incorrect"),
    ("present", "absent"),
    ("required", "forbidden"),
    ("supports", "rejects"),
    ("support", "reject"),
)

_NEGATION_TOKENS = frozenset(
    {
        "not",
        "never",
        "no",
        "cannot",
        "cant",
        "doesnt",
        "dont",
        "isnt",
        "wasnt",
        "without",
        "hardly",
        "barely",
    }
)


def strip_frontmatter(text: str) -> str:
    """Return card body with a leading YAML frontmatter fence removed."""
    if not text.startswith("---"):
        return text
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return text
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            return "\n".join(lines[index + 1 :])
    return text


def normalize_card_content(text: str) -> str:
    """Normalize card content for fingerprinting and similarity.

    Strips frontmatter, lowercases, removes punctuation, and collapses
    whitespace. The result is the canonical string hashed into ``fingerprint``.
    """
    body = strip_frontmatter(text)
    lowered = body.lower()
    without_punct = _PUNCT_RE.sub(" ", lowered)
    return _WS_RE.sub(" ", without_punct).strip()


def content_fingerprint(text: str) -> str:
    """Return a stable hex digest of normalized card content."""
    normalized = normalize_card_content(text)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def content_tokens(text: str) -> frozenset[str]:
    """Token set used for Jaccard near-match scoring."""
    normalized = normalize_card_content(text)
    return frozenset(_TOKEN_RE.findall(normalized))


def jaccard_similarity(left: str, right: str) -> float:
    """Token Jaccard similarity over normalized card content."""
    a = content_tokens(left)
    b = content_tokens(right)
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    intersection = len(a & b)
    union = len(a | b)
    return intersection / union if union else 0.0


def opposite_polarity(left: str, right: str) -> bool:
    """Return True when near-similar texts disagree on polarity.

    Detects closed antonym pairs (works/fails, …) and simple negation of a
    shared content token. Same-polarity restatements return False.
    """
    left_tokens = set(content_tokens(left))
    right_tokens = set(content_tokens(right))
    if not left_tokens or not right_tokens:
        return False

    for a, b in _POLARITY_PAIRS:
        left_has_a = a in left_tokens
        left_has_b = b in left_tokens
        right_has_a = a in right_tokens
        right_has_b = b in right_tokens
        if left_has_a and right_has_b and not left_has_b and not right_has_a:
            return True
        if left_has_b and right_has_a and not left_has_a and not right_has_b:
            return True

    left_negated = _negated_content_tokens(left_tokens)
    right_negated = _negated_content_tokens(right_tokens)
    shared = left_tokens & right_tokens
    # One side affirms a token the other negates (and the affirming side does
    # not also carry that negation).
    if left_negated & (right_tokens - right_negated) and not (left_negated & right_negated):
        return True
    if right_negated & (left_tokens - left_negated) and not (left_negated & right_negated):
        return True
    # Shared content with asymmetric negation markers is also opposite.
    if shared and (left_negated - right_negated or right_negated - left_negated):
        if (left_tokens & _NEGATION_TOKENS) ^ (right_tokens & _NEGATION_TOKENS):
            return True
    return False


def _negated_content_tokens(tokens: set[str]) -> set[str]:
    """Tokens that appear alongside a negation marker in the same token set.

    This is intentionally coarse (bag-of-words): good enough to separate
    "X works" from "X does not work" when Jaccard is already high.
    """
    if not (tokens & _NEGATION_TOKENS):
        return set()
    return {token for token in tokens if token not in _NEGATION_TOKENS and not token.isdigit()}


@dataclass(frozen=True)
class CardMatch:
    """A fingerprint or near-match against an existing card."""

    path: Path
    kind: str  # exact | near
    similarity: float
    opposite_polarity: bool = False


@dataclass(frozen=True)
class IndexedCard:
    path: Path
    text: str
    fingerprint: str
    tokens: frozenset[str]


def index_cards(cards_root: Path) -> list[IndexedCard]:
    """Load cards under ``cards_root`` for fingerprint / near-match lookup."""
    if not cards_root.is_dir():
        return []
    indexed: list[IndexedCard] = []
    for path in sorted(cards_root.rglob("*.md")):
        if not path.is_file():
            continue
        # Decay outputs are not canonical cards.
        if "decay" in path.relative_to(cards_root).parts:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        stored = _stored_fingerprint(text)
        fingerprint = stored or content_fingerprint(text)
        indexed.append(
            IndexedCard(
                path=path,
                text=text,
                fingerprint=fingerprint,
                tokens=content_tokens(text),
            )
        )
    return indexed


def find_card_match(content: str, indexed: list[IndexedCard]) -> CardMatch | None:
    """Return the best exact or near match for ``content``, if any."""
    fingerprint = content_fingerprint(content)
    for card in indexed:
        if card.fingerprint == fingerprint:
            return CardMatch(path=card.path, kind="exact", similarity=1.0)

    tokens = content_tokens(content)
    if not tokens:
        return None
    best: CardMatch | None = None
    for card in indexed:
        if not card.tokens:
            continue
        intersection = len(tokens & card.tokens)
        union = len(tokens | card.tokens)
        similarity = intersection / union if union else 0.0
        if similarity < NEAR_MATCH_THRESHOLD:
            continue
        polarity = opposite_polarity(content, card.text)
        candidate = CardMatch(
            path=card.path,
            kind="near",
            similarity=similarity,
            opposite_polarity=polarity,
        )
        if best is None or candidate.similarity > best.similarity:
            best = candidate
    return best


def ensure_fingerprint_frontmatter(content: str) -> str:
    """Insert ``fingerprint:`` into card frontmatter when missing."""
    fingerprint = content_fingerprint(content)
    meta, has_frontmatter = _parse_frontmatter_lines(content)
    if not has_frontmatter:
        return content
    if str(meta.get("fingerprint") or "").strip():
        # Keep an existing fingerprint even if stale; backfill owns repairs.
        return content
    return _insert_frontmatter_fields(content, {"fingerprint": fingerprint})


def reinforce_existing_card(
    card_path: Path,
    *,
    today: date,
    evidence_pointer: str,
    fingerprint: str | None = None,
) -> dict[str, Any]:
    """Reinforce an existing card in place and return the applied patch.

    Bumps ``last_reviewed``, increments ``reinforcements``, ensures
    ``fingerprint``, and appends ``evidence_pointer`` to evidence metadata.
    """
    text = card_path.read_text(encoding="utf-8", errors="replace")
    meta, has_frontmatter = _parse_frontmatter_lines(text)
    if not has_frontmatter:
        raise ValueError(f"card missing frontmatter: {card_path}")

    fp = fingerprint or str(meta.get("fingerprint") or "").strip() or content_fingerprint(text)
    prior = meta.get("reinforcements", 0)
    try:
        reinforcements = int(prior) + 1
    except (TypeError, ValueError):
        reinforcements = 1

    evidence = _evidence_list(meta)
    pointer = evidence_pointer.strip()
    if pointer and pointer not in evidence:
        evidence.append(pointer)

    updates: dict[str, Any] = {
        "last_reviewed": today.isoformat(),
        "reinforcements": reinforcements,
        "fingerprint": fp,
        "evidence": evidence,
    }
    patched = _upsert_frontmatter_fields(text, updates)
    card_path.write_text(patched, encoding="utf-8")
    return updates


def _stored_fingerprint(text: str) -> str | None:
    meta, has_frontmatter = _parse_frontmatter_lines(text)
    if not has_frontmatter:
        return None
    value = str(meta.get("fingerprint") or "").strip()
    return value or None


def _parse_frontmatter_lines(text: str) -> tuple[dict[str, Any], bool]:
    if not text.startswith("---\n") and text != "---":
        if not text.startswith("---\r\n"):
            return {}, False
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, False
    end = None
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            end = index
            break
    if end is None:
        return {}, False
    data: dict[str, Any] = {}
    for raw in lines[1:end]:
        if ":" not in raw:
            continue
        key, value = raw.split(":", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if value.startswith("[") and value.endswith("]"):
            try:
                import ast

                parsed = ast.literal_eval(value)
            except (SyntaxError, ValueError):
                parsed = value
            data[key] = parsed
        elif value.lower() in {"true", "false"}:
            data[key] = value.lower() == "true"
        else:
            data[key] = value.strip("'\"")
    return data, True


def _evidence_list(meta: dict[str, Any]) -> list[str]:
    value = meta.get("evidence", meta.get("sources"))
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    rendered = str(value).strip()
    return [rendered] if rendered else []


def _render_frontmatter_value(value: Any) -> str:
    if isinstance(value, list):
        import json

        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _insert_frontmatter_fields(text: str, fields: dict[str, Any]) -> str:
    lines = text.split("\n")
    closing = next(i for i, line in enumerate(lines[1:], start=1) if line.strip() == "---")
    additions = [f"{key}: {_render_frontmatter_value(value)}" for key, value in fields.items()]
    return "\n".join(lines[:closing] + additions + lines[closing:])


def _upsert_frontmatter_fields(text: str, fields: dict[str, Any]) -> str:
    lines = text.split("\n")
    closing = next(i for i, line in enumerate(lines[1:], start=1) if line.strip() == "---")
    remaining = dict(fields)
    for index in range(1, closing):
        line = lines[index]
        if ":" not in line:
            continue
        key = line.split(":", 1)[0].strip()
        if key in remaining:
            lines[index] = f"{key}: {_render_frontmatter_value(remaining.pop(key))}"
    if remaining:
        additions = [f"{key}: {_render_frontmatter_value(value)}" for key, value in remaining.items()]
        lines = lines[:closing] + additions + lines[closing:]
    return "\n".join(lines)
