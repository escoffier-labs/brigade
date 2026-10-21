"""Bounded, deterministic reflection from canonical cards and cited evidence.

These candidates are disposable previews. No function in this module writes a
card or calls a model, and a polarity signal never resolves a disagreement.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from . import memory_cmd
from . import memory_proposal_io as io
from .card_fingerprint import _upsert_frontmatter_fields, content_fingerprint, normalize_card_content
from .card_identity import CardIdentity, card_identity
from .memory_evidence_log import EvidenceLog, _timestamp


class UnsafeState(ValueError):
    """Reflect storage would write inside a configured card root."""


@dataclass
class Card:
    path: str
    text: str
    meta: dict[str, Any]
    identity: CardIdentity
    rows: list[dict[str, Any]]


def configuration(target: Path) -> memory_cmd.MemoryCareConfig:
    """Validate every configured path before using the care reader."""
    io.read_bytes(target / memory_cmd.CONFIG_REL_PATH, absent=True)
    care = memory_cmd._config_or_default(target)
    state = target / ".brigade/memory/reflect"
    for root in care.card_roots:
        if state.is_relative_to(target / root) or (target / root).is_relative_to(state):
            raise UnsafeState("reflect state overlaps a canonical card root")
    for values in (care.card_roots, care.include_paths, care.exclude_paths):
        if len(values) > io.MAX_LIST:
            raise ValueError("configuration list limit exceeded")
        for value in values:
            io.relative(value)
    for root in care.card_roots:
        try:
            with io.directory(target / root):
                pass
        except FileNotFoundError:
            pass
    return care


def read_cards(target: Path, care: memory_cmd.MemoryCareConfig, ledger: EvidenceLog, scope: str | None) -> list[Card]:
    paths = memory_cmd._iter_cards(target, care)
    if len(paths) > io.MAX_FILES:
        raise ValueError("canonical corpus file limit exceeded")
    cards: list[Card] = []
    size = 0
    identities: set[str] = set()
    for path in paths:
        raw = io.read_bytes(path, limit=io.MAX_CARD_BYTES)
        assert raw is not None
        size += len(raw)
        if size > io.MAX_CORPUS_BYTES:
            raise ValueError("canonical corpus byte limit exceeded")
        text = raw.decode("utf-8")
        meta, valid = memory_cmd._parse_frontmatter(text.replace("\r\n", "\n"))
        if not valid:
            raise ValueError("malformed canonical frontmatter")
        if scope is not None and meta.get("scope", "repo") != scope:
            continue
        rel = path.relative_to(target).as_posix()
        identity = card_identity(meta, rel)
        if identity.card_id in identities:
            raise ValueError("ambiguous canonical card identity")
        identities.add(identity.card_id)
        rows = [row for key in (identity.card_id, *identity.aliases) for row in ledger.by_identity.get(key, [])]
        cards.append(Card(rel, text, meta, identity, rows))
    return cards


def _resolved(target: Path, refs: list[str], logged: set[str]) -> list[str]:
    result: set[str] = set()
    if len(refs) > io.MAX_LIST:
        raise ValueError("card evidence list limit exceeded")
    for ref in refs:
        if not ref or len(ref) > 1024 or any(ord(c) < 32 for c in ref):
            continue
        if ref in logged:
            result.add(ref)
            continue
        try:
            rel = io.relative(ref.removeprefix("receipt:").split("#", 1)[0])
            if io.read_bytes(target / rel, absent=True) is not None:
                result.add(ref)
        except (OSError, ValueError):
            continue
    return sorted(result)


def _diff(card: Card, fields: dict[str, Any]) -> str:
    # Patch only the frontmatter. Preserve every body byte and line ending.
    newline = "\r\n" if card.text.startswith("---\r\n") else "\n"
    lines = card.text.splitlines(keepends=True)
    end = next(i for i, line in enumerate(lines[1:], 1) if line.strip() == "---")
    header = "".join(lines[: end + 1]).replace("\r\n", "\n")
    updated = _upsert_frontmatter_fields(header, fields).replace("\n", newline)
    after = updated + "".join(lines[end + 1 :])
    return "".join(
        difflib.unified_diff(
            card.text.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{card.path}",
            tofile=f"b/{card.path}",
        )
    )


def _candidate(
    kind: str,
    cards: list[Card],
    evidence: list[str],
    contrary: list[str],
    *,
    diff: str = "",
    summary: str,
) -> dict[str, Any]:
    cards = sorted(cards, key=lambda c: c.identity.card_id)
    scopes = sorted({str(card.meta.get("scope", "repo")) for card in cards})
    trusts = {str(card.meta.get("trust", card.meta.get("trust_state", "unknown"))) for card in cards}
    payload: dict[str, Any] = {
        "schema": "brigade.memory-reflect-candidate.v1",
        "kind": kind,
        "card_ids": [card.identity.card_id for card in cards],
        "card_paths": {card.identity.card_id: card.path for card in cards},
        "base_sha256": {card.identity.card_id: io.sha256(card.text.encode()) for card in cards},
        "evidence_ids": sorted(set(evidence)),
        "proof_count": len(set(evidence)),
        "contrary_ids": sorted(set(contrary)),
        "tier": 2 if contrary or len(scopes) > 1 or len(trusts) > 1 else 0,
        "scope": scopes,
        "diff": diff,
        "summary": summary,
    }
    payload["candidate_id"] = "candidate-" + io.sha256(io.json_bytes(payload))[:24]
    return payload


def generate(
    target: Path, care: memory_cmd.MemoryCareConfig, ledger: EvidenceLog, cards: list[Card], limit: int
) -> list[dict[str, Any]]:
    """Return stable candidates only when every cited id resolves locally."""
    logged = {row["evidence_id"] for rows in ledger.by_identity.values() for row in rows}
    candidates: list[dict[str, Any]] = []
    citations: dict[str, list[str]] = {}
    contrary_by_card: dict[str, list[str]] = {}
    groups: dict[str, list[Card]] = {}
    finding_config = replace(care, enabled_checks=("unconsolidated-evidence",))
    for card in cards:
        refs = memory_cmd._evidence_refs(card.meta)
        current = _resolved(target, refs, logged)
        row_ids = {row["evidence_id"] for row in card.rows}
        evidence = sorted(set(current) | row_ids)
        contrary = sorted({row["evidence_id"] for row in card.rows if row["kind"] == "opposite-polarity"})
        citations[card.path] = evidence
        contrary_by_card[card.path] = contrary
        if normalize_card_content(card.text):
            groups.setdefault(content_fingerprint(card.text), []).append(card)
        findings = ledger.findings(target, finding_config, card.path, card.identity, card.meta)
        if findings:
            stale_ids = findings[0]["evidence_references"]
            fields: dict[str, Any] = {
                "evidence": sorted(set(refs) | set(stale_ids)),
                "proof_count": len(set(current) | set(stale_ids)),
                "last_consolidated": max(
                    (row["ts"] for row in card.rows if row["evidence_id"] in stale_ids), key=_sort_timestamp
                ),
            }
            candidates.append(
                _candidate(
                    "stale-newer-evidence",
                    [card],
                    sorted(set(current) | set(stale_ids)),
                    contrary,
                    diff="" if contrary else _diff(card, fields),
                    summary="Review matched evidence newer than the last consolidation or review.",
                )
            )
        supporting = {row["evidence_id"] for row in card.rows if row["kind"] != "opposite-polarity"} - set(contrary)
        additions = supporting - set(refs)
        if additions:
            proof = sorted(set(current) | supporting)
            candidates.append(
                _candidate(
                    "reinforce",
                    [card],
                    proof,
                    contrary,
                    diff=_diff(card, {"evidence": sorted(set(refs) | supporting), "proof_count": len(proof)}),
                    summary="Append cited evidence and derive its distinct proof count.",
                )
            )
        if contrary:
            candidates.append(
                _candidate(
                    "opposite-polarity",
                    [card],
                    contrary,
                    contrary,
                    summary="Opposite-polarity flag only. Human review must decide the relationship.",
                )
            )
    for group in groups.values():
        if len(group) < 2:
            continue
        evidence = sorted({ref for card in group for ref in citations[card.path]})
        if not evidence:
            continue
        contrary = sorted({ref for card in group for ref in contrary_by_card[card.path]})
        candidates.append(
            _candidate(
                "duplicate",
                group,
                evidence,
                contrary,
                summary="would merge exact normalized duplicates: " + ", ".join(sorted(c.path for c in group)),
            )
        )
    return sorted(candidates, key=lambda c: (c["kind"], c["card_ids"], c["candidate_id"]))[:limit]


def _sort_timestamp(value: str) -> datetime:
    timestamp = _timestamp(value)
    assert timestamp is not None  # The evidence log validates every timestamp.
    return timestamp
