"""brigade command-line entrypoint."""

from __future__ import annotations

import argparse
import sys

from .. import __version__

# imported here so tests can monkeypatch cli.prompt_for_selection
from ..prompt import prompt_for_selection as prompt_for_selection

# Shared parser helpers live in cli._common. COMMAND_GROUPS is re-exported here
# so it stays on the public surface (tests reference cli.COMMAND_GROUPS).
from ._common import (
    COMMAND_GROUPS as COMMAND_GROUPS,
    _START_HERE,
    _TopLevelHelpFormatter,
    _grouped_epilog,
)

# Migrated command-group modules. Each exposes register(sub) and dispatch(args).
# They are registered in _build_parser in the same order the inline parser
# blocks were originally added.
from . import (
    init as _init_group,
    doctor as _doctor_group,
    status as _status_group,
    daily as _daily_group,
    add as _add_group,
    pantry as _pantry_group,
    notifications as _notifications_group,
    budgets as _budgets_group,
    untrusted as _untrusted_group,
    skills as _skills_group,
    operator as _operator_group,
    runbook as _runbook_group,
    dogfood as _dogfood_group,
    release as _release_group,
    roadmap as _roadmap_group,
    repos as _repos_group,
    handoff as _handoff_group,
    memory as _memory_group,
    work as _work_group,
    friction as _friction_group,
    chat as _chat_group,
    context as _context_group,
    projects as _projects_group,
    learn as _learn_group,
    research as _research_group,
    center as _center_group,
    run as _run_group,
    roster as _roster_group,
    runs as _runs_group,
    scrub as _scrub_group,
    security as _security_group,
    tools as _tools_group,
    handoff_template as _handoff_template_group,
    ingest as _ingest_group,
    openclaw_fragments as _openclaw_fragments_group,
    hermes_fragments as _hermes_fragments_group,
    reconfigure as _reconfigure_group,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="brigade",
        description=_START_HERE,
        formatter_class=_TopLevelHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"brigade {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="<command>")
    sub.required = True

    _init_group.register(sub)
    _doctor_group.register(sub)
    _status_group.register(sub)
    _daily_group.register(sub)
    _add_group.register(sub)
    _pantry_group.register(sub)
    _notifications_group.register(sub)
    _budgets_group.register(sub)
    _untrusted_group.register(sub)

    _skills_group.register(sub)
    _operator_group.register(sub)
    _runbook_group.register(sub)
    _dogfood_group.register(sub)
    _release_group.register(sub)
    _roadmap_group.register(sub)
    _repos_group.register(sub)
    _handoff_group.register(sub)

    _memory_group.register(sub)
    _work_group.register(sub)
    _friction_group.register(sub)

    _chat_group.register(sub)

    _context_group.register(sub)
    _projects_group.register(sub)
    _learn_group.register(sub)
    _research_group.register(sub)
    _center_group.register(sub)
    _run_group.register(sub)
    _roster_group.register(sub)

    _runs_group.register(sub)
    _scrub_group.register(sub)
    _security_group.register(sub)
    _tools_group.register(sub)
    _handoff_template_group.register(sub)
    _ingest_group.register(sub)
    _openclaw_fragments_group.register(sub)
    _hermes_fragments_group.register(sub)
    _reconfigure_group.register(sub)

    parser.epilog = _grouped_epilog(sub)
    return parser


def main(argv=None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    # Command groups dispatch via set_defaults(func=...). The parser is attached
    # so dispatch functions can call parser.error for unreachable
    # unknown-subcommand cases (required subparsers normally prevent these).
    args._brigade_parser = parser
    func = getattr(args, "func", None)
    if func is None:
        parser.error(f"unknown command: {args.command}")
        return 2
    return func(args)


def main_deprecated(argv=None) -> int:
    print(
        "warning: the 'solo-mise' command is deprecated; use 'brigade' instead.",
        file=sys.stderr,
    )
    return main(argv)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
