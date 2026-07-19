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
from .. import extras as _extras_mod
from . import (
    init as _init_group,
    doctor as _doctor_group,
    status as _status_group,
    profiles as _profiles_group,
    receipts as _receipts_group,
    daily as _daily_group,
    add as _add_group,
    setup as _setup_group,
    stations as _stations_group,
    pantry as _pantry_group,
    evidence as _evidence_group,
    search as _search_group,
    tokens as _tokens_group,
    notifications as _notifications_group,
    budgets as _budgets_group,
    untrusted as _untrusted_group,
    skills as _skills_group,
    harness as _harness_group,
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
    workflow as _workflow_group,
    chat as _chat_group,
    context as _context_group,
    projects as _projects_group,
    learn as _learn_group,
    outcome as _outcome_group,
    model as _model_group,
    research as _research_group,
    center as _center_group,
    run as _run_group,
    route as _route_group,
    roster as _roster_group,
    runs as _runs_group,
    guard as _guard_group,
    scrub as _scrub_group,
    security as _security_group,
    tools as _tools_group,
    mcp as _mcp_group,
    handoff_template as _handoff_template_group,
    ingest as _ingest_group,
    openclaw_fragments as _openclaw_fragments_group,
    hermes_fragments as _hermes_fragments_group,
    reconfigure as _reconfigure_group,
    completions as _completions_group,
    extras as _extras_group,
)

# Extras command name -> group module. Every name must appear in
# brigade.extras.EXTRAS_COMMANDS; the gate below keys off that list.
_EXTRAS_MODULES = {
    "budgets": _budgets_group,
    "center": _center_group,
    "chat": _chat_group,
    "context": _context_group,
    "dogfood": _dogfood_group,
    "friction": _friction_group,
    "workflow": _workflow_group,
    "hermes-fragments": _hermes_fragments_group,
    "learn": _learn_group,
    "notifications": _notifications_group,
    "openclaw-fragments": _openclaw_fragments_group,
    "pantry": _pantry_group,
    "projects": _projects_group,
    "release": _release_group,
    "repos": _repos_group,
    "research": _research_group,
    "roadmap": _roadmap_group,
    "runbook": _runbook_group,
    "untrusted": _untrusted_group,
}


def _register_extras(sub: argparse._SubParsersAction, name: str, extras_enabled: bool) -> None:
    if extras_enabled:
        _EXTRAS_MODULES[name].register(sub)
    else:
        _extras_group.register_stub(sub, name)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="brigade",
        description=_START_HERE,
        formatter_class=_TopLevelHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"brigade {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="<command>")
    sub.required = True

    extras_enabled = _extras_mod.enabled()

    _init_group.register(sub)
    _doctor_group.register(sub)
    _status_group.register(sub)
    _profiles_group.register(sub)
    _receipts_group.register(sub)
    _daily_group.register(sub)
    _add_group.register(sub)
    _setup_group.register(sub)
    _stations_group.register(sub)
    _evidence_group.register(sub)
    _search_group.register(sub)
    _tokens_group.register(sub)
    _register_extras(sub, "pantry", extras_enabled)
    _register_extras(sub, "notifications", extras_enabled)
    _register_extras(sub, "budgets", extras_enabled)
    _register_extras(sub, "untrusted", extras_enabled)

    _skills_group.register(sub)
    _harness_group.register(sub)
    _operator_group.register(sub)
    _register_extras(sub, "runbook", extras_enabled)
    _register_extras(sub, "dogfood", extras_enabled)
    _register_extras(sub, "release", extras_enabled)
    _register_extras(sub, "roadmap", extras_enabled)
    _register_extras(sub, "repos", extras_enabled)
    _handoff_group.register(sub)

    _memory_group.register(sub)
    _work_group.register(sub)
    _register_extras(sub, "friction", extras_enabled)
    _register_extras(sub, "workflow", extras_enabled)

    _register_extras(sub, "chat", extras_enabled)

    _register_extras(sub, "context", extras_enabled)
    _register_extras(sub, "projects", extras_enabled)
    _register_extras(sub, "learn", extras_enabled)
    _outcome_group.register(sub)
    _model_group.register(sub)
    _register_extras(sub, "research", extras_enabled)
    _register_extras(sub, "center", extras_enabled)
    _run_group.register(sub)
    _route_group.register(sub)
    _roster_group.register(sub)

    _runs_group.register(sub)
    _guard_group.register(sub)
    _scrub_group.register(sub)
    _security_group.register(sub)
    _tools_group.register(sub)
    _mcp_group.register(sub)
    _handoff_template_group.register(sub)
    _ingest_group.register(sub)
    _register_extras(sub, "openclaw-fragments", extras_enabled)
    _register_extras(sub, "hermes-fragments", extras_enabled)
    _reconfigure_group.register(sub)
    _completions_group.register(sub)
    _extras_group.register(sub)

    parser.epilog = _grouped_epilog(sub, extras_enabled=extras_enabled)
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
