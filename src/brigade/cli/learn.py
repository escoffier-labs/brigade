"""brigade learn command group."""

from __future__ import annotations

import argparse
from pathlib import Path


def register(sub: argparse._SubParsersAction) -> None:
    from .. import learn_cmd

    # learn
    p_learn = sub.add_parser("learn", help="Plan local self-learning candidates without mutating memory or source.")
    learn_sub = p_learn.add_subparsers(dest="learn_command", metavar="<learn-command>")
    learn_sub.required = True
    p_learn_plan = learn_sub.add_parser("plan", help="List local learning candidates.")
    p_learn_plan.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_learn_plan.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_learn_doctor = learn_sub.add_parser("doctor", help="Check local self-learning queue health.")
    p_learn_doctor.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_learn_doctor.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_learn_import = learn_sub.add_parser("import-issues", help="Import learning candidates into the work inbox.")
    p_learn_import.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_learn_import.add_argument("--dry-run", action="store_true", help="Report without writing imports.")
    p_learn_import.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_learn_import_learnings = learn_sub.add_parser(
        "import-learnings", help="Import structured .learnings/ markdown log entries into the work inbox."
    )
    p_learn_import_learnings.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_learn_import_learnings.add_argument(
        "--file", action="append", default=[], help="Override the .learnings file to read. May be repeated."
    )
    p_learn_import_learnings.add_argument("--dry-run", action="store_true", help="Report without writing imports.")
    p_learn_import_learnings.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_learn_skill_candidates = learn_sub.add_parser(
        "skill-candidates", help="Find repeatable learning patterns that could become reviewed skills."
    )
    p_learn_skill_candidates.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_learn_skill_candidates.add_argument(
        "--min-count", type=int, default=2, help="Minimum repeated evidence count required."
    )
    p_learn_skill_candidates.add_argument(
        "--source", default=None, help="Only include candidates from one learning source, such as security-scan."
    )
    p_learn_skill_candidates.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_learn_propose_skill = learn_sub.add_parser(
        "propose-skill", help="Write a reviewed skill proposal from a learning skill candidate."
    )
    p_learn_propose_skill.add_argument("candidate_id", help="Learning skill candidate id or unique prefix.")
    p_learn_propose_skill.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_learn_propose_skill.add_argument(
        "--min-count", type=int, default=2, help="Minimum repeated evidence count required."
    )
    p_learn_propose_skill.add_argument(
        "--source", default=None, help="Resolve the candidate within one learning source, such as security-scan."
    )
    p_learn_propose_skill.add_argument(
        "--dry-run", action="store_true", help="Preview generated skill source and inbox proposal without writing."
    )
    p_learn_propose_skill.add_argument(
        "--force", action="store_true", help="Refresh an existing generated skill source."
    )
    p_learn_propose_skill.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_learn_closeout = learn_sub.add_parser(
        "closeout", help="Close out a learning candidate as accepted, dismissed, archived, or deferred."
    )
    p_learn_closeout.add_argument("candidate_id", help="Learning candidate id.")
    p_learn_closeout.add_argument("--subsystem", default=None, help="Disambiguate by subsystem.")
    p_learn_closeout.add_argument(
        "--status", choices=sorted(learn_cmd.LEARNING_CLOSEOUT_STATUSES), required=True, help="Closeout status."
    )
    p_learn_closeout.add_argument("--reason", required=True, help="Review reason.")
    p_learn_closeout.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_learn_closeout.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_learn_closeouts = learn_sub.add_parser("closeouts", help="List learning closeout receipts.")
    p_learn_closeouts.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_learn_closeouts.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_learn_closeout_show = learn_sub.add_parser("closeout-show", help="Show a learning closeout receipt.")
    p_learn_closeout_show.add_argument("closeout_id", help="Closeout id or latest.")
    p_learn_closeout_show.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_learn_closeout_show.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_learn_replay = learn_sub.add_parser("replay", help="Export, inspect, and compare safe learning replay receipts.")
    learn_replay_sub = p_learn_replay.add_subparsers(dest="learn_replay_command", metavar="<learn-replay-command>")
    learn_replay_sub.required = True
    p_learn_replay_export = learn_replay_sub.add_parser(
        "export", help="Export a safe before/after learning replay receipt."
    )
    p_learn_replay_export.add_argument("scenario_id", help="Scenario id.")
    p_learn_replay_export.add_argument("--before-summary", required=True, help="Safe before summary.")
    p_learn_replay_export.add_argument("--after-summary", required=True, help="Safe after summary.")
    p_learn_replay_export.add_argument(
        "--before-count", type=int, default=None, help="Optional before candidate count."
    )
    p_learn_replay_export.add_argument("--after-count", type=int, default=None, help="Optional after candidate count.")
    p_learn_replay_export.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_learn_replay_export.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_learn_replay_list = learn_replay_sub.add_parser("list", help="List learning replay receipts.")
    p_learn_replay_list.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_learn_replay_list.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_learn_replay_show = learn_replay_sub.add_parser("show", help="Show a learning replay receipt.")
    p_learn_replay_show.add_argument("replay_id", help="Replay id or latest.")
    p_learn_replay_show.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_learn_replay_show.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_learn_replay_compare = learn_replay_sub.add_parser(
        "compare", help="Compare a learning replay before and after state."
    )
    p_learn_replay_compare.add_argument("replay_id", help="Replay id or latest.")
    p_learn_replay_compare.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_learn_replay_compare.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_learn.set_defaults(func=dispatch)


def dispatch(args) -> int:
    from .. import learn_cmd

    if args.learn_command == "plan":
        return learn_cmd.plan(target=args.target, json_output=args.json)
    if args.learn_command == "doctor":
        return learn_cmd.doctor(target=args.target, json_output=args.json)
    if args.learn_command == "import-issues":
        return learn_cmd.import_issues(target=args.target, dry_run=args.dry_run, json_output=args.json)
    if args.learn_command == "import-learnings":
        return learn_cmd.import_learnings(
            target=args.target, files=args.file or None, dry_run=args.dry_run, json_output=args.json
        )
    if args.learn_command == "skill-candidates":
        return learn_cmd.skill_candidates(
            target=args.target, min_count=args.min_count, source=args.source, json_output=args.json
        )
    if args.learn_command == "propose-skill":
        return learn_cmd.propose_skill(
            target=args.target,
            candidate_id=args.candidate_id,
            min_count=args.min_count,
            source=args.source,
            dry_run=args.dry_run,
            force=args.force,
            json_output=args.json,
        )
    if args.learn_command == "closeout":
        return learn_cmd.closeout(
            target=args.target,
            candidate_id=args.candidate_id,
            subsystem=args.subsystem,
            status=args.status,
            reason=args.reason,
            json_output=args.json,
        )
    if args.learn_command == "closeouts":
        return learn_cmd.closeouts(target=args.target, json_output=args.json)
    if args.learn_command == "closeout-show":
        return learn_cmd.closeout_show(target=args.target, closeout_id=args.closeout_id, json_output=args.json)
    if args.learn_command == "replay":
        if args.learn_replay_command == "export":
            return learn_cmd.replay_export(
                target=args.target,
                scenario_id=args.scenario_id,
                before_summary=args.before_summary,
                after_summary=args.after_summary,
                before_count=args.before_count,
                after_count=args.after_count,
                json_output=args.json,
            )
        if args.learn_replay_command == "list":
            return learn_cmd.replay_list(target=args.target, json_output=args.json)
        if args.learn_replay_command == "show":
            return learn_cmd.replay_show(target=args.target, replay_id=args.replay_id, json_output=args.json)
        if args.learn_replay_command == "compare":
            return learn_cmd.replay_compare(target=args.target, replay_id=args.replay_id, json_output=args.json)
        args._brigade_parser.error(f"unknown learn replay command: {args.learn_replay_command}")
        return 2
    args._brigade_parser.error(f"unknown learn command: {args.learn_command}")
    return 2
