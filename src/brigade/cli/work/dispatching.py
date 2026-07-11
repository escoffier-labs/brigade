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

from . import registration as _family_base

globals().update({name: value for name, value in vars(_family_base).items() if not name.startswith("__")})


def dispatch(args) -> int:
    from ... import work_cmd

    if args.work_command == "status":
        return work_cmd.status(target=args.target, limit=args.limit)
    if args.work_command == "doctor":
        return work_cmd.doctor(target=args.target)
    if args.work_command == "bootstrap":
        return work_cmd.bootstrap(
            target=args.target,
            artifacts_dir=args.artifacts_dir,
            handoff_inbox=args.handoff_inbox,
            force=args.force,
            handoff=not args.no_handoff,
            inspect=not args.no_inspect,
            native_read_only_sandbox=args.native_read_only_sandbox,
            timeout_seconds=args.timeout_seconds,
            update_gitignore=not args.no_gitignore,
        )
    if args.work_command == "resume":
        return work_cmd.resume(target=args.target)
    if args.work_command == "brief":
        return work_cmd.brief(target=args.target, limit=args.limit, json_output=args.json)
    if args.work_command == "sweep":
        if args.sweep_args:
            if args.sweep_args[0] != "closeout":
                args._brigade_parser.error(
                    "work sweep accepts only `closeout <sweep-id|latest>` as positional arguments"
                )
                return 2
            if len(args.sweep_args) > 2:
                args._brigade_parser.error("work sweep closeout accepts at most one sweep id")
                return 2
            return work_cmd.sweep_closeout(
                target=args.target,
                sweep_id=args.sweep_args[1] if len(args.sweep_args) == 2 else "latest",
                reason=args.reason,
                deferred_imports=args.defer,
                defer_all=args.defer_all,
                json_output=args.json,
            )
        return work_cmd.sweep(
            target=args.target,
            scanner_id=args.scanner,
            all_matching=args.all,
            include_disabled=args.include_disabled,
            force=args.force,
            ingest=not args.no_ingest,
            json_output=args.json,
        )
    if args.work_command == "sweeps":
        return work_cmd.sweeps(target=args.target, limit=args.limit, json_output=args.json)
    if args.work_command == "plans":
        return work_cmd.plans(target=args.target, limit=args.limit, json_output=args.json)
    if args.work_command == "plan-promote":
        return work_cmd.plan_promote(
            target=args.target, task_id=args.task_id, as_kind=args.as_kind, json_output=args.json
        )
    if args.work_command == "plan-proposals":
        return work_cmd.plan_proposals(target=args.target, json_output=args.json)
    if args.work_command == "sweep-show":
        return work_cmd.sweep_show(target=args.target, sweep_id=args.sweep_id, json_output=args.json)
    if args.work_command == "sweep-review":
        return work_cmd.sweep_review(target=args.target, sweep_id=args.sweep_id, json_output=args.json)
    if args.work_command == "verify":
        if args.verify_command == "plan":
            return work_cmd.verify_plan(target=args.target, commands=args.verify_commands, json_output=args.json)
        if args.verify_command == "run":
            has_command = bool(args.verify_commands)
            has_argv_json = args.verify_argv_json is not None
            if has_command and has_argv_json:
                args._brigade_parser.error("--command and --argv-json are mutually exclusive")
            if not has_command and not has_argv_json:
                args._brigade_parser.error("work verify run requires exactly one of --command or --argv-json")
            if has_argv_json:
                try:
                    parsed_argv = json.loads(args.verify_argv_json)
                except json.JSONDecodeError as exc:
                    args._brigade_parser.error(f"--argv-json is not valid JSON: {exc}")
                if (
                    not isinstance(parsed_argv, list)
                    or not parsed_argv
                    or not all(isinstance(item, str) for item in parsed_argv)
                ):
                    args._brigade_parser.error("--argv-json must be a JSON array of strings")
                commands = [parsed_argv]
            else:
                commands = args.verify_commands
            return work_cmd.verify_run(
                target=args.target,
                commands=commands,
                timeout=args.timeout,
                json_output=args.json,
                capture=args.capture,
                capture_kind=args.capture_kind,
            )
        if args.verify_command == "runs":
            return work_cmd.verify_runs(target=args.target, limit=args.limit, json_output=args.json)
        if args.verify_command == "show":
            return work_cmd.verify_show(target=args.target, run_id=args.run_id, json_output=args.json)
        args._brigade_parser.error(f"unknown verify command: {args.verify_command}")
        return 2
    if args.work_command == "closeout":
        return work_cmd.closeout(target=args.target, session_id=args.session_id, json_output=args.json)
    if args.work_command == "acceptance":
        return work_cmd.acceptance(target=args.target, json_output=args.json)
    if args.work_command == "inbox" and getattr(args, "inbox_command", None):
        if args.inbox_command == "doctor":
            return work_cmd.inbox_doctor(target=args.target, json_output=args.json)
        if args.inbox_command == "archive":
            return work_cmd.inbox_archive(target=args.target, json_output=args.json)
        args._brigade_parser.error(f"unknown inbox command: {args.inbox_command}")
        return 2
    if args.work_command == "inbox":
        return work_cmd.inbox(target=args.target, json_output=args.json, limit=args.limit)
    if args.work_command == "backup":
        if args.backup_command == "init":
            return work_cmd.backup_init(
                target=args.target,
                force=args.force,
                update_gitignore=not args.no_gitignore,
            )
        if args.backup_command == "contract":
            return work_cmd.backup_contract(
                target=args.target,
                destination_id=args.destination,
                json_output=args.json,
            )
        if args.backup_command == "status":
            return work_cmd.backup_status(target=args.target, json_output=args.json)
        if args.backup_command == "doctor":
            return work_cmd.backup_doctor(target=args.target, json_output=args.json)
        if args.backup_command == "import-issues":
            return work_cmd.backup_import_issues(target=args.target, json_output=args.json)
        if args.backup_command == "closeout":
            return work_cmd.backup_closeout(
                target=args.target,
                reason=args.reason,
                defer=args.defer,
                json_output=args.json,
            )
        args._brigade_parser.error(f"unknown backup command: {args.backup_command}")
        return 2
    if args.work_command == "scanners":
        if args.scanners_command == "init":
            return work_cmd.scanners_init(
                target=args.target,
                force=args.force,
                update_gitignore=not args.no_gitignore,
            )
        if args.scanners_command == "list":
            return work_cmd.scanners_list(target=args.target, json_output=args.json)
        if args.scanners_command == "show":
            return work_cmd.scanners_show(target=args.target, scanner_id=args.scanner_id, json_output=args.json)
        if args.scanners_command == "plan":
            return work_cmd.scanners_plan(target=args.target, json_output=args.json)
        if args.scanners_command == "run":
            return work_cmd.scanners_run(
                target=args.target,
                scanner_id=args.scanner_id,
                all_matching=args.all,
                due=args.due,
                include_disabled=args.include_disabled,
                force=args.force,
                ingest_output=args.ingest_output,
                json_output=args.json,
            )
        if args.scanners_command == "runs":
            return work_cmd.scanners_runs(target=args.target, limit=args.limit, json_output=args.json)
        if args.scanners_command == "run-show":
            return work_cmd.scanners_run_show(target=args.target, run_id=args.run_id, json_output=args.json)
        if args.scanners_command == "doctor":
            return work_cmd.scanners_doctor(
                target=args.target,
                json_output=args.json,
                import_issues=args.import_issues,
            )
        args._brigade_parser.error(f"unknown scanners command: {args.scanners_command}")
        return 2
    if args.work_command == "review":
        if args.review_command == "init":
            return work_cmd.review_init(
                target=args.target,
                force=args.force,
                update_gitignore=not args.no_gitignore,
            )
        if args.review_command == "plan":
            return work_cmd.review_plan(target=args.target, json_output=args.json)
        if args.review_command == "run":
            return work_cmd.review_run(
                target=args.target,
                reviewer_id=args.reviewer_id,
                all_matching=args.all,
                include_disabled=args.include_disabled,
                json_output=args.json,
            )
        if args.review_command == "runs":
            return work_cmd.review_runs(target=args.target, limit=args.limit, json_output=args.json)
        if args.review_command == "show":
            return work_cmd.review_show(target=args.target, run_id=args.run_id, json_output=args.json)
        if args.review_command == "import-findings":
            return work_cmd.review_import_findings(
                target=args.target,
                run_id=args.run_id,
                dry_run=args.dry_run,
                json_output=args.json,
            )
        if args.review_command == "findings":
            return work_cmd.review_findings(target=args.target, run_id=args.run_id, json_output=args.json)
        if args.review_command == "finding-show":
            return work_cmd.review_finding_show(target=args.target, finding_id=args.finding_id, json_output=args.json)
        if args.review_command == "closeout":
            return work_cmd.review_closeout(target=args.target, run_id=args.run_id, json_output=args.json)
        args._brigade_parser.error(f"unknown review command: {args.review_command}")
        return 2
    if args.work_command == "phases":
        from ... import phases_cmd

        if args.phases_command == "init":
            return phases_cmd.init(target=args.target, json_output=args.json)
        if args.phases_command == "plan":
            return phases_cmd.plan(
                target=args.target,
                phase_id=args.phase_id,
                phase_range=args.phase_range,
                title=args.title,
                source_goal=args.source_goal,
                grouped=args.grouped,
                force=args.force,
                json_output=args.json,
            )
        if args.phases_command == "list":
            return phases_cmd.list_phases(target=args.target, json_output=args.json)
        if args.phases_command == "schema":
            return phases_cmd.schema(target=args.target, json_output=args.json)
        if args.phases_command == "status":
            return phases_cmd.status(target=args.target, phase_range=args.phase_range, json_output=args.json)
        if args.phases_command == "next":
            return phases_cmd.next_phase(target=args.target, phase_range=args.phase_range, json_output=args.json)
        if args.phases_command == "show":
            return phases_cmd.show(target=args.target, phase_id=args.phase_id, json_output=args.json)
        if args.phases_command == "start":
            return phases_cmd.start(target=args.target, phase_id=args.phase_id, json_output=args.json)
        if args.phases_command == "complete":
            return phases_cmd.complete(
                target=args.target,
                phase_id=args.phase_id,
                status=args.status,
                summary=args.summary,
                files_changed=args.files_changed,
                tests_run=args.tests_run,
                test_result_summary=args.test_result,
                commit_hash=args.commit_hash,
                push_ref=args.push_ref,
                deferred_items=args.deferred_item,
                next_phase_recommendation=args.next_phase_recommendation,
                json_output=args.json,
            )
        if args.phases_command == "defer":
            return phases_cmd.defer(
                target=args.target,
                phase_id=args.phase_id,
                reason=args.reason,
                next_phase_recommendation=args.next_phase_recommendation,
                json_output=args.json,
            )
        if args.phases_command == "closeout":
            return phases_cmd.closeout(
                target=args.target,
                selector=args.selector,
                status=args.status,
                reason=args.reason,
                json_output=args.json,
            )
        if args.phases_command == "compare":
            return phases_cmd.compare(target=args.target, selector=args.selector, json_output=args.json)
        if args.phases_command == "reconcile":
            return phases_cmd.reconcile(target=args.target, selector=args.selector, json_output=args.json)
        if args.phases_command == "privacy":
            return phases_cmd.privacy(target=args.target, selector=args.selector, json_output=args.json)
        if args.phases_command == "handoff":
            return phases_cmd.handoff(target=args.target, selector=args.selector, lint=args.lint, json_output=args.json)
        if args.phases_command == "doctor":
            return phases_cmd.doctor(target=args.target, phase_range=args.phase_range, json_output=args.json)
        if args.phases_command == "import-issues":
            return phases_cmd.import_issues(
                target=args.target, phase_range=args.phase_range, dry_run=args.dry_run, json_output=args.json
            )
        if args.phases_command == "evidence":
            if args.phases_evidence_command == "add":
                return phases_cmd.evidence_add(
                    target=args.target,
                    phase_id=args.phase_id,
                    files_changed=args.files_changed,
                    tests_run=args.tests_run,
                    test_result_summary=args.test_result,
                    report_ids=args.report_id,
                    handoff_paths=args.handoff_paths,
                    notes=args.notes,
                    json_output=args.json,
                )
            args._brigade_parser.error(f"unknown phases evidence command: {args.phases_evidence_command}")
            return 2
        if args.phases_command == "verify":
            if args.phases_verify_command == "plan":
                return phases_cmd.verify_plan(target=args.target, selector=args.selector, json_output=args.json)
            if args.phases_verify_command == "record":
                return phases_cmd.verify_record(
                    target=args.target,
                    phase_id=args.phase_id,
                    command=args.verification_command,
                    status=args.status,
                    summary=args.summary,
                    json_output=args.json,
                )
            args._brigade_parser.error(f"unknown phases verify command: {args.phases_verify_command}")
            return 2
        if args.phases_command == "actions":
            if args.phases_actions_command == "plan":
                return phases_cmd.actions_plan(target=args.target, phase_range=args.phase_range, json_output=args.json)
            if args.phases_actions_command == "build":
                return phases_cmd.actions_build(target=args.target, phase_range=args.phase_range, json_output=args.json)
            if args.phases_actions_command == "list":
                return phases_cmd.actions_list(target=args.target, json_output=args.json)
            if args.phases_actions_command == "show":
                return phases_cmd.actions_show(target=args.target, action_id=args.action_id, json_output=args.json)
            if args.phases_actions_command == "start":
                return phases_cmd.actions_start(target=args.target, action_id=args.action_id, json_output=args.json)
            if args.phases_actions_command == "done":
                return phases_cmd.actions_done(target=args.target, action_id=args.action_id, json_output=args.json)
            if args.phases_actions_command == "defer":
                return phases_cmd.actions_defer(
                    target=args.target, action_id=args.action_id, reason=args.reason, json_output=args.json
                )
            if args.phases_actions_command == "archive":
                return phases_cmd.actions_archive(
                    target=args.target, action_id=args.action_id, completed=args.completed, json_output=args.json
                )
            if args.phases_actions_command == "import-issues":
                return phases_cmd.actions_import_issues(target=args.target, dry_run=args.dry_run, json_output=args.json)
            args._brigade_parser.error(f"unknown phases actions command: {args.phases_actions_command}")
            return 2
        if args.phases_command == "goal":
            if args.phases_goal_command == "scaffold":
                return phases_cmd.goal_scaffold(target=args.target, phase_range=args.phase_range, json_output=args.json)
            args._brigade_parser.error(f"unknown phases goal command: {args.phases_goal_command}")
            return 2
        if args.phases_command == "report":
            if args.phases_report_command == "build":
                return phases_cmd.report_build(target=args.target, phase_range=args.phase_range, json_output=args.json)
            if args.phases_report_command == "list":
                return phases_cmd.report_list(target=args.target, limit=args.limit, json_output=args.json)
            if args.phases_report_command == "show":
                return phases_cmd.report_show(target=args.target, report_id=args.report_id, json_output=args.json)
            if args.phases_report_command == "closeout":
                return phases_cmd.report_closeout(
                    target=args.target,
                    report_id=args.report_id,
                    status=args.status,
                    reason=args.reason,
                    json_output=args.json,
                )
            if args.phases_report_command == "compare":
                return phases_cmd.report_compare(target=args.target, report_id=args.report_id, json_output=args.json)
            args._brigade_parser.error(f"unknown phases report command: {args.phases_report_command}")
            return 2
        if args.phases_command == "session":
            if args.phases_session_command == "start":
                return phases_cmd.session_start(
                    target=args.target,
                    phase_range=args.phase_range,
                    source_goal=args.source_goal,
                    json_output=args.json,
                )
            if args.phases_session_command == "list":
                return phases_cmd.session_list(target=args.target, limit=args.limit, json_output=args.json)
            if args.phases_session_command == "show":
                return phases_cmd.session_show(target=args.target, session_id=args.session_id, json_output=args.json)
            if args.phases_session_command == "checkpoint":
                return phases_cmd.session_checkpoint(
                    target=args.target,
                    session_id=args.session_id,
                    phase_id=args.phase_id,
                    status=args.status,
                    summary=args.summary,
                    notes=args.notes,
                    json_output=args.json,
                )
            if args.phases_session_command == "checkpoints":
                if args.phases_session_checkpoints_command == "list":
                    return phases_cmd.session_checkpoint_list(
                        target=args.target, session_id=args.session_id, limit=args.limit, json_output=args.json
                    )
                if args.phases_session_checkpoints_command == "show":
                    return phases_cmd.session_checkpoint_show(
                        target=args.target, checkpoint_id=args.checkpoint_id, json_output=args.json
                    )
                if args.phases_session_checkpoints_command == "compare":
                    return phases_cmd.session_checkpoint_compare(
                        target=args.target, checkpoint_id=args.checkpoint_id, json_output=args.json
                    )
                if args.phases_session_checkpoints_command == "import-issues":
                    return phases_cmd.session_checkpoint_import_issues(
                        target=args.target,
                        checkpoint_id=args.checkpoint_id,
                        dry_run=args.dry_run,
                        json_output=args.json,
                    )
                if args.phases_session_checkpoints_command == "archive":
                    return phases_cmd.session_checkpoint_archive(
                        target=args.target, checkpoint_id=args.checkpoint_id, json_output=args.json
                    )
                args._brigade_parser.error(
                    f"unknown phases session checkpoints command: {args.phases_session_checkpoints_command}"
                )
            if args.phases_session_command == "recovery-note":
                return phases_cmd.session_recovery_note(
                    target=args.target,
                    session_id=args.session_id,
                    phase_id=args.phase_id,
                    summary=args.summary,
                    notes=args.notes,
                    evidence=args.evidence,
                    json_output=args.json,
                )
            if args.phases_session_command == "recovery-notes":
                if args.phases_session_recovery_notes_command == "list":
                    return phases_cmd.session_recovery_note_list(
                        target=args.target, session_id=args.session_id, limit=args.limit, json_output=args.json
                    )
                if args.phases_session_recovery_notes_command == "show":
                    return phases_cmd.session_recovery_note_show(
                        target=args.target, note_id=args.note_id, json_output=args.json
                    )
                if args.phases_session_recovery_notes_command == "closeout":
                    return phases_cmd.session_recovery_note_closeout(
                        target=args.target,
                        note_id=args.note_id,
                        status=args.status,
                        reason=args.reason,
                        json_output=args.json,
                    )
                args._brigade_parser.error(
                    f"unknown phases session recovery notes command: {args.phases_session_recovery_notes_command}"
                )
            if args.phases_session_command == "risk":
                return phases_cmd.session_risk(target=args.target, session_id=args.session_id, json_output=args.json)
            if args.phases_session_command == "verification":
                return phases_cmd.session_verification(
                    target=args.target, session_id=args.session_id, json_output=args.json
                )
            if args.phases_session_command == "privacy":
                return phases_cmd.session_privacy(target=args.target, session_id=args.session_id, json_output=args.json)
            if args.phases_session_command == "handoffs":
                return phases_cmd.session_handoffs(
                    target=args.target, session_id=args.session_id, json_output=args.json
                )
            if args.phases_session_command == "next":
                return phases_cmd.session_next(target=args.target, session_id=args.session_id, json_output=args.json)
            if args.phases_session_command == "protocol":
                return phases_cmd.session_protocol(
                    target=args.target, session_id=args.session_id, json_output=args.json
                )
            if args.phases_session_command == "audit":
                return phases_cmd.session_audit(target=args.target, session_id=args.session_id, json_output=args.json)
            if args.phases_session_command == "resume":
                return phases_cmd.session_resume(target=args.target, session_id=args.session_id, json_output=args.json)
            if args.phases_session_command == "closeout":
                return phases_cmd.session_closeout(
                    target=args.target,
                    session_id=args.session_id,
                    status=args.status,
                    reason=args.reason,
                    json_output=args.json,
                )
            if args.phases_session_command == "activity":
                return phases_cmd.session_activity(
                    target=args.target, session_id=args.session_id, json_output=args.json
                )
            if args.phases_session_command == "progress":
                return phases_cmd.session_progress(
                    target=args.target, session_id=args.session_id, json_output=args.json
                )
            if args.phases_session_command == "import-issues":
                return phases_cmd.session_import_issues(
                    target=args.target, session_id=args.session_id, dry_run=args.dry_run, json_output=args.json
                )
            if args.phases_session_command == "gate":
                return phases_cmd.session_gate(target=args.target, session_id=args.session_id, json_output=args.json)
            if args.phases_session_command == "report":
                if args.phases_session_report_command == "build":
                    return phases_cmd.session_report_build(
                        target=args.target, session_id=args.session_id, json_output=args.json
                    )
                if args.phases_session_report_command == "list":
                    return phases_cmd.session_report_list(target=args.target, limit=args.limit, json_output=args.json)
                if args.phases_session_report_command == "show":
                    return phases_cmd.session_report_show(
                        target=args.target, report_id=args.report_id, json_output=args.json
                    )
                args._brigade_parser.error(
                    f"unknown phases session report command: {args.phases_session_report_command}"
                )
                return 2
            args._brigade_parser.error(f"unknown phases session command: {args.phases_session_command}")
            return 2
        args._brigade_parser.error(f"unknown phases command: {args.phases_command}")
        return 2
    if args.work_command == "next":
        return work_cmd.next(target=args.target, json_output=args.json)
    if args.work_command == "tasks":
        return work_cmd.tasks(target=args.target, all_tasks=args.all, json_output=args.json)
    if args.work_command == "task":
        if args.task_command == "add":
            text = " ".join(args.text) if args.text else None
            return work_cmd.task_add(
                target=args.target,
                text=text,
                from_next=args.from_next,
                from_issue=args.from_issue,
                task_type=args.type,
                priority=args.priority,
                acceptance=args.acceptance,
                template=args.template,
            )
        if args.task_command == "show":
            return work_cmd.task_show(target=args.target, task_id=args.task_id)
        if args.task_command == "plan":
            return work_cmd.task_plan(
                target=args.target,
                task_id=args.task_id,
                json_output=args.json,
                write=args.write,
                title=args.title,
                assumptions=args.assumptions,
                risks=args.risks,
                sources=args.sources,
                next_command=args.next_command,
                accept=args.accept,
                kind="meta" if args.meta else "plan",
                steps=args.step,
                from_research=args.from_research,
            )
        if args.task_command == "done":
            return work_cmd.task_done(target=args.target, task_id=args.task_id)
        args._brigade_parser.error(f"unknown task command: {args.task_command}")
        return 2
    if args.work_command == "import":
        if args.import_command == "add":
            return work_cmd.import_add(
                target=args.target,
                text=" ".join(args.text),
                kind=args.kind,
                source=args.source,
                metadata=args.metadata,
            )
        if args.import_command == "context":
            if args.from_miseledger is not None:
                if args.text or args.from_file is not None:
                    args._brigade_parser.error("--from-miseledger cannot be combined with text or --from-file")
                return work_cmd.import_context_from_miseledger(
                    target=args.target,
                    query=args.from_miseledger,
                    limit=args.limit,
                    json_output=args.json,
                )
            if not args.text and args.from_file is None:
                args._brigade_parser.error("work import context requires text or --from-file")
            return work_cmd.import_context(
                target=args.target,
                text=" ".join(args.text) if args.text else "",
                source=args.source,
                context_kind=args.context_kind,
                from_file=args.from_file,
                max_chars=args.max_chars,
                json_output=args.json,
            )
        if args.import_command == "list":
            return work_cmd.import_list(
                target=args.target,
                all_imports=args.all,
                json_output=args.json,
                limit=args.limit,
                source=args.source,
                kind=args.kind,
                metadata=args.metadata,
            )
        if args.import_command == "validate":
            return work_cmd.import_validate(input_path=args.input_path, json_output=args.json)
        if args.import_command == "ingest":
            return work_cmd.import_ingest(
                target=args.target,
                input_path=args.input_path,
                dry_run=args.dry_run,
                json_output=args.json,
            )
        if args.import_command == "issue-repairs":
            return work_cmd.import_issue_repairs(
                target=args.target,
                dry_run=args.dry_run,
                json_output=args.json,
            )
        if args.import_command == "plan":
            return work_cmd.import_plan(target=args.target, import_id=args.import_id, json_output=args.json)
        if args.import_command == "plan-handoff":
            return work_cmd.import_plan_handoff(target=args.target, import_id=args.import_id, json_output=args.json)
        if args.import_command == "memory-care":
            return work_cmd.import_memory_care(
                target=args.target,
                queue=args.queue,
                dry_run=args.dry_run,
                json_output=args.json,
            )
        if args.import_command == "memory-refresh":
            return work_cmd.import_memory_refresh(
                target=args.target,
                queue=args.queue,
                dry_run=args.dry_run,
                json_output=args.json,
            )
        if args.import_command == "chat-sweep":
            return work_cmd.import_chat_sweep(
                target=args.target,
                input_path=args.input_path,
                dry_run=args.dry_run,
                json_output=args.json,
            )
        if args.import_command == "content-guard":
            return work_cmd.import_content_guard(
                target=args.target,
                scan_target=args.scan_target,
                policy=args.policy,
                dry_run=args.dry_run,
                json_output=args.json,
            )
        if args.import_command == "triage":
            return work_cmd.import_triage(
                target=args.target,
                json_output=args.json,
                limit=args.limit,
                source=args.source,
                kind=args.kind,
                metadata=args.metadata,
            )
        if args.import_command == "provenance":
            return work_cmd.import_provenance(target=args.target, json_output=args.json)
        if args.import_command == "show":
            return work_cmd.import_show(target=args.target, import_id=args.import_id)
        if args.import_command == "promote":
            return work_cmd.import_promote(
                target=args.target,
                import_id=args.import_id,
                all_matching=args.all,
                kind=args.kind,
                source=args.source,
                metadata=args.metadata,
                run_after=args.run,
            )
        if args.import_command == "promote-handoff":
            return work_cmd.import_promote_handoff(
                target=args.target,
                import_id=args.import_id,
                run_after=args.run,
                json_output=args.json,
            )
        if args.import_command == "dismiss":
            return work_cmd.import_dismiss(
                target=args.target,
                import_id=args.import_id,
                reason=args.reason,
                all_matching=args.all,
                kind=args.kind,
                source=args.source,
                metadata=args.metadata,
            )
        args._brigade_parser.error(f"unknown import command: {args.import_command}")
        return 2
    if args.work_command == "list":
        return work_cmd.list_sessions(target=args.target, limit=args.limit)
    if args.work_command == "latest":
        return work_cmd.latest(target=args.target)
    if args.work_command == "show":
        return work_cmd.show(target=args.target, session=args.session)
    if args.work_command == "recap":
        return work_cmd.recap(target=args.target, limit=args.limit, since=args.since)
    if args.work_command == "run":
        task = " ".join(args.task) if args.task else None
        return work_cmd.run(
            task,
            target=args.target,
            title=args.title,
            output_dir=args.output_dir,
            handoff=not args.no_handoff,
            handoff_inbox=args.handoff_inbox,
            dogfood_handoff=args.dogfood_handoff,
            inspect=not args.no_inspect,
            native_read_only_sandbox=args.native_read_only_sandbox,
            timeout_seconds=args.timeout_seconds,
            recap_limit=args.recap_limit,
            queue_next=args.queue_next,
        )
    if args.work_command == "start":
        title = " ".join(args.title) if args.title else None
        return work_cmd.start(target=args.target, title=title, force=args.force)
    if args.work_command == "note":
        return work_cmd.note(target=args.target, text=" ".join(args.text))
    if args.work_command == "end":
        return work_cmd.end(
            target=args.target,
            note=args.note,
            handoff=args.handoff,
            handoff_inbox=args.handoff_inbox,
        )
    args._brigade_parser.error(f"unknown work command: {args.work_command}")
    return 2
