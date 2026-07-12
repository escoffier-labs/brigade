"""brigade route command: inspect the deterministic route for a task."""

from __future__ import annotations

import argparse


def register(sub: argparse._SubParsersAction) -> None:
    p_route = sub.add_parser(
        "route",
        help="Show the deterministic route a task composes: signals, stages, waves, holds.",
    )
    p_route.add_argument("task", help="Task description to route.")
    p_route.add_argument("--template", default=None, help="Task template hint (e.g. vertical-slice, docs).")
    p_route.add_argument(
        "--changed-path",
        action="append",
        default=[],
        dest="changed_paths",
        help="Changed file path to feed surface derivation. Repeatable.",
    )
    p_route.add_argument(
        "--approve-ship",
        action="store_true",
        help="Grant the ship approval signal so a requested ship stage is released instead of held.",
    )
    p_route.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_route.set_defaults(func=dispatch)


def dispatch(args) -> int:
    import json

    from ..route_catalog import route_brief

    approvals = ("ship-approved",) if args.approve_ship else ()
    brief = route_brief(args.task, template=args.template, changed_paths=args.changed_paths, approvals=approvals)
    if args.json:
        print(json.dumps(brief.payload(), indent=2))
        return 0
    print(f"signals: {', '.join(brief.signals)}")
    print(f"size: {brief.size} ({len(brief.route)} stages)")
    for index, wave in enumerate(brief.waves, start=1):
        for name in wave:
            reason = brief.triggered_by.get(name, "")
            print(f"  wave {index}: {name}  (#{reason})")
    for name, untils in brief.held.items():
        print(f"  held: {name}  (waiting on {', '.join('#' + u for u in untils)})")
    return 0
