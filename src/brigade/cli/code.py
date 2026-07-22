"""Executable GraphTrail facade commands."""

from __future__ import annotations

import argparse

# Every engine verb reachable through `brigade code`, in engine help order.
# `init` stays unexposed (`sync` builds the db) and `watch` needs a streaming
# relay the facade does not have yet.
ENGINE_VERBS: dict[str, str] = {
    "sync": "Index or refresh the repo's code graph.",
    "search": "Search indexed symbols by name.",
    "neighbors": "List a file's incoming and outgoing graph neighbors.",
    "callers": "List callers of a symbol.",
    "callees": "List callees of a symbol.",
    "impact": "Show the blast radius of changing a symbol.",
    "context": "Rank entry points and related files for a task.",
    "dead-code": "List callables with no incoming call edges.",
    "cycles": "List file-level dependency cycles.",
    "affected": "List tests statically attributed to changed files.",
    "evaluate": "Dry-run extraction: print what a sync would store, writing nothing.",
    "explain": "Explain how call edges between two symbols resolved.",
    "export": "Export the graph as Graphviz dot, GraphML, or JSON Lines.",
    "stats": "Show code graph statistics.",
    "doctor": "Check code graph engine health.",
    "diff": "Diff two indexed graph databases.",
}


def register(sub: argparse._SubParsersAction) -> None:
    parser = sub.add_parser("code", help="Run code graph commands through Brigade.")
    commands = parser.add_subparsers(dest="code_command", metavar="<code-command>")
    commands.required = True
    for verb, summary in ENGINE_VERBS.items():
        command = commands.add_parser(
            verb,
            help=summary,
            description=(
                f"{summary} Accepts Brigade's `--target <dir>` to run against that"
                " repo; every other argument is forwarded to the engine unchanged."
            ),
        )
        command.add_argument("engine_args", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)
    parser.set_defaults(func=dispatch)


def dispatch(args) -> int:
    from .. import code_cmd

    return code_cmd.run(args.code_command, args.engine_args)
