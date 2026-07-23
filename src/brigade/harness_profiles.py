"""User-profile records for issue #438 (Claude/Codex slice 1 + the remaining harnesses)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SLICE1_HARNESS_IDS = ("claude", "codex")
HARNESS_IDS = SLICE1_HARNESS_IDS
SLICE2_HARNESS_IDS = ("openclaw", "kimi", "grok", "cursor", "opencode")
USER_SCOPE_HARNESS_IDS = (*SLICE1_HARNESS_IDS, *SLICE2_HARNESS_IDS)
USER_SCOPE_SLICE1_TARGETS = (*SLICE1_HARNESS_IDS, "all")
USER_SCOPE_TARGETS = (*USER_SCOPE_HARNESS_IDS, "all")
PROFILE_STATE_VERSION = 2
INSTRUCTION_START = "<!-- brigade:user-profile:start -->"
INSTRUCTION_END = "<!-- brigade:user-profile:end -->"


@dataclass(frozen=True)
class GeneratedFile:
    """A whole-file Brigade-owned artifact under the profile's user root."""

    relative: str
    text: str
    executable: bool = False


@dataclass(frozen=True)
class HookSurface:
    """A co-owned JSON hook config that must carry one Brigade-managed entry."""

    path: Path
    entry: dict[str, str]


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
    instruction_text: str | None = None
    instruction_mode: str = "marked-block"  # or "managed-file" for whole-file ownership
    generated: tuple[GeneratedFile, ...] = field(default=())
    hook: HookSurface | None = None
    mcp_path: Path | None = None  # overrides the MCP adapter's static path when probed


def _profile(
    profile_id: str,
    root: Path,
    instruction: str,
    mcp_harness: str,
    reload_hint: str,
    **overrides: Any,
) -> HarnessProfile:
    return HarnessProfile(
        harness=profile_id,
        user_root=root,
        instruction_path=root / instruction,
        skills_root=root / "skills",
        state_path=root / "brigade" / "install-state.json",
        receipt_path=root / "brigade" / "profile-receipt.json",
        mcp_harness=mcp_harness,
        reload_hint=reload_hint,
        **overrides,
    )


def _kimi_root(home: Path) -> Path:
    """Capability probe: newer Kimi CLI installs expose ~/.kimi, older ones ~/.kimi-code.

    The newer surface wins when it exists; otherwise the documented legacy root is
    used (including for a first-time sync where neither directory exists yet).
    """
    newer = home / ".kimi"
    return newer if newer.exists() else home / ".kimi-code"


def _cursor_generated() -> tuple[GeneratedFile, ...]:
    """Whole-file Cursor artifacts; the rule file itself is the instruction surface."""
    from . import cursor_user_cmd

    plugin = "plugins/local/brigade-loop"
    return (
        GeneratedFile(f"{plugin}/.cursor-plugin/plugin.json", cursor_user_cmd._plugin_manifest()),
        GeneratedFile("hooks/brigade-session-start", cursor_user_cmd._hook_text(), executable=True),
    )


def resolve_slice1_profiles(*, harness: str, home: Path, workspace: Path) -> tuple[HarnessProfile, ...]:
    """Resolve the Claude/Codex native user profiles without probing runtimes."""
    if harness not in USER_SCOPE_SLICE1_TARGETS:
        raise ValueError(f"unknown harness: {harness}")
    selected = SLICE1_HARNESS_IDS if harness == "all" else (harness,)
    specs = {
        "claude": _profile("claude", home / ".claude", "CLAUDE.md", "claude-user", "restart Claude Code"),
        "codex": _profile("codex", home / ".codex", "AGENTS.md", "codex-user", "restart Codex"),
    }
    return tuple(specs[profile_id] for profile_id in selected)


def resolve_user_profiles(*, harness: str, home: Path, workspace: Path) -> tuple[HarnessProfile, ...]:
    """Resolve every supported user-scope profile; slice-2 harnesses may probe surfaces."""
    if harness not in USER_SCOPE_TARGETS:
        raise ValueError(f"unknown harness: {harness}")
    selected = USER_SCOPE_HARNESS_IDS if harness == "all" else (harness,)
    profiles: list[HarnessProfile] = []
    slice1 = {
        profile.harness: profile for profile in resolve_slice1_profiles(harness="all", home=home, workspace=workspace)
    }
    for profile_id in selected:
        if profile_id in slice1:
            profiles.append(slice1[profile_id])
        elif profile_id == "openclaw":
            # OpenClaw instructions target the canonical workspace AGENTS.md.
            profiles.append(
                _profile("openclaw", home / ".openclaw", "workspace/AGENTS.md", "openclaw", "restart OpenClaw")
            )
        elif profile_id == "kimi":
            root = _kimi_root(home)
            profiles.append(
                _profile("kimi", root, "AGENTS.md", "kimi-user", "restart Kimi Code", mcp_path=root / "mcp.json")
            )
        elif profile_id == "grok":
            profiles.append(_profile("grok", home / ".grok", "AGENTS.md", "grok-user", "restart Grok CLI"))
        elif profile_id == "cursor":
            root = home / ".cursor"
            from . import cursor_user_cmd

            profiles.append(
                _profile(
                    "cursor",
                    root,
                    "plugins/local/brigade-loop/rules/brigade-loop.mdc",
                    "cursor-user",
                    "reload Cursor windows",
                    instruction_text=cursor_user_cmd._rule_text(),
                    instruction_mode="managed-file",
                    generated=_cursor_generated(),
                    hook=HookSurface(
                        path=root / "hooks.json",
                        entry=cursor_user_cmd._hook_entry(root),
                    ),
                )
            )
        elif profile_id == "opencode":
            profiles.append(
                _profile(
                    "opencode",
                    home / ".config" / "opencode",
                    "AGENTS.md",
                    "opencode-user",
                    "restart OpenCode",
                )
            )
    return tuple(profiles)


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
