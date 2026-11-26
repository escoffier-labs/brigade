"""Local authorization shared by connector discovery and tool calls."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

from .contracts import ALL_TOOL_SCHEMAS, ToolClass

if TYPE_CHECKING:
    from .profile import ConnectorProfile

TOOL_NOT_PERMITTED = "tool_not_permitted"


@dataclass(frozen=True)
class ToolGrants:
    classes: frozenset[ToolClass]
    deny: frozenset[str]


@dataclass(frozen=True)
class Allowed:
    pass


@dataclass(frozen=True)
class Denied:
    reason: str
    code: str = TOOL_NOT_PERMITTED


# Future class hooks must be pure decisions. No confirmation hooks are registered.
_CLASS_HOOKS: dict[ToolClass, Callable[[ConnectorProfile, str], Allowed | Denied]] = {}


def authorize(profile: ConnectorProfile, grants: ToolGrants | None, tool_name: object) -> Allowed | Denied:
    """Apply the runtime grants and the profile's tool inventory."""
    if not isinstance(tool_name, str) or tool_name not in ALL_TOOL_SCHEMAS:
        return Denied("unknown tool", "invalid_request")
    tool_class = ALL_TOOL_SCHEMAS[tool_name].tool_class
    # Check explicit grants before the inventory, which has already been narrowed at call time.
    if grants is not None:
        if tool_name in grants.deny:
            return Denied("tool is on the deny list")
        if tool_class not in grants.classes:
            return Denied(f"{tool_class.value} class is not granted")
    if tool_name not in profile.tools:
        return Denied("tool is outside the profile", "invalid_request")
    hook = _CLASS_HOOKS.get(tool_class)
    return Allowed() if hook is None else hook(profile, tool_name)
