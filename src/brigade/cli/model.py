"""brigade model command group."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any


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
            "--roster",
            type=Path,
            default=None,
            help="Roster path. Uses normal workspace/worktree-parent/user fallback.",
        )
        parser.add_argument("--output-dir", type=Path, default=None, help="Trial artifact directory.")
        parser.add_argument(
            "--run-budget",
            type=Path,
            default=None,
            help="Path to a brigade.run_budget.v1 JSON declaration for each trial cell.",
        )
        if command in {"run", "resume"}:
            parser.add_argument(
                "--retry-transient",
                action="store_true",
                help=(
                    "Re-run adapter_error and timeout cells as new attempts (original attempt kept; "
                    "capped per cell). Deterministic results are never retried."
                ),
            )
        parser.set_defaults(func=_dispatch_trial)
    for command in ("show", "summary"):
        parser = trial_sub.add_parser(command, help=f"{command.title()} trial artifacts.")
        parser.add_argument("output_dir", type=Path, help="Trial artifact directory.")
        parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
        parser.set_defaults(func=_dispatch_trial)
    for command in ("paired-plan", "paired-replay", "paired-validate", "paired-live"):
        parser = trial_sub.add_parser(command, help="Audit-only paired brigade-work pilot apparatus.")
        parser.add_argument("manifest", type=Path)
        parser.add_argument("--output-dir", type=Path)
        parser.add_argument("--transcript", type=Path, help="Offline JSON protocol script; never live evidence.")
        parser.add_argument("--evidence-sha256", help="Reviewed evidence digest required for validation.")
        if command == "paired-live":
            parser.add_argument("--claude-bin", default="claude", help="Claude Code executable (default: claude).")
            parser.add_argument(
                "--case", dest="cases", action="append", help="Run only this case; repeat for several (default: all)."
            )
            parser.add_argument("--timeout", type=float, default=600.0, help="Seconds allowed per arm session.")
            parser.add_argument(
                "--resume-digest",
                help=(
                    "Reviewed evidence digest of the existing output. Without it, earlier evidence in the "
                    "output directory is moved aside and every cell runs fresh."
                ),
            )
        parser.set_defaults(func=_dispatch_paired)
    p_mcp = trial_sub.add_parser(
        "paired-mcp", help="Exclusive MCP server for a live paired arm (spawned by the adapter)."
    )
    p_mcp.add_argument("--case", required=True)
    p_mcp.add_argument("--target", type=Path, required=True, help="Fresh empty fixture target.")
    p_mcp.add_argument("--package-dir", type=Path, required=True, help="Skill package for this arm.")
    p_mcp.add_argument("--state-dir", type=Path, required=True, help="Directory for the authoritative trace.")
    p_mcp.set_defaults(func=_dispatch_paired_mcp)
    p_drain = trial_sub.add_parser(
        "drain",
        help="Ask a running trial to launch no more cells; in-flight cells finish and record results.",
    )
    p_drain.add_argument("output_dir", type=Path, help="Trial artifact directory.")
    p_drain.set_defaults(func=_dispatch_trial)
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

    from .. import model_trials, run_budget
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
    if args.trial_command == "drain":
        output_dir = args.output_dir.expanduser().resolve()
        if not (output_dir / "plan.json").is_file():
            print(f"error: no trial plan at {output_dir}", file=sys.stderr)
            return 2
        marker = model_trials.request_drain(output_dir)
        print(f"drain requested: {marker}")
        return 0
    if args.trial_command == "regrade":
        return model_trials.regrade(args.output_dir)

    target = args.target.expanduser().resolve()
    try:
        run_budget_payload = run_budget.load_declaration_file(args.run_budget) if args.run_budget is not None else None
        roster_path = roster_mod.resolve_roster_path(target, args.roster)
        roster = roster_mod.load_roster(roster_path)
        manifest = model_trials.load_manifest(args.manifest)
        output_dir = args.output_dir or target / ".brigade" / "evals" / manifest["name"]
        plan, cells = model_trials.build_plan(args.manifest, roster, output_dir)
    except (FileNotFoundError, ValueError, run_budget.BudgetCompatibilityError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.trial_command == "plan":
        print(json.dumps(plan, indent=2, sort_keys=True))
        return 0
    execute_kwargs: dict[str, Any] = {
        "workspace": target,
        "output_dir": output_dir,
        "resume": args.trial_command == "resume",
    }
    if run_budget_payload is not None:
        execute_kwargs["run_budget_payload"] = run_budget_payload
    if getattr(args, "retry_transient", False):
        execute_kwargs["retry_transient"] = True
    return model_trials.execute(
        args.manifest,
        roster,
        **execute_kwargs,
    )


def _dispatch_paired(args) -> int:
    import json
    import subprocess
    import sys

    from .. import model_trials
    from ..model_trial_paired import replay_session

    try:
        if args.trial_command == "paired-live":
            if args.output_dir is None:
                raise ValueError("live paired trials need --output-dir")
            result = model_trials.paired_live(
                args.manifest,
                args.output_dir,
                claude_bin=args.claude_bin,
                cases=args.cases,
                timeout=args.timeout,
                resume_digest=args.resume_digest,
            )
        elif args.trial_command == "paired-plan":
            result = model_trials.paired_plan(args.manifest)
        elif args.trial_command == "paired-validate":
            if args.output_dir is None or args.evidence_sha256 is None:
                raise ValueError("validation needs --output-dir and reviewed --evidence-sha256")
            result = model_trials.paired_validate(args.output_dir, args.evidence_sha256)
        else:
            if args.output_dir is None or args.transcript is None:
                raise ValueError("offline replay needs --output-dir and --transcript")
            script = json.loads(args.transcript.read_text(encoding="utf-8"))
            result = model_trials.paired_execute(args.manifest, args.output_dir, replay_session(script))
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def _dispatch_paired_mcp(args) -> int:
    from ..paired_mcp import serve

    return serve(args.case, args.target, args.package_dir, args.state_dir)
