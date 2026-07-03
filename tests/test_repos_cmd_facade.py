r"""Guard the repos_cmd package facade surface.

The repos_cmd package split relies on the facade re-exporting every name that
external modules and tests reference as ``repos_cmd.X``. This list was derived
from ``repos_cmd.`` references in ``src`` and ``tests`` at split time and is
frozen here so a re-export regression fails loudly.
"""

from brigade import repos_cmd

EXPECTED_FACADE_SYMBOLS = (
    "BACKLOG_STALE_DAYS",
    "RELEASE_EVIDENCE_STATUSES",
    "RELEASE_EVIDENCE_STEPS",
    "RELEASE_WAIVER_SCOPES",
    "REQUIRED_RELEASE_EVIDENCE_STEPS",
    "RepoEntry",
    "SweepCommand",
    "_find_action",
    "_now",
    "_read_actions",
    "_read_release_actions",
    "_repo_summaries",
    "_repo_summary",
    "_write_actions",
    "actions_archive_completed",
    "actions_build",
    "actions_context_build",
    "actions_context_plan",
    "actions_defer",
    "actions_dispatch_apply",
    "actions_dispatch_plan",
    "actions_dispatch_report",
    "actions_done",
    "actions_health",
    "actions_list",
    "actions_plan",
    "actions_reconcile",
    "actions_show",
    "actions_start",
    "config_path",
    "daily_use_health",
    "discover_plan",
    "doctor",
    "first_run_plan",
    "health",
    "health_commands",
    "import_issues",
    "ingest_fleet",
    "init",
    "latest_release_train",
    "list_repos",
    "rearm",
    "release_actions_archive_completed",
    "release_actions_build",
    "release_actions_defer",
    "release_actions_done",
    "release_actions_list",
    "release_actions_plan",
    "release_actions_show",
    "release_actions_start",
    "release_activity",
    "release_archive",
    "release_audit",
    "release_build",
    "release_checklist",
    "release_closeout",
    "release_compare",
    "release_evidence_list",
    "release_evidence_plan",
    "release_evidence_record",
    "release_evidence_show",
    "release_hygiene",
    "release_import_issues",
    "release_list",
    "release_manifest",
    "release_matrix",
    "release_plan",
    "release_ready",
    "release_reconcile",
    "release_report",
    "release_show",
    "release_summary",
    "release_train_health",
    "release_waiver_doctor",
    "release_waiver_import_issues",
    "release_waiver_list",
    "release_waiver_record",
    "release_waiver_renew",
    "release_waiver_revoke",
    "release_waiver_show",
    "release_waiver_templates",
    "report_archive",
    "report_build",
    "report_closeout",
    "report_list",
    "report_plan",
    "report_show",
    "scan",
    "show",
    "sweep_closeout",
    "sweep_plan",
    "sweep_run",
    "sweep_runs",
    "sweep_show",
)


def test_facade_exposes_externally_referenced_symbols():
    missing = [name for name in EXPECTED_FACADE_SYMBOLS if not hasattr(repos_cmd, name)]
    assert missing == []


def test_facade_submodules_importable():
    from brigade.repos_cmd import actions_dispatch, constants, fleet, fleet_health, release_ops, release_train, sweeps

    for module in (constants, fleet, sweeps, actions_dispatch, release_train, release_ops, fleet_health):
        assert module.__name__.startswith("brigade.repos_cmd.")
