"""Claude and Codex user-profile records for issue #438 slice 1."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

HARNESS_IDS = ("claude", "codex")
USER_SCOPE_SLICE1_TARGETS = (*HARNESS_IDS, "all")
SLICE1_HARNESS_IDS = HARNESS_IDS
PROFILE_STATE_VERSION = 2
INSTRUCTION_START = "<!-- brigade:user-profile:start -->"
INSTRUCTION_END = "<!-- brigade:user-profile:end -->"


@dataclass(frozen=True)
class HarnessProfile:
    harness: str
    user_root: Path
    instruction_path: Path
    skills_root: Path
    state_path: Path
    receipt_path: Path
    mcp_harness: str
    reload_hint: str


def resolve_slice1_profiles(*, harness: str, home: Path, workspace: Path) -> tuple[HarnessProfile, ...]:
    """Resolve the supported native user profiles without probing runtimes."""
    if harness not in USER_SCOPE_SLICE1_TARGETS:
        raise ValueError(f"unknown harness: {harness}")
    selected = SLICE1_HARNESS_IDS if harness == "all" else (harness,)
    specs = {
        "claude": (home / ".claude", "CLAUDE.md", "claude-user", "restart Claude Code"),
        "codex": (home / ".codex", "AGENTS.md", "codex-user", "restart Codex"),
    }
    return tuple(
        HarnessProfile(
            harness=profile_id,
            user_root=root,
            instruction_path=root / instruction,
            skills_root=root / "skills",
            state_path=root / "brigade" / "install-state.json",
            receipt_path=root / "brigade" / "profile-receipt.json",
            mcp_harness=mcp_harness,
            reload_hint=reload_hint,
        )
        for profile_id in selected
        for root, instruction, mcp_harness, reload_hint in (specs[profile_id],)
    )


def resolve_profiles(
    *, harness: str, home: Path, workspace: Path, **_unsupported: object
) -> tuple[HarnessProfile, ...]:
    """Compatibility entry point limited to the Claude/Codex slice."""
    return resolve_slice1_profiles(harness=harness, home=home, workspace=workspace)


def managed_instruction_text() -> str:
    """Return the common managed user-profile instruction body."""
    return (
        "## Brigade work loop\n\n"
        "Invoke the `using-skillet` skill and use each applicable reviewed skill before substantive work in a Brigade-wired repository.\n\n"
        "Run `brigade work brief --target .` when a `.brigade/` directory exists or `brigade status --target .` succeeds, and follow the brief before editing.\n\n"
        "Route worker-sized or parallelizable implementation through `brigade run` and keep the frontier session on planning, dispatch, review, and synthesis.\n\n"
        'Run counting checks through `brigade work verify run --target . --command "<command>" --capture brigade-work`, capturing failures as evidence so the outcome ledger stays honest.\n\n'
        "After substantial work, create a Memory Handoff through the standard Rocinante flow and never edit canonical memory directly.\n"
    )
