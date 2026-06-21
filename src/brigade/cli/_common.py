"""Shared parser helpers for the brigade CLI package.

Group modules import from here (and from ``brigade.*`` domain modules), never
from the cli package ``__init__`` itself, to avoid circular imports.
"""

from __future__ import annotations

import argparse


# Top-level help groups. Every sub.add_parser command must appear in exactly
# one group; tests/test_cli_help.py enforces full coverage.
COMMAND_GROUPS: list[tuple[str, list[str]]] = [
    ("Core memory loop", ["init", "handoff", "handoff-template", "ingest", "memory", "doctor", "status"]),
    ("Daily operator loop", ["operator", "daily", "work", "friction", "center", "runbook", "budgets", "notifications"]),
    ("Stations and tools", ["add", "skills", "tools", "mcp", "pantry", "roster", "run", "runs", "dogfood"]),
    (
        "Review, security, and research",
        ["security", "scrub", "untrusted", "research", "learn", "outcome", "chat", "context", "projects"],
    ),
    (
        "Wiring and advanced",
        ["release", "roadmap", "repos", "reconfigure", "completions", "openclaw-fragments", "hermes-fragments"],
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


def _grouped_epilog(sub: argparse._SubParsersAction) -> str:
    helps = {action.dest: (action.help or "") for action in sub._choices_actions}
    lines = ["commands:"]
    for title, names in COMMAND_GROUPS:
        lines.append("")
        lines.append(f"{title}:")
        lines.extend(f"  {name:<22}{helps.get(name, '')}" for name in names)
    lines.append("")
    lines.append("Run 'brigade <command> --help' for details on any command.")
    return "\n".join(lines)
