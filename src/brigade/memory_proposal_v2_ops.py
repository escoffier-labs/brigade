"""Pure block addressing and bounded typed operations for reviewed proposals."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from . import memory_proposal_io as io
from . import memory_proposals as legacy
from .memory_reflect_candidates import _evidence_id
from .memory_claim_blocks import claim_spans

ANCHOR = re.compile(r" \^(c-[0-9a-f]{6})$")
ANCHOR_ID = re.compile(r"c-[0-9a-f]{6}\Z")
MAX_ANCHORS = 4096


def claim_body(text: str) -> str:
    """Remove suffixes only from addressable claims; retain all other bytes."""
    offset = legacy._body_offset(text)
    body = text[offset:]
    for span in reversed(claim_spans(text, offset)):
        match = ANCHOR.search(text[span.start : span.end])
        if match:
            position = span.start + match.start() - offset
            body = body[:position] + body[span.end - offset :]
    return body


@dataclass(frozen=True)
class Block:
    start: int
    end: int
    text: str
    block_id: str | None

    @property
    def sha256(self) -> str:
        return io.sha256(self.text.encode())


def blocks(text: str) -> list[Block]:
    """Address only structurally supported spans, preserving public offsets."""
    result = []
    for span in claim_spans(text, legacy._body_offset(text)):
        value = text[span.start : span.end]
        match = ANCHOR.search(value)
        result.append(Block(span.start, span.end, value, match[1] if match else None))
    ids = [block.block_id for block in result if block.block_id]
    if len(ids) != len(set(ids)):
        raise legacy.ProposalError("duplicate claim block ids")
    return result


def reservations(text: str) -> set[str]:
    value = legacy._meta(text).get("claim_anchor_ids", [])
    if (
        not isinstance(value, list)
        or len(value) > MAX_ANCHORS
        or any(not isinstance(x, str) or not ANCHOR_ID.fullmatch(x) for x in value)
    ):
        raise legacy.ProposalError("invalid claim anchor reservations")
    used = set(value) | set(re.findall(r"\^(c-[0-9a-f]{6})\b", text))
    if len(used) > MAX_ANCHORS:
        raise legacy.ProposalError("claim anchor reservation limit exceeded")
    return used


def anchor_text(text: str, path: str) -> str:
    """Mint deterministic ids once and retain all reservations after removal."""
    used = reservations(text)
    edits: list[tuple[int, str]] = []
    for block in blocks(text):
        if block.block_id:
            continue
        nonce = 0
        while True:
            ident = "c-" + io.sha256(io.json_bytes([path, io.sha256(text.encode()), block.start, nonce]))[:6]
            if ident not in used:
                break
            nonce += 1
        used.add(ident)
        if len(used) > MAX_ANCHORS:
            raise legacy.ProposalError("claim anchor reservation limit exceeded")
        edits.append((block.end, " ^" + ident))
    if not edits:
        return text
    for position, suffix in reversed(edits):
        text = text[:position] + suffix + text[position:]
    return legacy._update_metadata(text, {"claim_anchor_ids": sorted(used)})


def _history(text: str, old: str, now: str | None = None) -> str:
    old = ANCHOR.sub("", old)
    old = " ".join(old.splitlines())
    suffix = f", now {now}" if now is not None else ""
    return text.rstrip("\r\n") + f"\n\npreviously {old}{suffix}\n"


def _list(meta: dict[str, Any], key: str) -> list[str]:
    value = meta.get(key, [])
    # The canonical evidence reader also accepts one scalar citation. Refuse
    # structured-looking values the frontmatter parser leaves as strings.
    if key == "evidence" and isinstance(value, str) and not value.startswith(("[", "{")):
        value = [value]
    if not isinstance(value, list) or len(value) > io.MAX_LIST or not all(isinstance(x, str) for x in value):
        raise legacy.ProposalError("invalid or oversized existing metadata list")
    return value


def apply_op(
    texts: dict[str, str | None], op: dict[str, Any], *, resolved: set[str], consolidated_at: str | None = None
) -> None:
    """Apply one fully validated op to a private copy, or raise without publishing."""
    common = {"type", "card_path", "block_id", "expected_block_hash"}
    extras = {
        "append_evidence": {"evidence_ids"},
        "replace_block": {"text"},
        "remove_block": set(),
        "add_previously": {"text"},
        "supersede_card": {"other_card"},
        "merge_exact_duplicates": {"other_card"},
    }
    kind = op.get("type")
    fields = set(op) - ({"last_consolidated"} if kind == "append_evidence" else set())
    if not isinstance(kind, str) or kind not in extras or fields != common | extras[kind]:
        raise legacy.ProposalError("invalid typed op schema")
    path = io.relative(op["card_path"])
    text = texts[path]
    if text is None:
        raise legacy.ProposalError("op addresses a removed card")
    if not isinstance(op["block_id"], str) or not ANCHOR_ID.fullmatch(op["block_id"]):
        raise legacy.ProposalError("op block id is not a valid claim anchor")
    found = [block for block in blocks(text) if block.block_id == op["block_id"]]
    if len(found) != 1 or found[0].sha256 != op["expected_block_hash"]:
        raise legacy.ProposalError("block id or expected block hash mismatch")
    block = found[0]
    if kind in ("replace_block", "add_previously"):
        wording = op["text"]
        if not isinstance(wording, str) or not wording.strip() or len(wording.encode()) > io.MAX_REASON_BYTES:
            raise legacy.ProposalError("invalid claim wording")
        legacy._clean(wording)
        # Replacement must remain one paragraph or bullet and cannot forge ids.
        if "\n" in wording or "\r" in wording or "^" in wording:
            raise legacy.ProposalError("claim wording must be one unanchored block")
        parsed = blocks(wording)
        if len(parsed) != 1 or parsed[0].text != wording:
            raise legacy.ProposalError("claim wording is not a claim block")
    if kind == "append_evidence":
        refs = op["evidence_ids"]
        if (
            not isinstance(refs, list)
            or not refs
            or len(refs) > io.MAX_LIST
            or not all(isinstance(ref, str) and ref in resolved for ref in refs)
        ):
            raise legacy.ProposalError("op evidence does not resolve")
        from . import memory_cmd

        meta = legacy._meta(text)
        _list(meta, "evidence")
        # Existing citations may live in any field the canonical reader supports.
        merged = sorted(set(memory_cmd._evidence_refs(meta)) | set(refs))
        if len(merged) > io.MAX_LIST:
            raise legacy.ProposalError("evidence list limit exceeded")
        updates: dict[str, Any] = {
            "evidence": merged,
            "proof_count": len({_evidence_id(ref) for ref in set(merged) & resolved}),
        }
        if "last_consolidated" in op:
            if consolidated_at is None or op["last_consolidated"] != consolidated_at:
                raise legacy.ProposalError("consolidation timestamp does not match cited evidence")
            updates["last_consolidated"] = consolidated_at
        text = legacy._update_metadata(text, updates)
    elif kind == "replace_block":
        replacement = op["text"] + " ^" + str(block.block_id)
        text = text[: block.start] + replacement + text[block.end :]
        text = _history(text, block.text, op["text"])
    elif kind == "remove_block":
        used = reservations(text)
        text = text[: block.start] + text[block.end :]
        text = legacy._update_metadata(text, {"claim_anchor_ids": sorted(used)})
        text = _history(text, block.text)
    elif kind == "add_previously":
        text = _history(text, op["text"])
    else:
        other = io.relative(op["other_card"])
        if other == path or other not in texts or texts[other] is None:
            raise legacy.ProposalError("op requires another retained source card")
        other_text = texts[other]
        assert other_text is not None
        if kind == "supersede_card":
            text = legacy._update_metadata(text, {"status": "superseded", "superseded_by": other})
        else:
            if " ".join(claim_body(text).split()) != " ".join(claim_body(other_text).split()):
                raise legacy.ProposalError("merge requires exact duplicate claims")
            meta, other_meta = legacy._meta(text), legacy._meta(other_text)
            from . import memory_cmd
            from .card_identity import card_identity

            identity = card_identity(other_meta, other)
            own = card_identity(meta, path)
            aliases = sorted((set(_list(meta, "aliases")) | {identity.card_id, *identity.aliases}) - {own.card_id})
            _list(meta, "evidence"), _list(other_meta, "evidence")
            # Citations may live in any field the canonical reader supports.
            refs = sorted(set(memory_cmd._evidence_refs(meta)) | set(memory_cmd._evidence_refs(other_meta)))
            if len(aliases) > io.MAX_LIST or len(refs) > io.MAX_LIST:
                raise legacy.ProposalError("merged metadata list limit exceeded")
            text = legacy._update_metadata(
                text,
                {
                    "aliases": aliases,
                    "evidence": refs,
                    "proof_count": len({_evidence_id(ref) for ref in set(refs) & resolved}),
                    "claim_anchor_ids": sorted(reservations(text) | reservations(other_text)),
                },
            )
            texts[other] = None
    if len(text.encode()) > io.MAX_CARD_BYTES:
        raise legacy.ProposalError("op result exceeds card byte limit")
    legacy._clean(text)
    texts[path] = text
