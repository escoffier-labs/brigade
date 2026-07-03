r"""Guard the phases_cmd package facade surface.

The phases_cmd package split relies on the facade re-exporting every name that
external modules and tests reference as ``phases_cmd.X``. This list was derived
from ``phases_cmd.`` references in ``src`` and ``tests`` at split time and is
frozen here so a re-export regression fails loudly.
"""

from brigade import phases_cmd
from brigade.phases_cmd import constants

EXPECTED_FACADE_SYMBOLS = (
    "_actions_root",
    "_latest_checkpoint_for_session",
    "_latest_session",
    "_phase_has_current_closeout",
    "_read_actions",
    "_read_session_checkpoints",
    "_records",
    "_records_root",
    "_resolve_session",
    "_session_checkpoints_root",
    "_session_next_payload",
    "_session_summary",
    "_sessions_root",
    "actions_archive",
    "actions_build",
    "actions_defer",
    "actions_done",
    "actions_import_issues",
    "actions_list",
    "actions_plan",
    "actions_show",
    "actions_start",
    "closeout",
    "compare",
    "complete",
    "defer",
    "doctor",
    "doctor_payload",
    "evidence_add",
    "goal_scaffold",
    "handoff",
    "health",
    "import_issues",
    "init",
    "list_phases",
    "next_phase",
    "plan",
    "privacy",
    "reconcile",
    "report_build",
    "report_closeout",
    "report_compare",
    "report_list",
    "report_show",
    "schema",
    "session_activity",
    "session_audit",
    "session_checkpoint",
    "session_checkpoint_archive",
    "session_checkpoint_compare",
    "session_checkpoint_import_issues",
    "session_checkpoint_list",
    "session_checkpoint_show",
    "session_closeout",
    "session_gate",
    "session_handoffs",
    "session_import_issues",
    "session_list",
    "session_next",
    "session_privacy",
    "session_progress",
    "session_protocol",
    "session_recovery_note",
    "session_recovery_note_closeout",
    "session_recovery_note_list",
    "session_recovery_note_show",
    "session_report_build",
    "session_report_list",
    "session_report_show",
    "session_resume",
    "session_risk",
    "session_show",
    "session_start",
    "session_verification",
    "show",
    "start",
    "status",
    "status_payload",
    "verify_plan",
    "verify_record",
)


def test_facade_exposes_externally_referenced_symbols():
    missing = [name for name in EXPECTED_FACADE_SYMBOLS if not hasattr(phases_cmd, name)]
    assert missing == []


def test_facade_submodules_importable():
    from brigade.phases_cmd import checks_actions, constants, session_lifecycle, session_ops

    for module in (constants, session_lifecycle, session_ops, checks_actions):
        assert module.__name__.startswith("brigade.phases_cmd.")


def test_facade_patch_reaches_cross_family_reference(monkeypatch, tmp_path):
    assert (
        phases_cmd.plan(
            target=tmp_path,
            phase_id="phase-610",
            title="Facade patch propagation",
            source_goal="test",
            json_output=True,
        )
        == 0
    )
    assert phases_cmd.complete(target=tmp_path, phase_id="phase-610", summary="Done", json_output=True) == 0
    assert phases_cmd.report_build(target=tmp_path, json_output=True) == 0
    report = phases_cmd._latest_report(tmp_path)
    assert report is not None
    report["git_head"] = "old-head"

    monkeypatch.setattr(phases_cmd, "_git_head", lambda target: "new-head")
    monkeypatch.setattr(phases_cmd, "_same_commit", lambda expected, current: False)

    payload = constants._report_compare_summary(tmp_path, report)

    assert payload is not None
    assert any(check["name"] == "phase_report_head_changed" for check in payload["checks"])
