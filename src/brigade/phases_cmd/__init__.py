"""Phase ledger command package facade.

Re-exports the flat ``phases_cmd.X`` surface while implementation lives in
family submodules. The module setattr bridge keeps legacy facade-level
monkeypatches working by forwarding patched symbols to owning submodules.
"""

# ruff: noqa: F401
from __future__ import annotations

import sys
from types import ModuleType
from typing import Any

from . import checks_actions
from . import constants
from . import session_lifecycle
from . import session_ops

from .constants import (
    DONE_STATUSES,
    PHASE_ACTION_STATUSES,
    PHASE_CLOSEOUT_STATUSES,
    PHASE_REPORT_CLOSEOUT_STATUSES,
    PHASE_SESSION_CLOSEOUT_STATUSES,
    PHASE_STATUSES,
    PHASE_VERIFY_STATUSES,
    PRIVACY_PATTERNS,
    REPORT_STALE_HOURS,
    SCHEMA_VERSION,
    STALE_IN_PROGRESS_HOURS,
    STALE_UNREVIEWED_COMPLETED_HOURS,
    _actions_root,
    _append_jsonl,
    _append_unique,
    _checkpoint_summary,
    _closeouts_root,
    _contract_schema,
    _default_record,
    _find_record,
    _goals_root,
    _index_path,
    _latest_checkpoint_for_session,
    _latest_record,
    _latest_report,
    _latest_report_for_range,
    _latest_session,
    _latest_session_for_range,
    _parse_range,
    _parse_time,
    _phase_id_for,
    _read_actions,
    _read_closeouts,
    _read_reports,
    _read_session_checkpoints,
    _read_session_recovery_notes,
    _read_session_reports,
    _read_sessions,
    _record_path,
    _record_summary,
    _records,
    _records_root,
    _recovery_note_summary,
    _report_compare_summary,
    _reports_root,
    _resolve_report,
    _resolve_session,
    _resolve_session_checkpoint,
    _resolve_session_recovery_note,
    _root,
    _safe_phase_number,
    _schema,
    _selected_records,
    _session_checkpoints_archive_path,
    _session_checkpoints_root,
    _session_phase_records,
    _session_recovery_notes_root,
    _session_reports_root,
    _session_summary,
    _sessions_root,
    _slug,
    _source_fingerprint,
    _status_counts,
    complete,
    defer,
    init,
    list_phases,
    next_phase,
    plan,
    schema,
    show,
    start,
    status,
    status_payload,
)
from .session_lifecycle import (
    _checkpoint_issue_import_records,
    _checkpoint_state_for_session_next,
    _latest_phase_handoff,
    _latest_privacy_check,
    _record_has_handoff_deferral,
    _session_checkpoint_compare_payload,
    _session_handoffs_payload,
    _session_payload,
    _session_privacy_payload,
    _session_risk_payload,
    _session_verification_payload,
    session_checkpoint,
    session_checkpoint_compare,
    session_checkpoint_import_issues,
    session_checkpoint_list,
    session_checkpoint_show,
    session_closeout,
    session_handoffs,
    session_list,
    session_privacy,
    session_recovery_note,
    session_recovery_note_closeout,
    session_recovery_note_list,
    session_recovery_note_show,
    session_risk,
    session_show,
    session_start,
    session_verification,
)
from .session_ops import (
    _activity_event,
    _gate_check,
    _goal_scaffold_markdown,
    _latest_session_report_for_session,
    _record_has_clean_privacy,
    _record_has_linted_handoff,
    _resolve_session_report,
    _session_activity_payload,
    _session_audit_payload,
    _session_blocker_fingerprint,
    _session_blocker_import_candidates,
    _session_gate_payload,
    _session_import_summaries,
    _session_next_payload,
    _session_progress_payload,
    _session_protocol_payload,
    _session_report_payload,
    _write_session_report_markdown,
    goal_scaffold,
    session_activity,
    session_audit,
    session_checkpoint_archive,
    session_gate,
    session_import_issues,
    session_next,
    session_progress,
    session_protocol,
    session_report_build,
    session_report_list,
    session_report_show,
    session_resume,
)
from .checks_actions import (
    _action_source_fingerprint,
    _action_summary,
    _actions_update_status,
    _check,
    _find_action,
    _git_added_text_for_file,
    _git_commit_exists,
    _git_commit_on_branch,
    _git_dirty_paths,
    _git_head,
    _handoff_root,
    _phase_action_candidates,
    _phase_handoff_content,
    _phase_has_current_closeout,
    _privacy_findings_for_text,
    _report_payload,
    _safe_handoff_text,
    _same_commit,
    _set_action_status,
    _verification_entries,
    _write_report_markdown,
    actions_archive,
    actions_build,
    actions_defer,
    actions_done,
    actions_import_issues,
    actions_list,
    actions_plan,
    actions_show,
    actions_start,
    closeout,
    compare,
    doctor,
    doctor_payload,
    evidence_add,
    handoff,
    health,
    import_issues,
    privacy,
    reconcile,
    report_build,
    report_closeout,
    report_compare,
    report_list,
    report_show,
    verify_plan,
    verify_record,
)

_MODULES = (constants, session_lifecycle, session_ops, checks_actions)


class _PhasesFacade(ModuleType):
    def __setattr__(self, name: str, value: Any) -> None:
        super().__setattr__(name, value)
        for module in _MODULES:
            if hasattr(module, name):
                setattr(module, name, value)


sys.modules[__name__].__class__ = _PhasesFacade

for _module in _MODULES:
    for _name in getattr(_module, "__all__", ()):
        if not _name.startswith("__"):
            globals()[_name] = getattr(_module, _name)

__all__ = tuple(name for name in globals() if not name.startswith("__"))
