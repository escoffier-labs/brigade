"""Ordered brief sections shared by ``brigade work brief`` and static wiring.

Issue #733 makes the dynamically generated work brief the single source of truth
for workflow guidance. Static harness instruction blocks are rendered from the
same ordered sections:

- ``minimal``: short pointer (identity, key commands, hard rules)
- ``full``: pointer plus session-close and work-loop protocol

Live ``brigade work brief`` text opens with the session-close checklist, then
the dynamic status dump. Keeping both surfaces on one renderer prevents the
static full profile from drifting away from the brief.
"""

from __future__ import annotations

from typing import Iterable, Sequence

PROFILE_MINIMAL = "minimal"
PROFILE_FULL = "full"
KNOWN_PROFILES = (PROFILE_MINIMAL, PROFILE_FULL)
DEFAULT_PROFILE = PROFILE_FULL

# Richness order: higher wins when multiple integrations share one file.
PROFILE_RANK = {
    PROFILE_MINIMAL: 1,
    PROFILE_FULL: 2,
}

SECTION_SESSION_CLOSE = "session_close"
SECTION_IDENTITY = "identity"
SECTION_KEY_COMMANDS = "key_commands"
SECTION_HARD_RULES = "hard_rules"
SECTION_WORK_LOOP = "work_loop"

MINIMAL_SECTIONS: tuple[str, ...] = (
    SECTION_IDENTITY,
    SECTION_KEY_COMMANDS,
    SECTION_HARD_RULES,
)
FULL_SECTIONS: tuple[str, ...] = (
    SECTION_SESSION_CLOSE,
    SECTION_IDENTITY,
    SECTION_KEY_COMMANDS,
    SECTION_HARD_RULES,
    SECTION_WORK_LOOP,
)

_SESSION_CLOSE_ITEMS: tuple[str, ...] = (
    'Verify captured through Brigade? (`brigade work verify run --target . --command "..." --capture brigade-work`)',
    "Outcome recorded? (`brigade outcome capture <skill-or-card-id> --run-id latest`)",
    "Memory Handoff written when durable knowledge was produced?",
)


def normalize_profile(value: str | None, *, default: str = DEFAULT_PROFILE) -> str:
    """Return a known instruction profile name, or ``default`` when unset/unknown."""
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in PROFILE_RANK:
        return normalized
    raise ValueError(f"unknown instruction profile: {value!r}")


def richer_profile(left: str, right: str) -> str:
    """Return the richer of two profiles."""
    left_n = normalize_profile(left)
    right_n = normalize_profile(right)
    return left_n if PROFILE_RANK[left_n] >= PROFILE_RANK[right_n] else right_n


def effective_profile(requirements: Iterable[str]) -> str | None:
    """Compute the richest active requirement, or None when nothing is required."""
    richest: str | None = None
    for raw in requirements:
        candidate = normalize_profile(raw)
        richest = candidate if richest is None else richer_profile(richest, candidate)
    return richest


def profiles_for_depth(profile: str) -> tuple[str, ...]:
    """Return the ordered static section ids for an instruction profile."""
    normalized = normalize_profile(profile)
    if normalized == PROFILE_MINIMAL:
        return MINIMAL_SECTIONS
    return FULL_SECTIONS


def session_close_items() -> tuple[str, ...]:
    """Checklist items that lead the live brief and the full static profile."""
    return _SESSION_CLOSE_ITEMS


def render_session_close_section(*, heading: str = "## Session close") -> str:
    lines = [heading, ""]
    lines.extend(f"- [ ] {item}" for item in _SESSION_CLOSE_ITEMS)
    lines.append("")
    return "\n".join(lines)


def render_identity_section() -> str:
    return (
        "## Brigade\n\n"
        "Brigade is the local work loop for this workspace: briefs, verification "
        "receipts, outcomes, and Memory Handoffs.\n"
    )


def render_key_commands_section() -> str:
    return (
        "### Start here\n\n"
        "1. Run `brigade work brief --target .` and follow it before editing.\n"
        "2. Run counting checks through Brigade, not raw.\n"
        "3. After substantial work, write a Memory Handoff; never edit canonical memory directly.\n"
    )


def render_hard_rules_section() -> str:
    return (
        "### Hard rules\n\n"
        "- Verify through Brigade when the result should count.\n"
        "- Capture outcomes (failures included) so the ledger stays honest.\n"
        "- Handoff at the end of substantial work.\n"
    )


def render_work_loop_section() -> str:
    return (
        "### Work loop protocol\n\n"
        "Invoke the `using-skillet` skill and use each applicable reviewed skill "
        "before substantive work in a Brigade-wired repository.\n\n"
        "Run `brigade work brief --target .` when a `.brigade/` directory exists "
        "or `brigade status --target .` succeeds, and follow the brief before editing.\n\n"
        "Route worker-sized or parallelizable implementation through `brigade run` "
        "and keep the frontier session on planning, dispatch, review, and synthesis.\n\n"
        "Run counting checks through "
        '`brigade work verify run --target . --command "<command>" --capture brigade-work`, '
        "capturing failures as evidence so the outcome ledger stays honest.\n\n"
        "After substantial work, create a Memory Handoff through the standard "
        "Rocinante flow and never edit canonical memory directly.\n"
    )


_SECTION_RENDERERS = {
    SECTION_SESSION_CLOSE: render_session_close_section,
    SECTION_IDENTITY: render_identity_section,
    SECTION_KEY_COMMANDS: render_key_commands_section,
    SECTION_HARD_RULES: render_hard_rules_section,
    SECTION_WORK_LOOP: render_work_loop_section,
}


def render_section(section_id: str) -> str:
    """Render one named brief section."""
    renderer = _SECTION_RENDERERS.get(section_id)
    if renderer is None:
        raise ValueError(f"unknown brief section: {section_id!r}")
    return renderer()


def render_sections(section_ids: Sequence[str]) -> str:
    """Render ordered sections joined with a blank line between non-empty parts."""
    parts: list[str] = []
    for section_id in section_ids:
        text = render_section(section_id).rstrip()
        if text:
            parts.append(text)
    if not parts:
        return ""
    return "\n\n".join(parts) + "\n"


def render_static_instruction(profile: str = DEFAULT_PROFILE) -> str:
    """Render the static managed-instruction body for ``minimal`` or ``full``."""
    return render_sections(profiles_for_depth(profile))


def render_brief_session_close_lines() -> list[str]:
    """Lines printed at the top of live ``brigade work brief`` text output."""
    lines = ["session_close:"]
    lines.extend(f"  - [ ] {item}" for item in _SESSION_CLOSE_ITEMS)
    return lines
