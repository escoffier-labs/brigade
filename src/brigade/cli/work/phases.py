"""brigade work command group."""
# ruff: noqa: E402,F401,F403,F811,F821

from __future__ import annotations

import argparse
import json
from pathlib import Path

from ... import extras as _extras_mod
from ...dogfood_cmd import DEFAULT_TIMEOUT_SECONDS
from ...work_cmd import TASK_PRIORITIES, TASK_TYPES
from .. import extras as _extras_cli

from . import register as _family_base

globals().update({name: value for name, value in vars(_family_base).items() if not name.startswith("__")})


def _register_phases(work_sub: argparse._SubParsersAction) -> None:
    p_work_phases = work_sub.add_parser("phases", help="Plan and inspect auditable phase execution records.")
    phases_sub = p_work_phases.add_subparsers(dest="phases_command", metavar="<phases-command>")
    phases_sub.required = True
    p_work_phases_init = phases_sub.add_parser("init", help="Initialize the local phase execution ledger.")
    p_work_phases_init.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_work_phases_init.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_plan = phases_sub.add_parser("plan", help="Plan one phase or a range of phases.")
    p_work_phases_plan.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_work_phases_plan.add_argument(
        "--phase-id", "--phase", dest="phase_id", default=None, help="Phase id to create, such as phase-165."
    )
    p_work_phases_plan.add_argument(
        "--range", dest="phase_range", default=None, help="Phase range to create, such as 165-170."
    )
    p_work_phases_plan.add_argument("--title", default=None, help="Phase title.")
    p_work_phases_plan.add_argument("--goal", dest="source_goal", default=None, help="Source goal text or label.")
    p_work_phases_plan.add_argument("--grouped", action="store_true", help="Declare an explicit grouped phase range.")
    p_work_phases_plan.add_argument("--force", action="store_true", help="Overwrite existing phase records.")
    p_work_phases_plan.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_list = phases_sub.add_parser("list", help="List local phase records.")
    p_work_phases_list.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_work_phases_list.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_schema = phases_sub.add_parser("schema", help="Show phase ledger JSON contracts.")
    p_work_phases_schema.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_work_phases_schema.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_status = phases_sub.add_parser("status", help="Summarize phase ledger range status.")
    p_work_phases_status.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_work_phases_status.add_argument("--range", dest="phase_range", default=None, help="Phase range, such as 165-170.")
    p_work_phases_status.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_next = phases_sub.add_parser("next", help="Show the next open phase.")
    p_work_phases_next.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_work_phases_next.add_argument("--range", dest="phase_range", default=None, help="Phase range, such as 165-170.")
    p_work_phases_next.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_show = phases_sub.add_parser("show", help="Show one local phase record.")
    p_work_phases_show.add_argument("phase_id", help="Phase id or unique prefix.")
    p_work_phases_show.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_work_phases_show.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_start = phases_sub.add_parser("start", help="Mark one phase in progress.")
    p_work_phases_start.add_argument("phase_id", help="Phase id or unique prefix.")
    p_work_phases_start.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_work_phases_start.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_complete = phases_sub.add_parser("complete", help="Attach completion evidence to one phase.")
    p_work_phases_complete.add_argument("phase_id", help="Phase id or unique prefix.")
    p_work_phases_complete.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_work_phases_complete.add_argument(
        "--status",
        choices=["implemented", "verified", "committed", "pushed"],
        default="implemented",
        help="Completion status.",
    )
    p_work_phases_complete.add_argument("--summary", default=None, help="Implementation summary.")
    p_work_phases_complete.add_argument(
        "--file", dest="files_changed", action="append", default=[], help="Changed file. May be repeated."
    )
    p_work_phases_complete.add_argument(
        "--test", dest="tests_run", action="append", default=[], help="Verification command. May be repeated."
    )
    p_work_phases_complete.add_argument("--test-result", default=None, help="Test result summary.")
    p_work_phases_complete.add_argument("--commit", dest="commit_hash", default=None, help="Commit hash.")
    p_work_phases_complete.add_argument("--push-ref", default=None, help="Push ref.")
    p_work_phases_complete.add_argument(
        "--deferred-item", action="append", default=[], help="Deferred item. May be repeated."
    )
    p_work_phases_complete.add_argument(
        "--next", dest="next_phase_recommendation", default=None, help="Next phase recommendation."
    )
    p_work_phases_complete.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_defer = phases_sub.add_parser("defer", help="Defer one phase with a reason.")
    p_work_phases_defer.add_argument("phase_id", help="Phase id or unique prefix.")
    p_work_phases_defer.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_work_phases_defer.add_argument("--reason", required=True, help="Deferral reason.")
    p_work_phases_defer.add_argument(
        "--next", dest="next_phase_recommendation", default=None, help="Next phase recommendation."
    )
    p_work_phases_defer.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_closeout = phases_sub.add_parser("closeout", help="Review or close out phase records.")
    p_work_phases_closeout.add_argument("selector", help="Phase id, range such as 201-205, or latest.")
    p_work_phases_closeout.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_work_phases_closeout.add_argument(
        "--status", choices=["reviewed", "deferred", "blocked", "archived"], default="reviewed", help="Closeout state."
    )
    p_work_phases_closeout.add_argument("--reason", default=None, help="Closeout reason.")
    p_work_phases_closeout.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_compare = phases_sub.add_parser("compare", help="Compare phase evidence against current local state.")
    p_work_phases_compare.add_argument("selector", help="Phase id, range such as 201-205, or latest.")
    p_work_phases_compare.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_work_phases_compare.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_reconcile = phases_sub.add_parser(
        "reconcile", help="Reconcile phase commit and push evidence against local git state."
    )
    p_work_phases_reconcile.add_argument("selector", help="Phase id, range such as 211-225, or latest.")
    p_work_phases_reconcile.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_work_phases_reconcile.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_privacy = phases_sub.add_parser(
        "privacy", help="Scan phase evidence for protected private/reference values."
    )
    p_work_phases_privacy.add_argument("selector", help="Phase id, range such as 211-225, or latest.")
    p_work_phases_privacy.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_work_phases_privacy.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_handoff = phases_sub.add_parser("handoff", help="Draft a Memory Handoff from phase evidence.")
    p_work_phases_handoff.add_argument("selector", help="Phase id, range such as 211-225, or latest.")
    p_work_phases_handoff.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_work_phases_handoff.add_argument("--lint", action="store_true", help="Run handoff lint before returning.")
    p_work_phases_handoff.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_doctor = phases_sub.add_parser("doctor", help="Check phase execution ledger health.")
    p_work_phases_doctor.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_work_phases_doctor.add_argument(
        "--range", dest="phase_range", default=None, help="Required phase range, such as 165-170."
    )
    p_work_phases_doctor.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_import = phases_sub.add_parser(
        "import-issues", help="Import phase ledger issues into the work inbox."
    )
    p_work_phases_import.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_work_phases_import.add_argument("--range", dest="phase_range", default=None, help="Phase range, such as 165-170.")
    p_work_phases_import.add_argument("--dry-run", action="store_true", help="Report imports without writing them.")
    p_work_phases_import.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_evidence = phases_sub.add_parser("evidence", help="Attach local evidence metadata to phase records.")
    phases_evidence_sub = p_work_phases_evidence.add_subparsers(
        dest="phases_evidence_command", metavar="<phases-evidence-command>"
    )
    phases_evidence_sub.required = True
    p_work_phases_evidence_add = phases_evidence_sub.add_parser("add", help="Attach local evidence to one phase.")
    p_work_phases_evidence_add.add_argument("phase_id", help="Phase id or unique prefix.")
    p_work_phases_evidence_add.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_work_phases_evidence_add.add_argument(
        "--file", dest="files_changed", action="append", default=[], help="Changed file path. May be repeated."
    )
    p_work_phases_evidence_add.add_argument(
        "--test", dest="tests_run", action="append", default=[], help="Verification command. May be repeated."
    )
    p_work_phases_evidence_add.add_argument("--test-result", default=None, help="Verification result summary.")
    p_work_phases_evidence_add.add_argument(
        "--report-id", action="append", default=[], help="Related phase report id. May be repeated."
    )
    p_work_phases_evidence_add.add_argument(
        "--handoff", dest="handoff_paths", action="append", default=[], help="Memory Handoff path. May be repeated."
    )
    p_work_phases_evidence_add.add_argument(
        "--note", dest="notes", action="append", default=[], help="Evidence note. May be repeated."
    )
    p_work_phases_evidence_add.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_verify = phases_sub.add_parser("verify", help="Plan and record phase verification metadata.")
    phases_verify_sub = p_work_phases_verify.add_subparsers(
        dest="phases_verify_command", metavar="<phases-verify-command>"
    )
    phases_verify_sub.required = True
    p_work_phases_verify_plan = phases_verify_sub.add_parser("plan", help="Plan verification for a phase selector.")
    p_work_phases_verify_plan.add_argument("selector", help="Phase id, range such as 211-225, or latest.")
    p_work_phases_verify_plan.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_work_phases_verify_plan.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_verify_record = phases_verify_sub.add_parser("record", help="Record one phase verification result.")
    p_work_phases_verify_record.add_argument("phase_id", help="Phase id or unique prefix.")
    p_work_phases_verify_record.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_work_phases_verify_record.add_argument(
        "--command", dest="verification_command", required=True, help="Verification command label."
    )
    p_work_phases_verify_record.add_argument(
        "--status", choices=["passed", "failed", "skipped", "deferred"], required=True, help="Verification result."
    )
    p_work_phases_verify_record.add_argument("--summary", default=None, help="Verification result summary.")
    p_work_phases_verify_record.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_actions = phases_sub.add_parser("actions", help="Plan and manage local phase ledger action records.")
    phases_actions_sub = p_work_phases_actions.add_subparsers(
        dest="phases_actions_command", metavar="<phases-actions-command>"
    )
    phases_actions_sub.required = True
    p_work_phases_actions_plan = phases_actions_sub.add_parser(
        "plan", help="Preview phase ledger actions from current issues."
    )
    p_work_phases_actions_plan.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_work_phases_actions_plan.add_argument(
        "--range", dest="phase_range", default=None, help="Phase range, such as 201-205."
    )
    p_work_phases_actions_plan.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_actions_build = phases_actions_sub.add_parser(
        "build", help="Build local phase ledger action records."
    )
    p_work_phases_actions_build.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_work_phases_actions_build.add_argument(
        "--range", dest="phase_range", default=None, help="Phase range, such as 201-205."
    )
    p_work_phases_actions_build.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_actions_list = phases_actions_sub.add_parser("list", help="List local phase ledger actions.")
    p_work_phases_actions_list.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_work_phases_actions_list.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_actions_show = phases_actions_sub.add_parser("show", help="Show one phase ledger action.")
    p_work_phases_actions_show.add_argument("action_id", help="Action id or unique prefix.")
    p_work_phases_actions_show.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_work_phases_actions_show.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_actions_start = phases_actions_sub.add_parser("start", help="Mark one phase ledger action active.")
    p_work_phases_actions_start.add_argument("action_id", help="Action id or unique prefix.")
    p_work_phases_actions_start.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_work_phases_actions_start.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_actions_done = phases_actions_sub.add_parser("done", help="Mark one phase ledger action done.")
    p_work_phases_actions_done.add_argument("action_id", help="Action id or unique prefix.")
    p_work_phases_actions_done.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_work_phases_actions_done.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_actions_defer = phases_actions_sub.add_parser("defer", help="Defer one phase ledger action.")
    p_work_phases_actions_defer.add_argument("action_id", help="Action id or unique prefix.")
    p_work_phases_actions_defer.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_work_phases_actions_defer.add_argument("--reason", required=True, help="Deferral reason.")
    p_work_phases_actions_defer.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_actions_archive = phases_actions_sub.add_parser("archive", help="Archive phase ledger actions.")
    p_work_phases_actions_archive.add_argument("action_id", nargs="?", default=None, help="Action id or unique prefix.")
    p_work_phases_actions_archive.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_work_phases_actions_archive.add_argument(
        "--completed", action="store_true", help="Archive done and deferred actions."
    )
    p_work_phases_actions_archive.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_actions_import = phases_actions_sub.add_parser(
        "import-issues", help="Import open phase actions into the work inbox."
    )
    p_work_phases_actions_import.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_work_phases_actions_import.add_argument(
        "--dry-run", action="store_true", help="Report imports without writing them."
    )
    p_work_phases_actions_import.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_goal = phases_sub.add_parser("goal", help="Draft reviewed phase goal prompts.")
    phases_goal_sub = p_work_phases_goal.add_subparsers(dest="phases_goal_command", metavar="<phases-goal-command>")
    phases_goal_sub.required = True
    p_work_phases_goal_scaffold = phases_goal_sub.add_parser(
        "scaffold", help="Draft a local goal prompt from phase ledger state."
    )
    p_work_phases_goal_scaffold.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_work_phases_goal_scaffold.add_argument(
        "--range", dest="phase_range", required=True, help="Phase range, such as 211-225."
    )
    p_work_phases_goal_scaffold.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_report = phases_sub.add_parser("report", help="Build and inspect phase ledger reports.")
    phases_report_sub = p_work_phases_report.add_subparsers(
        dest="phases_report_command", metavar="<phases-report-command>"
    )
    phases_report_sub.required = True
    p_work_phases_report_build = phases_report_sub.add_parser("build", help="Build a local phase ledger report.")
    p_work_phases_report_build.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_work_phases_report_build.add_argument(
        "--range", dest="phase_range", default=None, help="Phase range, such as 165-170."
    )
    p_work_phases_report_build.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_report_list = phases_report_sub.add_parser("list", help="List phase ledger reports.")
    p_work_phases_report_list.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_work_phases_report_list.add_argument("--limit", type=int, default=20, help="Maximum reports to list.")
    p_work_phases_report_list.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_report_show = phases_report_sub.add_parser("show", help="Show one phase ledger report.")
    p_work_phases_report_show.add_argument("report_id", help="Report id, unique prefix, or latest.")
    p_work_phases_report_show.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_work_phases_report_show.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_report_closeout = phases_report_sub.add_parser("closeout", help="Close out one phase ledger report.")
    p_work_phases_report_closeout.add_argument("report_id", help="Report id, unique prefix, or latest.")
    p_work_phases_report_closeout.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_work_phases_report_closeout.add_argument(
        "--status",
        choices=["reviewed", "deferred", "superseded", "archived"],
        default="reviewed",
        help="Report closeout state.",
    )
    p_work_phases_report_closeout.add_argument("--reason", default=None, help="Closeout reason.")
    p_work_phases_report_closeout.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_report_compare = phases_report_sub.add_parser(
        "compare", help="Compare one phase ledger report against current state."
    )
    p_work_phases_report_compare.add_argument("report_id", help="Report id, unique prefix, or latest.")
    p_work_phases_report_compare.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_work_phases_report_compare.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_session = phases_sub.add_parser("session", help="Start and review phase execution sessions.")
    phases_session_sub = p_work_phases_session.add_subparsers(
        dest="phases_session_command", metavar="<phases-session-command>"
    )
    phases_session_sub.required = True
    p_work_phases_session_start = phases_session_sub.add_parser("start", help="Start a local phase execution session.")
    p_work_phases_session_start.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_work_phases_session_start.add_argument(
        "--range", dest="phase_range", required=True, help="Phase range, such as 211-225."
    )
    p_work_phases_session_start.add_argument("--goal", dest="source_goal", default=None, help="Source goal text.")
    p_work_phases_session_start.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_session_list = phases_session_sub.add_parser("list", help="List phase execution sessions.")
    p_work_phases_session_list.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_work_phases_session_list.add_argument("--limit", type=int, default=20, help="Maximum sessions to list.")
    p_work_phases_session_list.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_session_show = phases_session_sub.add_parser("show", help="Show one phase execution session.")
    p_work_phases_session_show.add_argument("session_id", help="Session id, unique prefix, or latest.")
    p_work_phases_session_show.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_work_phases_session_show.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_session_checkpoint = phases_session_sub.add_parser(
        "checkpoint", help="Record a local phase session checkpoint."
    )
    p_work_phases_session_checkpoint.add_argument(
        "session_id", nargs="?", default="latest", help="Session id, unique prefix, or latest."
    )
    p_work_phases_session_checkpoint.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_work_phases_session_checkpoint.add_argument("--phase-id", default=None, help="Phase id for the checkpoint.")
    p_work_phases_session_checkpoint.add_argument(
        "--status", choices=["noted", "blocked", "recovered"], default="noted", help="Checkpoint state."
    )
    p_work_phases_session_checkpoint.add_argument("--summary", default=None, help="Safe checkpoint summary.")
    p_work_phases_session_checkpoint.add_argument(
        "--note", dest="notes", action="append", default=[], help="Safe local note. May be repeated."
    )
    p_work_phases_session_checkpoint.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_session_checkpoints = phases_session_sub.add_parser(
        "checkpoints", help="List and inspect phase session checkpoints."
    )
    phases_session_checkpoints_sub = p_work_phases_session_checkpoints.add_subparsers(
        dest="phases_session_checkpoints_command", metavar="<phases-session-checkpoints-command>"
    )
    phases_session_checkpoints_sub.required = True
    p_work_phases_session_checkpoints_list = phases_session_checkpoints_sub.add_parser(
        "list", help="List phase session checkpoints."
    )
    p_work_phases_session_checkpoints_list.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_work_phases_session_checkpoints_list.add_argument(
        "--session", dest="session_id", default=None, help="Limit to one session id, prefix, or latest."
    )
    p_work_phases_session_checkpoints_list.add_argument(
        "--limit", type=int, default=20, help="Maximum checkpoints to list."
    )
    p_work_phases_session_checkpoints_list.add_argument(
        "--json", action="store_true", help="Print machine-readable JSON."
    )
    p_work_phases_session_checkpoints_show = phases_session_checkpoints_sub.add_parser(
        "show", help="Show one phase session checkpoint."
    )
    p_work_phases_session_checkpoints_show.add_argument(
        "checkpoint_id", help="Checkpoint id, unique prefix, or latest."
    )
    p_work_phases_session_checkpoints_show.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_work_phases_session_checkpoints_show.add_argument(
        "--json", action="store_true", help="Print machine-readable JSON."
    )
    p_work_phases_session_checkpoints_compare = phases_session_checkpoints_sub.add_parser(
        "compare", help="Compare one checkpoint against current session state."
    )
    p_work_phases_session_checkpoints_compare.add_argument(
        "checkpoint_id", help="Checkpoint id, unique prefix, or latest."
    )
    p_work_phases_session_checkpoints_compare.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_work_phases_session_checkpoints_compare.add_argument(
        "--json", action="store_true", help="Print machine-readable JSON."
    )
    p_work_phases_session_checkpoints_import = phases_session_checkpoints_sub.add_parser(
        "import-issues", help="Import checkpoint blockers into the work inbox."
    )
    p_work_phases_session_checkpoints_import.add_argument(
        "checkpoint_id", nargs="?", default="latest", help="Checkpoint id, unique prefix, or latest."
    )
    p_work_phases_session_checkpoints_import.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_work_phases_session_checkpoints_import.add_argument(
        "--dry-run", action="store_true", help="Preview imports without writing them."
    )
    p_work_phases_session_checkpoints_import.add_argument(
        "--json", action="store_true", help="Print machine-readable JSON."
    )
    p_work_phases_session_checkpoints_archive = phases_session_checkpoints_sub.add_parser(
        "archive", help="Archive one phase session checkpoint."
    )
    p_work_phases_session_checkpoints_archive.add_argument(
        "checkpoint_id", help="Checkpoint id, unique prefix, or latest."
    )
    p_work_phases_session_checkpoints_archive.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_work_phases_session_checkpoints_archive.add_argument(
        "--json", action="store_true", help="Print machine-readable JSON."
    )
    p_work_phases_session_recovery_note = phases_session_sub.add_parser(
        "recovery-note", help="Record a local phase session recovery note."
    )
    p_work_phases_session_recovery_note.add_argument(
        "session_id", nargs="?", default="latest", help="Session id, unique prefix, or latest."
    )
    p_work_phases_session_recovery_note.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_work_phases_session_recovery_note.add_argument("--phase-id", default=None, help="Phase id for the recovery note.")
    p_work_phases_session_recovery_note.add_argument("--summary", default=None, help="Safe recovery note summary.")
    p_work_phases_session_recovery_note.add_argument(
        "--note", dest="notes", action="append", default=[], help="Safe local note. May be repeated."
    )
    p_work_phases_session_recovery_note.add_argument(
        "--evidence", action="append", default=[], help="Local evidence label or reference. May be repeated."
    )
    p_work_phases_session_recovery_note.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_session_recovery_notes = phases_session_sub.add_parser(
        "recovery-notes", help="List and inspect phase session recovery notes."
    )
    phases_session_recovery_notes_sub = p_work_phases_session_recovery_notes.add_subparsers(
        dest="phases_session_recovery_notes_command", metavar="<phases-session-recovery-notes-command>"
    )
    phases_session_recovery_notes_sub.required = True
    p_work_phases_session_recovery_notes_list = phases_session_recovery_notes_sub.add_parser(
        "list", help="List phase session recovery notes."
    )
    p_work_phases_session_recovery_notes_list.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_work_phases_session_recovery_notes_list.add_argument(
        "--session", dest="session_id", default=None, help="Limit to one session id, prefix, or latest."
    )
    p_work_phases_session_recovery_notes_list.add_argument(
        "--limit", type=int, default=20, help="Maximum recovery notes to list."
    )
    p_work_phases_session_recovery_notes_list.add_argument(
        "--json", action="store_true", help="Print machine-readable JSON."
    )
    p_work_phases_session_recovery_notes_show = phases_session_recovery_notes_sub.add_parser(
        "show", help="Show one phase session recovery note."
    )
    p_work_phases_session_recovery_notes_show.add_argument(
        "note_id", help="Recovery note id, unique prefix, or latest."
    )
    p_work_phases_session_recovery_notes_show.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_work_phases_session_recovery_notes_show.add_argument(
        "--json", action="store_true", help="Print machine-readable JSON."
    )
    p_work_phases_session_recovery_notes_closeout = phases_session_recovery_notes_sub.add_parser(
        "closeout", help="Close out one phase session recovery note."
    )
    p_work_phases_session_recovery_notes_closeout.add_argument(
        "note_id", help="Recovery note id, unique prefix, or latest."
    )
    p_work_phases_session_recovery_notes_closeout.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_work_phases_session_recovery_notes_closeout.add_argument(
        "--status",
        choices=["reviewed", "deferred", "blocked", "archived"],
        default="reviewed",
        help="Recovery note closeout state.",
    )
    p_work_phases_session_recovery_notes_closeout.add_argument("--reason", default=None, help="Closeout reason.")
    p_work_phases_session_recovery_notes_closeout.add_argument(
        "--json", action="store_true", help="Print machine-readable JSON."
    )
    p_work_phases_session_risk = phases_session_sub.add_parser("risk", help="Summarize phase session risk.")
    p_work_phases_session_risk.add_argument(
        "session_id", nargs="?", default="latest", help="Session id, unique prefix, or latest."
    )
    p_work_phases_session_risk.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_work_phases_session_risk.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_session_verification = phases_session_sub.add_parser(
        "verification", help="Summarize phase session verification."
    )
    p_work_phases_session_verification.add_argument(
        "session_id", nargs="?", default="latest", help="Session id, unique prefix, or latest."
    )
    p_work_phases_session_verification.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_work_phases_session_verification.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_session_privacy = phases_session_sub.add_parser(
        "privacy", help="Summarize phase session privacy checks."
    )
    p_work_phases_session_privacy.add_argument(
        "session_id", nargs="?", default="latest", help="Session id, unique prefix, or latest."
    )
    p_work_phases_session_privacy.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_work_phases_session_privacy.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_session_handoffs = phases_session_sub.add_parser(
        "handoffs", help="Summarize phase session handoff coverage."
    )
    p_work_phases_session_handoffs.add_argument(
        "session_id", nargs="?", default="latest", help="Session id, unique prefix, or latest."
    )
    p_work_phases_session_handoffs.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_work_phases_session_handoffs.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_session_next = phases_session_sub.add_parser(
        "next", help="Show the next required phase session step."
    )
    p_work_phases_session_next.add_argument(
        "session_id", nargs="?", default="latest", help="Session id, unique prefix, or latest."
    )
    p_work_phases_session_next.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_work_phases_session_next.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_session_protocol = phases_session_sub.add_parser(
        "protocol", help="Show wrapper-safe phase session resume protocol."
    )
    p_work_phases_session_protocol.add_argument(
        "session_id", nargs="?", default="latest", help="Session id, unique prefix, or latest."
    )
    p_work_phases_session_protocol.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_work_phases_session_protocol.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_session_audit = phases_session_sub.add_parser("audit", help="Self-audit AFK phase session evidence.")
    p_work_phases_session_audit.add_argument(
        "session_id", nargs="?", default="latest", help="Session id, unique prefix, or latest."
    )
    p_work_phases_session_audit.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_work_phases_session_audit.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_session_resume = phases_session_sub.add_parser(
        "resume", help="Record a safe phase session resume recommendation."
    )
    p_work_phases_session_resume.add_argument(
        "session_id", nargs="?", default="latest", help="Session id, unique prefix, or latest."
    )
    p_work_phases_session_resume.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_work_phases_session_resume.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_session_closeout = phases_session_sub.add_parser(
        "closeout", help="Close out one phase execution session."
    )
    p_work_phases_session_closeout.add_argument("session_id", help="Session id, unique prefix, or latest.")
    p_work_phases_session_closeout.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_work_phases_session_closeout.add_argument(
        "--status",
        choices=["reviewed", "deferred", "blocked", "archived"],
        default="reviewed",
        help="Session closeout state.",
    )
    p_work_phases_session_closeout.add_argument("--reason", default=None, help="Closeout reason.")
    p_work_phases_session_closeout.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_session_activity = phases_session_sub.add_parser(
        "activity", help="Show chronological phase session activity."
    )
    p_work_phases_session_activity.add_argument(
        "session_id", nargs="?", default="latest", help="Session id, unique prefix, or latest."
    )
    p_work_phases_session_activity.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_work_phases_session_activity.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_session_progress = phases_session_sub.add_parser(
        "progress", help="Show phase session progress summary."
    )
    p_work_phases_session_progress.add_argument(
        "session_id", nargs="?", default="latest", help="Session id, unique prefix, or latest."
    )
    p_work_phases_session_progress.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_work_phases_session_progress.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_session_import = phases_session_sub.add_parser(
        "import-issues", help="Import unresolved phase session blockers into the work inbox."
    )
    p_work_phases_session_import.add_argument(
        "session_id", nargs="?", default="latest", help="Session id, unique prefix, or latest."
    )
    p_work_phases_session_import.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_work_phases_session_import.add_argument(
        "--dry-run", action="store_true", help="Preview imports without writing them."
    )
    p_work_phases_session_import.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_session_gate = phases_session_sub.add_parser(
        "gate", help="Check whether a phase session is safe to claim complete."
    )
    p_work_phases_session_gate.add_argument(
        "session_id", nargs="?", default="latest", help="Session id, unique prefix, or latest."
    )
    p_work_phases_session_gate.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_work_phases_session_gate.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_session_report = phases_session_sub.add_parser(
        "report", help="Build and inspect phase session reports."
    )
    phases_session_report_sub = p_work_phases_session_report.add_subparsers(
        dest="phases_session_report_command", metavar="<phases-session-report-command>"
    )
    phases_session_report_sub.required = True
    p_work_phases_session_report_build = phases_session_report_sub.add_parser(
        "build", help="Build a local phase session report."
    )
    p_work_phases_session_report_build.add_argument(
        "session_id", nargs="?", default="latest", help="Session id, unique prefix, or latest."
    )
    p_work_phases_session_report_build.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_work_phases_session_report_build.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_session_report_list = phases_session_report_sub.add_parser("list", help="List phase session reports.")
    p_work_phases_session_report_list.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_work_phases_session_report_list.add_argument("--limit", type=int, default=20, help="Maximum reports to list.")
    p_work_phases_session_report_list.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_work_phases_session_report_show = phases_session_report_sub.add_parser(
        "show", help="Show one phase session report."
    )
    p_work_phases_session_report_show.add_argument("report_id", help="Report id, unique prefix, or latest.")
    p_work_phases_session_report_show.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_work_phases_session_report_show.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
