"""Selection data model: depth + harnesses + owner + includes."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional


KNOWN_DEPTHS = ("repo", "workspace")
KNOWN_HARNESSES = ("claude", "codex", "opencode", "antigravity", "pi", "openclaw", "hermes")
# Writer harness id -> repo-relative handoff inbox dir. Single source of truth;
# install, ingest, doctor, the fleet sweep, and the handoff doctor consume this.
WRITER_INBOXES = {
    "claude": ".claude/memory-handoffs",
    "codex": ".codex/memory-handoffs",
    "opencode": ".opencode/memory-handoffs",
    "antigravity": ".antigravity/memory-handoffs",
    "pi": ".pi/memory-handoffs",
    "hermes": ".hermes/memory-handoffs",
}
KNOWN_INCLUDES = ("publisher",)

# Higher priority owners come first. The first harness in this list that
# also appears in the selection becomes the canonical memory owner unless
# the user passes --owner.
HARNESS_PRIORITY = ["openclaw", "hermes", "claude", "codex", "this-repo"]


@dataclass
class Selection:
    depth: str
    harnesses: List[str] = field(default_factory=list)
    owner: str = "this-repo"
    includes: List[str] = field(default_factory=list)

    def validate(self) -> None:
        if self.depth not in KNOWN_DEPTHS:
            raise ValueError(
                f"unknown depth: {self.depth!r} (valid: {KNOWN_DEPTHS})"
            )
        for h in self.harnesses:
            if h not in KNOWN_HARNESSES:
                raise ValueError(
                    f"unknown harness: {h!r} (valid: {KNOWN_HARNESSES})"
                )
        for inc in self.includes:
            if inc not in KNOWN_INCLUDES:
                raise ValueError(
                    f"unknown include: {inc!r} (valid: {KNOWN_INCLUDES})"
                )
        if self.owner != "this-repo" and self.owner not in self.harnesses:
            raise ValueError(
                f"owner {self.owner!r} not in selected harnesses {self.harnesses}"
            )


def resolve_owner(harnesses: List[str], override: Optional[str] = None) -> str:
    """Pick the canonical memory owner.

    If override is provided, it must be 'this-repo' or one of the selected
    harnesses. Otherwise the first entry in HARNESS_PRIORITY that also appears
    in `harnesses` wins; if none match, returns 'this-repo'.
    """
    if override is not None:
        if override == "this-repo":
            return override
        if override not in harnesses:
            raise ValueError(
                f"owner override {override!r} not in selected harnesses {harnesses}"
            )
        return override
    for candidate in HARNESS_PRIORITY:
        if candidate == "this-repo":
            continue
        if candidate in harnesses:
            return candidate
    return "this-repo"
