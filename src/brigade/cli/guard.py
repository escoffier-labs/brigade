"""brigade guard command group."""

from __future__ import annotations

import argparse
import importlib

# Subcommands with their own entry modules. Everything else falls through to
# the embedded guard CLI (scan, redact, diff, audit, baseline, allow).
_ROUTED: dict[str, tuple[str, str]] = {
    "git": ("git_scan", "Scan tracked files or push history in a git repo."),
    "commits": ("git_commits", "Scan the content introduced by a commit range."),
    "publish-check": ("publish_check", "Pre-publication scan for a repo about to go public."),
    "pr": ("pr_draft", "Scan a PR draft body before filing."),
    "pr-prepare": ("pr_prepare", "Prepare and scan a PR draft."),
    "n8n-advisory": ("n8n_advisory", "Advisory scan for n8n workflow content."),
    "n8n-validate": ("n8n_validate", "Run the guard against the bundled n8n fixtures."),
}

_EMBEDDED: dict[str, str] = {
    "scan": "Scan a file or stdin against a policy.",
    "redact": "Redact findings from a file or stdin.",
    "diff": "Show what redaction would change.",
    "audit": "Aggregate scan results across a directory.",
    "baseline": "Manage the accepted-findings baseline.",
    "allow": "Manage policy allow_values.",
}


def _help_text() -> str:
    lines = [
        "usage: brigade guard <command> [args...]",
        "",
        "Policy-driven content scanning and redaction (embedded content guard).",
        "",
        "commands:",
    ]
    width = max(len(name) for name in (*_EMBEDDED, *_ROUTED)) + 2
    for name, desc in _EMBEDDED.items():
        lines.append(f"  {name.ljust(width)}{desc}")
    for name, (_mod, desc) in _ROUTED.items():
        lines.append(f"  {name.ljust(width)}{desc}")
    lines.append("")
    lines.append("Run brigade guard <command> --help for command-specific options.")
    return "\n".join(lines)


def register(sub: argparse._SubParsersAction) -> None:
    p_guard = sub.add_parser("guard", help="Run the embedded content guard.", add_help=False)
    p_guard.add_argument("-h", "--help", action="store_true", dest="guard_help")
    p_guard.add_argument("guard_args", nargs=argparse.REMAINDER)
    p_guard.set_defaults(func=dispatch)


def dispatch(args) -> int:
    rest = list(args.guard_args)
    if not rest:
        print(_help_text())
        return 0
    head = rest[0]
    if head in ("-h", "--help"):
        print(_help_text())
        return 0
    if head in _ROUTED:
        mod = importlib.import_module(f"brigade.guard.{_ROUTED[head][0]}")
        sub_args = rest[1:]
        if args.guard_help:
            sub_args = ["--help", *sub_args]
        return int(mod.main(sub_args))
    from ..guard.cli import main as embedded_main

    if args.guard_help:
        rest = ["--help", *rest]
    return int(embedded_main(rest))
