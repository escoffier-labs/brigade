"""brigade model command group."""

from __future__ import annotations

import argparse
from pathlib import Path


def register(sub: argparse._SubParsersAction) -> None:
    p_model = sub.add_parser("model", help="Inspect model performance across Brigade runs.")
    model_sub = p_model.add_subparsers(dest="model_command", metavar="<model-command>")
    model_sub.required = True

    p_scorecard = model_sub.add_parser(
        "scorecard",
        help="Aggregate run artifacts into a per-(cli, model) scorecard (read-only).",
    )
    p_scorecard.add_argument(
        "--target",
        "-t",
        type=Path,
        default=Path("."),
        help="Workspace whose .brigade/runs directory should be scanned (default: .).",
    )
    p_scorecard.add_argument(
        "--runs-dir",
        type=Path,
        action="append",
        default=None,
        dest="runs_dirs",
        help="Explicit runs directory (repeatable). When set, replaces the default under --target.",
    )
    p_scorecard.add_argument(
        "--since",
        default=None,
        help="Only include runs with started_at on or after this UTC date (YYYY-MM-DD).",
    )
    p_scorecard.add_argument("--json", action="store_true", help="Emit machine-readable JSON instead of text.")
    p_scorecard.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="List skipped run directories and skip reasons.",
    )
    p_scorecard.set_defaults(func=_dispatch_scorecard)

    p_trial = model_sub.add_parser("trial", help="Plan and run resumable model evaluation cells.")
    trial_sub = p_trial.add_subparsers(dest="trial_command", metavar="<trial-command>")
    trial_sub.required = True
    for command in ("plan", "run", "resume"):
        parser = trial_sub.add_parser(command, help=f"{command.title()} a model trial manifest.")
        parser.add_argument("manifest", type=Path, help="brigade.eval_manifest.v1 JSON file.")
        parser.add_argument("--target", "-t", type=Path, default=Path("."), help="Workspace used by trial runs.")
        parser.add_argument(
            "--roster", type=Path, default=None, help="Roster path. Uses normal workspace/user fallback."
        )
        parser.add_argument("--output-dir", type=Path, default=None, help="Trial artifact directory.")
        parser.set_defaults(func=_dispatch_trial)
    for command in ("show", "summary"):
        parser = trial_sub.add_parser(command, help=f"{command.title()} trial artifacts.")
        parser.add_argument("output_dir", type=Path, help="Trial artifact directory.")
        parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
        parser.set_defaults(func=_dispatch_trial)
    p_regrade = trial_sub.add_parser(
        "regrade", help="Re-run graders from stored trial output without re-running seats."
    )
    p_regrade.add_argument("output_dir", type=Path, help="Trial artifact directory.")
    p_regrade.set_defaults(func=_dispatch_trial)


def _dispatch_scorecard(args) -> int:
    from .. import model_scorecard

    return model_scorecard.scorecard(
        target=args.target,
        runs_dirs=args.runs_dirs,
        since=args.since,
        json_output=args.json,
        verbose=args.verbose,
    )


def _dispatch_trial(args) -> int:
    import json
    import sys

    from .. import model_trials
    from .. import roster as roster_mod

    if args.trial_command in {"show", "summary"}:
        if args.trial_command == "show":
            return model_trials.show(args.output_dir, json_output=args.json)
        payload = model_trials.summarize(args.output_dir)
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            for state, count in payload["counts"].items():
                print(f"{state}: {count}")
        return 0
    if args.trial_command == "regrade":
        return model_trials.regrade(args.output_dir)

    target = args.target.expanduser().resolve()
    try:
        roster_path = roster_mod.resolve_roster_path(target, args.roster)
        roster = roster_mod.load_roster(roster_path)
        manifest = model_trials.load_manifest(args.manifest)
        output_dir = args.output_dir or target / ".brigade" / "evals" / manifest["name"]
        plan, cells = model_trials.build_plan(args.manifest, roster, output_dir)
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.trial_command == "plan":
        print(json.dumps(plan, indent=2, sort_keys=True))
        return 0
    return model_trials.execute(
        args.manifest,
        roster,
        workspace=target,
        output_dir=output_dir,
        resume=args.trial_command == "resume",
    )
