"""User-scope harness profile records and native path resolution.

Issue #438: one user-profile layer across eight harnesses. This module holds
immutable profile records and native path resolution, including a single
Kimi capability probe resolved once per command and threaded through as a
value. Managed-block parsing, ownership-state validation, skill/artifact
reconciliation, and aggregate install/uninstall/doctor live in the sibling
``harness_profile_cmd`` module.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

HARNESS_IDS = (
    "claude",
    "codex",
    "openclaw",
    "kimi",
    "grok",
    "cursor",
    "opencode",
    "pi",
)
PROFILE_STATE_VERSION = 2
INSTRUCTION_START = "<!-- brigade:user-profile:start -->"
INSTRUCTION_END = "<!-- brigade:user-profile:end -->"


@dataclass(frozen=True)
class HarnessProfile:
    harness: str
    user_root: Path
    instruction_path: Path | None
    skills_root: Path
    state_path: Path
    mcp_harness: str | None
    reload_hint: str
    capabilities: dict[str, bool]


def probe_kimi_native_mcp() -> bool:
    """Probe whether a native Kimi CLI exposes an ``mcp`` subcommand.

    ``shutil.which`` is consulted first; an absent executable means ``False``
    without executing anything. Otherwise the probe runs
    ``<absolute_exe> mcp --help`` with all stdio streams detached and returns
    ``True`` only on a zero exit code. ``OSError`` and ``TimeoutExpired`` are
    treated as "not native".
    """
    executable = shutil.which("kimi")
    if executable is None:
        return False
    try:
        completed = subprocess.run(
            [executable, "mcp", "--help"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def resolve_profiles(
    *,
    harness: str,
    home: Path,
    workspace: Path,
    kimi_native_mcp: bool | None = None,
) -> tuple[HarnessProfile, ...]:
    """Resolve the immutable profile records for the selected harnesses.

    Rejects unknown harness IDs with ``ValueError``. Probes Kimi's native MCP
    capability at most once, and only when Kimi is in the selection and the
    caller did not supply an explicit value.
    """
    if harness not in (*HARNESS_IDS, "all"):
        raise ValueError(f"unknown harness: {harness}")
    selected = HARNESS_IDS if harness == "all" else (harness,)
    if "kimi" in selected and kimi_native_mcp is None:
        kimi_native_mcp = probe_kimi_native_mcp()
    kimi_native_mcp = bool(kimi_native_mcp)

    roots: dict[str, Path] = {
        "claude": home / ".claude",
        "codex": home / ".codex",
        "openclaw": home / ".openclaw",
        "kimi": home / (".kimi" if kimi_native_mcp else ".kimi-code"),
        "grok": home / ".grok",
        "cursor": home / ".cursor",
        "opencode": home / ".config" / "opencode",
        "pi": home / ".pi" / "agent",
    }
    instructions: dict[str, str] = {
        "claude": "CLAUDE.md",
        "codex": "AGENTS.md",
        "kimi": "AGENTS.md",
        "grok": "AGENTS.md",
        "opencode": "AGENTS.md",
        "pi": "AGENTS.md",
    }
    mcp: dict[str, str | None] = {
        "claude": "claude-user",
        "codex": "codex-user",
        "openclaw": "openclaw",
        "kimi": "kimi-user",
        "grok": "grok-user",
        "cursor": "cursor-user",
        "opencode": "opencode-user",
        "pi": None,
    }
    hints: dict[str, str] = {
        "claude": "restart Claude Code",
        "codex": "restart Codex",
        "openclaw": "restart OpenClaw",
        "kimi": "restart Kimi Code",
        "grok": "restart Grok CLI",
        "cursor": "reload Cursor windows",
        "opencode": "restart OpenCode",
        "pi": "restart Pi",
    }

    result: list[HarnessProfile] = []
    for profile_id in selected:
        root = roots[profile_id]
        if profile_id == "cursor":
            instruction: Path | None = None
        elif profile_id == "openclaw":
            instruction = workspace / "AGENTS.md"
        else:
            instruction = root / instructions[profile_id]
        capabilities = {"kimi_native_mcp": kimi_native_mcp} if profile_id == "kimi" else {}
        result.append(
            HarnessProfile(
                harness=profile_id,
                user_root=root,
                instruction_path=instruction,
                skills_root=root / "skills",
                state_path=root / "brigade" / "install-state.json",
                mcp_harness=mcp[profile_id],
                reload_hint=hints[profile_id],
                capabilities=capabilities,
            )
        )
    return tuple(result)
