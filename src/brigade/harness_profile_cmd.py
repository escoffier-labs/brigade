"""User-scope harness profile command logic.

Issue #438: managed-block parsing, ownership-state validation, skill/artifact
reconciliation, and aggregate install/uninstall/doctor. This module owns the
managed instruction block surface plan; profile records and native path
resolution live in the sibling ``harness_profiles`` module.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import harness_profiles

_RECOVERY_COMMAND = "brigade harness install <harness> --scope user --adopt --write"


@dataclass(frozen=True)
class SurfacePlan:
    surface: str
    path: Path
    status: str
    action: str
    desired_digest: str | None = None
    rendered: str | None = None
    detail: str | None = None


def digest_text(text: str) -> str:
    """Return the sha256 hex digest of ``text`` encoded as UTF-8."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _block(start: str, body: str, end: str) -> str:
    return f"{start}\n{body}\n{end}\n"


def _find_markers(text: str, start: str, end: str) -> tuple[int, int] | None:
    if text.count(start) != 1 or text.count(end) != 1:
        return None
    start_pos = text.find(start)
    end_pos = text.find(end)
    if end_pos <= start_pos:
        return None
    after_start = start_pos + len(start)
    before_end = end_pos
    if after_start >= len(text) or text[after_start] != "\n":
        return None
    if before_end <= 0 or text[before_end - 1] != "\n":
        return None
    return start_pos, end_pos


def _split_components(text: str, start: str, end: str) -> tuple[str, str, str] | None:
    found = _find_markers(text, start, end)
    if found is None:
        return None
    start_pos, end_pos = found
    body = text[start_pos + len(start) + 1 : end_pos - 1]
    before = text[:start_pos]
    block_end = end_pos + len(end)
    if block_end < len(text) and text[block_end] == "\n":
        block_end += 1
    after = text[block_end:]
    return before, body, after


def plan_instruction(
    *,
    path: Path,
    desired: str,
    state: dict[str, Any],
    adopt: bool = False,
) -> SurfacePlan:
    """Plan an install/update of the managed instruction block in ``path``."""
    start = harness_profiles.INSTRUCTION_START
    end = harness_profiles.INSTRUCTION_END
    desired_digest_body = digest_text(desired)
    instructions = state.get("instructions", {}) if state else {}
    owned_digest = instructions.get("digest") if isinstance(instructions, dict) else None

    if not path.exists():
        rendered = _block(start, desired, end)
        return SurfacePlan(
            surface="instruction",
            path=path,
            status="missing",
            action="create",
            desired_digest=desired_digest_body,
            rendered=rendered,
        )

    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return SurfacePlan(
            surface="instruction",
            path=path,
            status="conflict",
            action="preserve",
            desired_digest=desired_digest_body,
            detail=str(exc),
        )

    components = _split_components(text, start, end)
    if components is None:
        if start not in text and end not in text:
            sep = "\n" if text else ""
            before = text
            rendered = before + sep + _block(start, desired, end)
            return SurfacePlan(
                surface="instruction",
                path=path,
                status="missing",
                action="create",
                desired_digest=digest_text(before + sep + desired),
                rendered=rendered,
            )
        return SurfacePlan(
            surface="instruction",
            path=path,
            status="conflict",
            action="preserve",
            desired_digest=desired_digest_body,
            detail=f"managed instruction markers are malformed; recover with: {_RECOVERY_COMMAND}",
        )

    before, body, after = components
    live_digest = digest_text(before + body + after)
    desired_digest = digest_text(before + desired + after)

    if live_digest == desired_digest:
        return SurfacePlan(
            surface="instruction",
            path=path,
            status="current",
            action="none",
            desired_digest=desired_digest,
            rendered=None,
        )
    if owned_digest is not None and live_digest == owned_digest:
        rendered = before + _block(start, desired, end) + after
        return SurfacePlan(
            surface="instruction",
            path=path,
            status="stale",
            action="update",
            desired_digest=desired_digest,
            rendered=rendered,
        )
    if adopt:
        rendered = before + _block(start, desired, end) + after
        return SurfacePlan(
            surface="instruction",
            path=path,
            status="stale",
            action="update",
            desired_digest=desired_digest,
            rendered=rendered,
        )
    return SurfacePlan(
        surface="instruction",
        path=path,
        status="conflict",
        action="preserve",
        desired_digest=desired_digest,
        detail=f"foreign managed instruction block; recover with: {_RECOVERY_COMMAND}",
    )


def plan_instruction_removal(*, path: Path, state: dict[str, Any]) -> SurfacePlan:
    """Plan removal of the managed instruction block owned by ``state``."""
    start = harness_profiles.INSTRUCTION_START
    end = harness_profiles.INSTRUCTION_END
    instructions = state.get("instructions", {}) if state else {}
    owned_digest = instructions.get("digest") if isinstance(instructions, dict) else None

    if not path.exists():
        return SurfacePlan(
            surface="instruction",
            path=path,
            status="absent",
            action="none",
            desired_digest=None,
            rendered=None,
        )

    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return SurfacePlan(
            surface="instruction",
            path=path,
            status="conflict",
            action="preserve",
            desired_digest=None,
            detail=str(exc),
        )

    components = _split_components(text, start, end)
    if components is None:
        if start not in text and end not in text:
            return SurfacePlan(
                surface="instruction",
                path=path,
                status="absent",
                action="none",
                desired_digest=None,
                rendered=None,
            )
        return SurfacePlan(
            surface="instruction",
            path=path,
            status="conflict",
            action="preserve",
            desired_digest=None,
            detail=f"managed instruction markers are malformed; recover with: {_RECOVERY_COMMAND}",
        )

    before, body, after = components
    live_digest = digest_text(before + body + after)
    if owned_digest is not None and live_digest == owned_digest:
        if before.endswith("\n"):
            rendered = before[:-1] + after
        else:
            rendered = before + after
        return SurfacePlan(
            surface="instruction",
            path=path,
            status="managed",
            action="remove",
            desired_digest=owned_digest,
            rendered=rendered,
        )
    return SurfacePlan(
        surface="instruction",
        path=path,
        status="conflict",
        action="preserve",
        desired_digest=owned_digest,
        detail=f"managed instruction block was edited; recover with: {_RECOVERY_COMMAND}",
    )
