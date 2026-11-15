"""Preference handoffs use the existing digest-bound proposal review and apply path."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import ingest, instruction_overflow, memory_proposal_io as io, memory_proposals as proposals

SCHEMA = "brigade.memory-preference-proposal.v1"


def replacement_lines(sections: dict[str, str]) -> tuple[str, str]:
    """Validate the optional, backward compatible no-card replacement branch."""
    if (
        sections.get("type", "").strip().lower() != "preference"
        or sections.get("recommended memory action", "").strip().lower() != "no-card"
        or sections.get("target document", "").strip() != "USER.md"
    ):
        raise proposals.ProposalError("preference replacement requires type preference, action no-card and USER.md")
    old = sections.get("superseded preference", "")
    new = sections.get("suggested document content", "")
    for line in (old, new):
        if not line.strip() or len(line.splitlines()) != 1 or line.lstrip().startswith("#"):
            raise proposals.ProposalError("old and new preferences must each be one nonempty non-heading line")
        proposals._clean(line)
    if old == new:
        raise proposals.ProposalError("old and new preferences are identical")
    return old, new


def _compose(target: Path, text: str, ident: str, created: str, reason: str) -> dict[str, Any]:
    sections = ingest.parse(Path("handoff.md"), text.encode())
    if any(key not in ingest.KNOWN_SECTIONS for key in sections):
        raise proposals.ProposalError("preference handoff contains unknown sections")
    proposals._clean(text)
    old, new = replacement_lines(sections)
    care, binding = proposals._configuration(target, require_card_roots=False)
    proposals._outside_roots(proposals._root(target), target, care)
    before = io.text(target / "USER.md", limit=io.MAX_CARD_BYTES, absent=True)
    lines = (before or "").splitlines(keepends=True)
    matches = [i for i, line in enumerate(lines) if line.rstrip("\r\n") == old]
    if not matches:
        raise proposals.ProposalError("preference target line no longer exists in USER.md, nothing changed")
    if len(matches) != 1:
        raise proposals.ProposalError("preference target line is ambiguous in USER.md, nothing changed")
    index = matches[0]
    ending = lines[index][len(old) :]
    lines[index] = new + ending
    after = "".join(lines)
    assert before is not None
    pin = instruction_overflow.direct_pin(target, "USER.md")
    if len(after.encode()) > max(len(before.encode()), pin.act_at or 4000):
        raise proposals.ProposalError("replacement would exceed the USER.md bootstrap budget")
    history_path = f"{proposals.STATE_REL}/{ident}/history.md"
    history = f"# Preference history\n\n## {created}\n\n{old}\n"
    changes = [proposals._change("USER.md", before, after), proposals._change(history_path, None, history)]
    for change in changes:
        mode = io.file_mode(target / change["path"], absent=True)
        change.update(before_mode=mode, after_mode=mode if mode is not None else 0o600)
    proposals._refuse_read_only(care, [change["path"] for change in changes])
    return {
        "schema": SCHEMA,
        "revision": 1,
        "id": ident,
        "binding": binding,
        "scope": binding["available_scope"],
        "reason": reason,
        "created_at": created,
        "relation": "replace-preference",
        "preference_diff": {"old": old, "new": new},
        "history_path": history_path,
        "sources": [{"path": "USER.md", "text": before, "sha256": io.sha256(before.encode())}],
        "handoff_text": text,
        "mutations": changes,
        "relationship": {"relation": "replace-preference", "history_path": history_path},
    }


def create_payload(*, target: Path, handoff_path: str | None, reason: str) -> dict[str, Any]:
    target = proposals._target(target)
    if handoff_path is None:
        raise proposals.ProposalError("replace-preference requires a handoff path")
    handoff_path = io.relative(handoff_path)
    text = io.text(target / handoff_path, limit=io.MAX_CARD_BYTES)
    assert text is not None
    ident = "proposal-" + uuid.uuid4().hex
    proposal = _compose(target, text, ident, datetime.now(timezone.utc).isoformat(), proposals._reason(reason))
    proposal["handoff_path"] = handoff_path
    proposal["proposal_path"] = f"{proposals.STATE_REL}/{ident}/revision-1.json"
    proposal["digest"] = io.sha256(io.json_bytes(proposal))
    with proposals._locked(target):
        validate_live(target, proposal)
        io.write_exclusive(target / proposal["proposal_path"], io.json_bytes(proposal))
    return proposal


def validate_live(target: Path, proposal: dict[str, Any]) -> None:
    """Recompose all mutations before review and again at the publication boundary."""
    fresh = _compose(target, proposal["handoff_text"], proposal["id"], proposal["created_at"], proposal["reason"])
    if any(proposal.get(key) != value for key, value in fresh.items()):
        raise proposals.ProposalError("preference source or owner configuration changed, nothing changed")
    if io.read_bytes(target / io.relative(proposal["history_path"]), absent=True) is not None:
        raise proposals.ProposalError("preference history already exists, nothing changed")
