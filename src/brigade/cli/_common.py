"""Shared parser helpers for the brigade CLI package.

Group modules import from here (and from ``brigade.*`` domain modules), never
from the cli package ``__init__`` itself, to avoid circular imports.
"""

from __future__ import annotations

import argparse


# Top-level help groups. Every sub.add_parser command must appear in exactly
# one group; tests/test_cli_help.py enforces full coverage.
COMMAND_GROUPS: list[tuple[str, list[str]]] = [
    (
        "Core memory loop",
        ["init", "handoff", "handoff-template", "ingest", "memory", "doctor", "status", "profiles", "receipts"],
    ),
    (
        "Daily operator loop",
        ["operator", "daily", "work", "friction", "workflow", "center", "runbook", "budgets", "notifications"],
    ),
    (
        "Stations and tools",
        [
            "add",
            "stations",
            "skills",
            "tools",
            "mcp",
            "evidence",
            "search",
            "tokens",
            "pantry",
            "roster",
            "run",
            "runs",
            "model",
            "dogfood",
        ],
    ),
    (
        "Review, security, and research",
        ["security", "guard", "scrub", "untrusted", "research", "learn", "outcome", "chat", "context", "projects"],
    ),
    (
        "Wiring and advanced",
        [
            "release",
            "roadmap",
            "repos",
            "reconfigure",
            "completions",
            "extras",
            "openclaw-fragments",
            "hermes-fragments",
        ],
    ),
]

_START_HERE = """Brigade: run your agent brigade. Operator-system CLI for agent workspaces.

Start here:
  brigade operator quickstart --target <repo> --harnesses codex   set up a repo or workspace
  brigade operator doctor --target <repo>                         check the wiring
  brigade handoff draft --target <repo> ...                       record a durable note
  brigade ingest --target <workspace>                             route handoffs into memory"""


class _TopLevelHelpFormatter(argparse.RawDescriptionHelpFormatter):
    """Suppress argparse's flat subcommand dump on the top-level parser.

    The grouped epilog lists every command instead. Subparsers do not inherit
    this formatter, so `brigade <command> --help` keeps the normal listing.
    """

    def _iter_indented_subactions(self, action):
        if isinstance(action, argparse._SubParsersAction):
            return iter(())
        return super()._iter_indented_subactions(action)


def _grouped_epilog(sub: argparse._SubParsersAction, *, extras_enabled: bool = True) -> str:
    from ..extras import EXTRAS_COMMANDS

    helps = {action.dest: (action.help or "") for action in sub._choices_actions}
    lines = ["commands:"]
    gated: list[str] = []
    for title, names in COMMAND_GROUPS:
        shown = names if extras_enabled else [n for n in names if n not in EXTRAS_COMMANDS]
        gated.extend(n for n in names if not extras_enabled and n in EXTRAS_COMMANDS)
        if not shown:
            continue
        lines.append("")
        lines.append(f"{title}:")
        lines.extend(f"  {name:<22}{helps.get(name, '')}" for name in shown)
    if gated:
        lines.append("")
        lines.append("Extras (disabled; run 'brigade extras on' to enable):")
        lines.append("  " + ", ".join(sorted(gated)))
    lines.append("")
    lines.append("Run 'brigade <command> --help' for details on any command.")
    return "\n".join(lines)
