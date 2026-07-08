"""The extras wall: which CLI groups register beyond the core surface.

The core surface is the pitch: wire a repo, sync MCP/tools/skills, write and
ingest handoffs, run the verified-learning loop, dispatch cross-model runs.
The extras surface is the wider operator suite (release trains, fleet health,
mission control, research, chat archives). It stays fully functional but only
registers when the operator opts in, so `brigade --help` and the command
count match what a new user actually needs.

Enable with any of:
- `brigade extras on` (writes a user-level marker file)
- `BRIGADE_EXTRAS=1` in the environment (per-invocation or per-shell)
"""

from __future__ import annotations

import os
from pathlib import Path

# Top-level command groups gated behind the extras wall. Everything not
# listed here is core and always registers.
EXTRAS_COMMANDS: tuple[str, ...] = (
    "budgets",
    "center",
    "chat",
    "context",
    "dogfood",
    "friction",
    "hermes-fragments",
    "learn",
    "notifications",
    "openclaw-fragments",
    "pantry",
    "projects",
    "release",
    "repos",
    "research",
    "roadmap",
    "runbook",
    "untrusted",
    "workflow",
)


def marker_path() -> Path:
    config_home = os.environ.get("XDG_CONFIG_HOME")
    base = Path(config_home) if config_home else Path.home() / ".config"
    return base / "brigade" / "extras"


def enabled() -> bool:
    env = os.environ.get("BRIGADE_EXTRAS", "").strip().lower()
    if env in {"1", "true", "yes", "on"}:
        return True
    if env in {"0", "false", "no", "off"}:
        return False
    return marker_path().is_file()


def enable() -> Path:
    path = marker_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("enabled by `brigade extras on`\n")
    return path


def disable() -> None:
    try:
        marker_path().unlink()
    except FileNotFoundError:
        pass
