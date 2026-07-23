"""Selection data model: depth + harnesses + owner + includes."""

from __future__ import annotations

import shutil
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional


KNOWN_DEPTHS = ("repo", "workspace")
KNOWN_HARNESSES = (
    "claude",
    "codex",
    "opencode",
    "antigravity",
    "pi",
    "cursor",
    "aider",
    "goose",
    "continue",
    "copilot",
    "qwen",
    "kimi",
    "adal",
    "openhands",
    "grok",
    "amp",
    "crush",
    "openclaw",
    "hermes",
)
# Writer harness id -> repo-relative handoff inbox dir. Single source of truth;
# install, ingest, doctor, the fleet sweep, and the handoff doctor consume this.
WRITER_INBOXES = {
    "claude": ".claude/memory-handoffs",
    "codex": ".codex/memory-handoffs",
    "opencode": ".opencode/memory-handoffs",
    "antigravity": ".antigravity/memory-handoffs",
    "pi": ".pi/memory-handoffs",
    "cursor": ".cursor/memory-handoffs",
    "aider": ".aider/memory-handoffs",
    "goose": ".goose/memory-handoffs",
    "continue": ".continue/memory-handoffs",
    "copilot": ".copilot/memory-handoffs",
    "qwen": ".qwen/memory-handoffs",
    "kimi": ".kimi/memory-handoffs",
    "adal": ".adal/memory-handoffs",
    "openhands": ".openhands/memory-handoffs",
    "grok": ".grok/memory-handoffs",
    "amp": ".amp/memory-handoffs",
    "crush": ".crush/memory-handoffs",
    "hermes": ".hermes/memory-handoffs",
}
KNOWN_INCLUDES = ("publisher", "repo-extras")

# Higher priority owners come first. The first harness in this list that
# also appears in the selection becomes the canonical memory owner unless
# the user passes --owner.
HARNESS_PRIORITY = ["openclaw", "hermes", "claude", "codex", "this-repo"]
SURFACE_PROJECTIONS = {
    "cursor-cli": "cursor",
    "cursor-gui": "cursor",
}
SURFACE_BINARIES = {
    "cursor-cli": ("cursor-agent",),
}
EXTERNAL_ONLY_SURFACES = {"cursor-gui"}


class SurfaceInstallRefusal(ValueError):
    """A selected vendor surface cannot be installed with the requested mode."""

    def __init__(self, surface_id: str, detail: str) -> None:
        self.surface_id = surface_id
        self.detail = detail
        super().__init__(f"surface {surface_id!r}: {detail}")


@dataclass
class SurfaceRecord:
    """Probe evidence for one vendor surface projected through a base harness."""

    surface_id: str
    projection_harness: str
    capabilities: list[dict[str, Any]] = field(default_factory=list)
    availability: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.capabilities = deepcopy(self.capabilities)
        self.availability = deepcopy(self.availability)

    @classmethod
    def from_fixture(
        cls,
        fixture: dict[str, Any],
        *,
        projection_harness: str,
        availability: dict[str, Any],
    ) -> "SurfaceRecord":
        harness = fixture.get("harness", {})
        surface_id = harness.get("id")
        if not isinstance(surface_id, str) or not surface_id:
            raise ValueError("surface fixture must contain a non-empty harness.id")
        capabilities = fixture.get("capabilities", [])
        if not isinstance(capabilities, list) or not isinstance(availability, dict):
            raise ValueError("surface fixture capabilities and resolved availability must be collections")
        return cls(
            surface_id=surface_id,
            projection_harness=projection_harness,
            capabilities=capabilities,
            availability=availability,
        )

    @classmethod
    def resolve_known(
        cls,
        surface_id: str,
        *,
        which: Callable[[str], str | None] | None = None,
    ) -> "SurfaceRecord":
        """Resolve the availability state for a built-in surface without executing it."""
        projection_harness = SURFACE_PROJECTIONS.get(surface_id)
        if projection_harness is None:
            raise ValueError(f"unknown harness surface: {surface_id!r} (valid: {tuple(SURFACE_PROJECTIONS)})")
        availability: dict[str, Any]
        if surface_id in EXTERNAL_ONLY_SURFACES:
            availability = {
                "state": "external_only",
                "reason": "desktop_or_gui_surface",
            }
        else:
            resolver = which or shutil.which
            commands = list(SURFACE_BINARIES[surface_id])
            command_available = {command: resolver(command) is not None for command in commands}
            available_commands = [command for command, available in command_available.items() if available]
            availability = {
                "state": "available" if available_commands else "externally_blocked",
                "commands": commands,
                "command_available": command_available,
            }
            if available_commands:
                availability["available_commands"] = available_commands
            else:
                availability["reason"] = "binary_not_found"
        return cls(
            surface_id=surface_id,
            projection_harness=projection_harness,
            availability=availability,
        )

    def persisted_evidence(self, *, projection_only: bool) -> dict[str, Any]:
        state = self.availability.get("state")
        if state == "available":
            install_mode = "native"
        elif projection_only:
            install_mode = "projection_only"
        else:
            install_mode = "unverified"
        return {
            "projection_harness": self.projection_harness,
            "availability": deepcopy(self.availability),
            "capabilities": deepcopy(self.capabilities),
            "runtime_present": state == "available",
            "install_mode": install_mode,
        }


@dataclass
class Selection:
    depth: str
    harnesses: List[str] = field(default_factory=list)
    owner: str = "this-repo"
    includes: List[str] = field(default_factory=list)
    surfaces: List[SurfaceRecord] = field(default_factory=list)

    def validate(self) -> None:
        if self.depth not in KNOWN_DEPTHS:
            raise ValueError(f"unknown depth: {self.depth!r} (valid: {KNOWN_DEPTHS})")
        for h in self.harnesses:
            if h not in KNOWN_HARNESSES:
                raise ValueError(f"unknown harness: {h!r} (valid: {KNOWN_HARNESSES})")
        for inc in self.includes:
            if inc not in KNOWN_INCLUDES:
                raise ValueError(f"unknown include: {inc!r} (valid: {KNOWN_INCLUDES})")
        if self.owner != "this-repo" and self.owner not in self.harnesses:
            raise ValueError(f"owner {self.owner!r} not in selected harnesses {self.harnesses}")
        surface_ids: set[str] = set()
        for surface in self.surfaces:
            if surface.surface_id in surface_ids:
                raise ValueError(f"duplicate selected surface: {surface.surface_id!r}")
            surface_ids.add(surface.surface_id)
            expected_projection = SURFACE_PROJECTIONS.get(surface.surface_id)
            if expected_projection is not None and surface.projection_harness != expected_projection:
                raise ValueError(
                    f"surface {surface.surface_id!r} must project through harness {expected_projection!r}, "
                    f"not {surface.projection_harness!r}"
                )
            if surface.projection_harness not in self.harnesses:
                raise ValueError(
                    f"surface {surface.surface_id!r} projection harness {surface.projection_harness!r} is not selected"
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
            raise ValueError(f"owner override {override!r} not in selected harnesses {harnesses}")
        return override
    for candidate in HARNESS_PRIORITY:
        if candidate == "this-repo":
            continue
        if candidate in harnesses:
            return candidate
    return "this-repo"
