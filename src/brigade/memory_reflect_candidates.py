"""Bounded, deterministic reflection from canonical cards and cited evidence.

These candidates are disposable previews. No function in this module writes a
card or calls a model, and a polarity signal never resolves a disagreement.
"""

from __future__ import annotations

import difflib
import json
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from . import memory_cmd
from . import memory_proposal_io as io
from .card_fingerprint import _upsert_frontmatter_fields, content_fingerprint, normalize_card_content
from .card_identity import CardIdentity, card_identity, identity_collision_aliases
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
    care = replace(care, card_roots=tuple(root.rstrip("/") for root in care.card_roots))
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


def read_cards(
    target: Path, care: memory_cmd.MemoryCareConfig, ledger: EvidenceLog, scope: str | None
) -> tuple[list[Card], list[dict[str, str]]]:
    paths = memory_cmd._iter_cards(target, care)
    if len(paths) > io.MAX_FILES:
        raise ValueError("canonical corpus file limit exceeded")
    cards: list[Card] = []
    size = 0
    skipped: list[dict[str, str]] = []
    for path in paths:
        rel = path.relative_to(target).as_posix()
        try:
            raw = io.read_bytes(path, limit=io.MAX_CARD_BYTES)
        except PermissionError:
            skipped.append({"path": rel, "reason": "canonical card is unreadable"})
            continue
        except OSError as exc:
            if str(exc) != "file must be regular and single-link":
                raise
            skipped.append({"path": rel, "reason": str(exc)})
            continue
        except ValueError as exc:
            if str(exc) != "file size limit exceeded":
                raise
            skipped.append({"path": rel, "reason": "canonical card byte limit exceeded"})
            continue
        assert raw is not None
        size += len(raw)
        if size > io.MAX_CORPUS_BYTES:
            raise ValueError("canonical corpus byte limit exceeded")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            skipped.append({"path": rel, "reason": "canonical card is not UTF-8"})
            continue
        meta, valid = memory_cmd._parse_frontmatter(text.replace("\r\n", "\n"))
        if not valid:
            if scope is None or meta.get("scope", "repo") == scope:
                skipped.append({"path": rel, "reason": "malformed canonical frontmatter"})
            continue
        if len(memory_cmd._evidence_refs(meta)) > io.MAX_LIST:
            skipped.append({"path": rel, "reason": "card evidence list limit exceeded"})
            continue
        identity = card_identity(meta, rel)
        rows = [row for key in (identity.card_id, *identity.aliases) for row in ledger.by_identity.get(key, [])]
        cards.append(Card(rel, text, meta, identity, rows))
    collisions = identity_collision_aliases([(card.path, card.identity) for card in cards])
    ambiguous = {path for paths in collisions.values() for path in paths}
    cards = [card for card in cards if scope is None or card.meta.get("scope", "repo") == scope]
    skipped.extend(
        {"path": card.path, "reason": "ambiguous canonical card identity"} for card in cards if card.path in ambiguous
    )
    return [card for card in cards if card.path not in ambiguous], sorted(skipped, key=lambda item: item["path"])


EVIDENCE_ROOTS = (
    ".brigade/work/verify-runs",
    ".brigade/receipts",
    ".brigade/evidence",
    "memory/evidence",
    "memory/handoff-inbox",
    ".claude/memory-handoffs",
    ".codex/memory-handoffs",
    ".agents/memory-handoffs",
    ".cursor/memory-handoffs",
    ".gemini/memory-handoffs",
)


def _resolved(target: Path, refs: list[str]) -> list[str]:
    result: set[str] = set()
    if len(refs) > io.MAX_LIST:
        raise ValueError("card evidence list limit exceeded")
    for ref in refs:
        if not ref or len(ref) > 1024 or any(ord(c) < 32 for c in ref):
            continue
        try:
            bundle = memory_cmd._miseledger_evidence_id(ref)
            if bundle is not None:
                path = memory_cmd._miseledger_evidence_path(bundle)
            else:
                rel = io.relative(ref.removeprefix("receipt:").split("#", 1)[0])
                if not any(rel.startswith(root + "/") for root in EVIDENCE_ROOTS):
                    continue
                path = target / rel
            if path is not None and io.file_mode(path, absent=True) is not None:
                result.add(ref)
        except (OSError, ValueError):
            continue
    return sorted(result)


def _structured_preview(card: Card) -> bool:
    """Refuse citation formats the scalar evidence upserter cannot preserve."""
    for key in ("evidence", "sources", "source", "refs", "links"):
        value = card.meta.get(key)
        if value not in (None, ""):
            if key != "evidence" or (isinstance(value, str) and value.startswith("[")):
                return True
            break
    active = ""
    for line in card.text.replace("\r\n", "\n").split("\n")[1:]:
        if line.strip() == "---":
            break
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line[0].isspace() or line.startswith("- "):
            if active in {"evidence", "sources", "source", "refs", "links", "proof_count", "last_consolidated"}:
                return True
        else:
            active = line.split(":", 1)[0].strip()
    return False


def _diff(card: Card, fields: dict[str, Any]) -> str:
    if _structured_preview(card):
        return ""
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


def _trust_labels(meta: dict[str, Any]) -> set[str]:
    contexts = [meta]
    labels: set[str] = set()
    for context in contexts:
        for key in ("trust", "trust_label", "trust_state", "provenance", "metadata"):
            value = context.get(key)
            if isinstance(value, str) and value.startswith("{"):
                try:
                    value = json.loads(value)
                except ValueError:
                    labels.add("unknown")
            if isinstance(value, dict):
                # Canonical metadata only nests these channels a few levels.
                if len(contexts) < 8:
                    contexts.append(value)
                if key == "trust":
                    labels.add(str(value.get("label", "unknown")))
            elif value is not None and key in {"trust", "trust_label", "trust_state"}:
                labels.add(str(value))
    return labels or {"unknown"}


def _candidate(
    kind: str,
    cards: list[Card],
    evidence: list[str],
    contrary: list[str],
    *,
    diff: str = "",
    review: list[str] | None = None,
    summary: str,
) -> dict[str, Any]:
    cards = sorted(cards, key=lambda c: c.identity.card_id)
    scopes = sorted({str(card.meta.get("scope", "repo")) for card in cards})
    trusts = {label for card in cards for label in _trust_labels(card.meta)}
    review = sorted(set(review or []))
    review_only = bool(
        contrary or review or len(scopes) > 1 or len(trusts) > 1 or any(_structured_preview(c) for c in cards)
    )
    payload: dict[str, Any] = {
        "schema": "brigade.memory-reflect-candidate.v1",
        "kind": kind,
        "card_ids": [card.identity.card_id for card in cards],
        "card_paths": {card.identity.card_id: card.path for card in cards},
        "base_sha256": {card.identity.card_id: io.sha256(card.text.encode()) for card in cards},
        "evidence_ids": sorted(set(evidence)),
        "proof_count": len(set(evidence) - set(contrary) - set(review)),
        "review_evidence_ids": review,
        "contrary_ids": sorted(set(contrary)),
        "tier": 2 if review_only else 0,
        "scope": scopes,
        "diff": "" if review_only else diff,
        "summary": summary,
    }
    payload["candidate_id"] = "candidate-" + io.sha256(io.json_bytes(payload))[:24]
    return payload


def generate(
    target: Path, care: memory_cmd.MemoryCareConfig, ledger: EvidenceLog, cards: list[Card]
) -> list[dict[str, Any]]:
    """Return stable candidates only when every cited id resolves locally."""
    candidates: list[dict[str, Any]] = []
    citations: dict[str, list[str]] = {}
    contrary_by_card: dict[str, list[str]] = {}
    review_by_card: dict[str, list[str]] = {}
    groups: dict[str, list[Card]] = {}
    finding_config = replace(care, enabled_checks=("unconsolidated-evidence",))
    for card in cards:
        refs = memory_cmd._evidence_refs(card.meta)
        current = _resolved(target, refs)
        # Keep the newest distinct ids, independent of append order or aliases.
        newest = dict.fromkeys(
            row["evidence_id"]
            for row in sorted(card.rows, key=lambda row: (_sort_timestamp(row["ts"]), row["evidence_id"]), reverse=True)
        )
        row_ids = set(_resolved(target, list(newest)[: io.MAX_LIST]))
        evidence = sorted(set(current) | row_ids)
        contrary = sorted({row["evidence_id"] for row in card.rows if row["kind"] == "opposite-polarity"} & row_ids)
        review = sorted({row["evidence_id"] for row in card.rows if row["kind"] == "near-match"} & row_ids)
        citations[card.path] = evidence
        contrary_by_card[card.path] = contrary
        review_by_card[card.path] = review
        if normalize_card_content(card.text):
            groups.setdefault(content_fingerprint(card.text), []).append(card)
        findings = ledger.findings(target, finding_config, card.path, card.identity, card.meta)
        stale_ids = sorted(set(findings[0]["evidence_references"]) & row_ids) if findings else []
        if stale_ids:
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
                    review=review,
                    diff="" if contrary else _diff(card, fields),
                    summary="Review matched evidence newer than the last consolidation or review.",
                )
            )
        supporting = {
            row["evidence_id"] for row in card.rows if row["kind"] in {"reinforced", "outcome"}
        } & row_ids - set(contrary) - set(review)
        already_reinforced = {row["evidence_id"] for row in card.rows if row["kind"] == "reinforced"}
        additions = supporting - set(refs) - already_reinforced
        if additions:
            proof = sorted(set(current) | supporting)
            candidates.append(
                _candidate(
                    "reinforce",
                    [card],
                    proof,
                    contrary,
                    review=sorted(set(review) & set(proof)),
                    diff=_diff(card, {"evidence": sorted(set(refs) | set(proof)), "proof_count": len(proof)}),
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
                review=sorted({ref for card in group for ref in review_by_card[card.path]}),
                summary="would merge exact normalized duplicates: " + ", ".join(sorted(c.path for c in group)),
            )
        )
    priority = {"opposite-polarity": 0, "stale-newer-evidence": 1, "reinforce": 2, "duplicate": 3}
    return sorted(candidates, key=lambda c: (priority[c["kind"]], c["card_ids"], c["candidate_id"]))


def _sort_timestamp(value: str) -> datetime:
    timestamp = _timestamp(value)
    assert timestamp is not None  # The evidence log validates every timestamp.
    return timestamp
