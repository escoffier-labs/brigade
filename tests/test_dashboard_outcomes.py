import json
import subprocess
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock

import pytest

from brigade import outcome_ledger
from brigade.center_cmd.dashboard.views import outcome_rank


def test_fetch_uses_outcome_rank_and_ratchet_json(monkeypatch, tmp_path):
    calls: list[tuple[Path, list[str]]] = []

    def fake_run_json(target: Path, args: list[str]) -> dict:
        calls.append((target, args))
        return {"ranking": []} if args == ["outcome", "rank"] else {"version": 1}

    monkeypatch.setattr("brigade.center_cmd.dashboard.data.run_json", fake_run_json)

    payload = outcome_rank.fetch(tmp_path)
    assert payload["ranking"] == []
    assert payload["ratchet"] == {"version": 1}
    assert calls == [(tmp_path, ["outcome", "rank"]), (tmp_path, ["outcome", "ratchet"])]


def test_render_displays_ranking_rows_with_cohort_and_recency_columns():
    payload = {
        "ranking": [
            {
                "artifact_id": "example-skill-one",
                "score": 0.4567,
                "helped": 12,
                "hurt": 3,
                "capability_score": 0.6789,
                "capability_helped": 9,
                "capability_hurt": 2,
                "local_trials": 7,
                "attested_trials": 2,
                "stale_records": 1,
                "legacy_records": 4,
            },
            {
                "artifact_id": "example-skill-two",
                "score": 0.1,
                "helped": 2,
                "hurt": 5,
                "capability_score": 0.25,
                "capability_helped": 1,
                "capability_hurt": 3,
                "local_trials": 1,
                "attested_trials": 0,
                "stale_records": 0,
                "legacy_records": 0,
            },
        ],
        "target": "/fake/worktree",
    }

    fragment = outcome_rank.render(payload, "unused")

    for header in (
        "Artifact",
        "Score",
        "Helped",
        "Hurt",
        "Capability score",
        "Capability helped",
        "Capability hurt",
        "Local trials",
        "Attested trials",
        "Stale records",
        "Legacy records",
    ):
        assert header in fragment
    assert "example-skill-one" in fragment
    assert "example-skill-two" in fragment
    for value in ("0.457", "12", "3", "0.679", "9", "2", "7", "1", "4"):
        assert f">{value}<" in fragment
    assert "0.100" in fragment
    assert "0.250" in fragment
    # Row order follows the payload ranking order.
    assert fragment.index("example-skill-one") < fragment.index("example-skill-two")


def test_summary_strip_renders_before_table_from_ranking_data():
    payload = {
        "ranking": [
            {"artifact_id": "example-skill-one", "helped": 12, "hurt": 3},
            {"artifact_id": "example-skill-two", "helped": 2, "hurt": 5},
        ]
    }

    fragment = outcome_rank.render(payload, "unused")

    assert "biggest net help" in fragment
    assert "biggest net drag" in fragment
    assert "example-skill-one" in fragment.split("<table")[0]
    assert "example-skill-two" in fragment.split("<table")[0]
    assert fragment.index("biggest net help") < fragment.index("<table")
    assert "<style" not in fragment


def test_render_escapes_untrusted_values():
    payload = {
        "ranking": [
            {
                "artifact_id": '<img src=x onerror="alert(1)">',
                "score": 0.5,
                "helped": 1,
                "hurt": 0,
            }
        ]
    }

    fragment = outcome_rank.render(payload, "unused")

    assert "<img" not in fragment
    assert "&lt;img" in fragment
    assert "&quot;alert(1)&quot;" in fragment


def test_render_missing_entry_values_degrade_to_dash():
    fragment = outcome_rank.render({"ranking": [{"artifact_id": "sparse-skill"}]}, "unused")

    assert "sparse-skill" in fragment
    assert ">-<" in fragment


def test_render_error_payload_renders_error_panel_only():
    fragment = outcome_rank.render({"error": "boom"}, "unused")

    assert "boom" in fragment
    assert 'class="error"' in fragment
    assert "<table" not in fragment


def test_render_empty_payloads_render_not_being_fed_panel():
    for payload in ({}, {"ranking": []}, {"ranking": None}, {"ranking": "not-a-list"}):
        fragment = outcome_rank.render(payload, "unused")
        assert "ranking is empty" in fragment
        assert "outcome loop is not being fed" in fragment
        assert "<table" not in fragment
        assert "biggest net help" not in fragment


def test_render_ratchet_panel_follows_ranking_table():
    payload = {
        "ranking": [{"artifact_id": "example-skill", "helped": 3, "hurt": 0}],
        "ratchet": {
            "version": 1,
            "generated_at": "2026-11-17T07:30:00+00:00",
            "apply": False,
            "summary": {"decisions": 5, "regression_holds": 2, "regression_rollbacks": 0},
            "decisions_by_action": {"bump": 1, "hold": 4},
            "ineligibility_rate": 0.25,
            "leading_ineligibility_reason": "unattributed",
            "regressions": [
                {
                    "artifact_id": "skill-a",
                    "prior_state": "candidate",
                    "reason_code": "regression_hold",
                    "reason": "withheld: verified regression present",
                    "helped": 1,
                    "hurt": 2,
                },
                {
                    "artifact_id": "skill-b",
                    "prior_state": "promoted",
                    "reason_code": "regression_rollback",
                    "reason": "verified regression measured",
                    "helped": 0,
                    "hurt": 3,
                },
            ],
        },
    }

    fragment = outcome_rank.render(payload, "unused")

    assert fragment.index("example-skill</td>") < fragment.index("Outcome ratchet report")
    assert "generated_at 2026-11-17T07:30:00+00:00" in fragment
    assert "mode=dry-run" in fragment
    assert "regression holds 2" in fragment
    assert "ineligibility_rate 0.250" in fragment
    assert "leading reason unattributed" in fragment
    assert "skill-a" in fragment
    assert "skill-b" in fragment
    assert "regression_rollback" in fragment
    assert "verified regression measured" in fragment


def test_render_ratchet_panel_marks_an_applied_run():
    payload = {
        "ranking": [],
        "ratchet": {
            "version": 1,
            "generated_at": "2026-11-17T07:30:00+00:00",
            "apply": True,
            "summary": {"decisions": 1, "regression_holds": 0, "regression_rollbacks": 1},
            "regressions": [],
        },
    }

    fragment = outcome_rank.render(payload, "unused")

    assert "mode=applied" in fragment
    assert "mode=dry-run" not in fragment


def test_render_ratchet_panel_marks_a_stale_report():
    payload = {
        "ranking": [],
        "ratchet": {
            "version": 1,
            "generated_at": "2020-01-01T00:00:00+00:00",
            "summary": {"decisions": 1, "regression_holds": 0, "regression_rollbacks": 0},
            "regressions": [],
        },
    }

    fragment = outcome_rank.render(payload, "unused")

    assert "stale," in fragment
    assert "No regression holds or rollbacks in the last run." in fragment


def test_render_ratchet_panel_absent_and_error_states():
    absent = outcome_rank.render({"ranking": []}, "unused")
    assert "No outcome ratchet report yet" in absent
    assert "outcome reconcile --target . --report --proposals" in absent

    failed = outcome_rank.render({"ranking": [], "ratchet": {"error": "ratchet boom"}}, "unused")
    assert "ratchet boom" in failed
    assert 'class="error"' in failed

    # A broken report must not hide the ranking table.
    assert "example-skill" in outcome_rank.render(
        {"ranking": [{"artifact_id": "example-skill"}], "ratchet": {"error": "boom"}}, "unused"
    )


def test_render_escapes_ratchet_values():
    payload = {
        "ranking": [],
        "ratchet": {
            "version": 1,
            "generated_at": '<img src=x onerror="alert(1)">',
            "summary": {"decisions": 1, "regression_holds": 1, "regression_rollbacks": 0},
            "regressions": [
                {
                    "artifact_id": "<script>bad</script>",
                    "prior_state": "candidate",
                    "reason_code": "regression_hold",
                    "reason": "<b>boom</b>",
                    "helped": 0,
                    "hurt": 1,
                }
            ],
        },
    }

    fragment = outcome_rank.render(payload, "unused")

    assert "<script>" not in fragment
    assert "<b>boom</b>" not in fragment
    assert "&lt;script&gt;" in fragment
    assert "&lt;b&gt;boom&lt;/b&gt;" in fragment


def test_render_ratchet_panel_shows_the_stale_pending_count(monkeypatch):
    generated_at = "2026-11-17T07:30:00+00:00"
    clock = Mock(wraps=datetime)
    clock.now.return_value = datetime.fromisoformat(generated_at)
    monkeypatch.setattr(outcome_rank, "datetime", clock)
    base = {"version": 1, "generated_at": generated_at, "regressions": []}
    stale = outcome_rank.render(
        {
            "ranking": [],
            "ratchet": {
                **base,
                "summary": {"decisions": 1, "pending_physical_actions": 2, "stale_physical_actions": 5},
            },
        },
        "unused",
    )
    assert "pending physical actions 2 (stale 5)" in stale

    quiet = outcome_rank.render(
        {
            "ranking": [],
            "ratchet": {
                **base,
                "summary": {"decisions": 1, "pending_physical_actions": 2, "stale_physical_actions": 0},
            },
        },
        "unused",
    )
    assert "pending physical actions 2;" in quiet
    assert "(stale" not in quiet


def test_render_ratchet_panel_shows_the_inbox_proposal_counts():
    base = {
        "version": 1,
        "generated_at": "2026-11-17T07:30:00+00:00",
        "apply": False,
        "regressions": [],
    }
    filed = outcome_rank.render(
        {
            "ranking": [],
            "ratchet": {
                **base,
                "summary": {
                    "decisions": 3,
                    "outcome_proposals": {"filed": 2, "existing": 1, "flagged": 4, "skipped": 0},
                },
            },
        },
        "unused",
    )
    assert "inbox proposals filed 2, existing 1, flagged 4, skipped 0" in filed

    plain = outcome_rank.render({"ranking": [], "ratchet": {**base, "summary": {"decisions": 3}}}, "unused")
    assert "inbox proposals not requested" in plain


def test_render_ratchet_panel_labels_report_file_trial_counts():
    payload = {
        "ranking": [],
        "ratchet": {
            "version": 1,
            "generated_at": "2026-11-17T07:30:00+00:00",
            "apply": False,
            "summary": {"decisions": 1, "local_trials": 3, "attested_trials": 9},
            "regressions": [
                {"artifact_id": "skill-a", "helped": 1, "hurt": 2, "local_trials": 3, "attested_trials": 9}
            ],
        },
    }

    fragment = outcome_rank.render(payload, "unused")

    assert "trials local 3 attested 9 (from report file)" in fragment
    assert "Reported local trials" in fragment
    assert "Reported attested trials" in fragment

    payload["ratchet"]["summary"] = {"decisions": 1}
    payload["ratchet"]["regressions"] = [{"artifact_id": "skill-a"}]
    fragment = outcome_rank.render(payload, "unused")

    assert "local not recorded attested not recorded (from report file)" in fragment
    assert ">not recorded<" in fragment


_STAMP = "2026-11-17T07:30:00Z"
_MAIN = "a" * 40
_RESTORE = "b" * 40


@pytest.fixture
def ledger_repo(tmp_path, monkeypatch):
    def git(*args):
        subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True)

    git("init", "-q")
    git("config", "user.name", "Example")
    git("config", "user.email", "example@example.invalid")
    git("commit", "--allow-empty", "-qm", "initial")
    monkeypatch.setattr(outcome_rank.data, "run_json", lambda *_: {"ranking": []})

    def commit_ledger(raw, *, with_slug=False):
        ledger_file = tmp_path / outcome_ledger.LEDGER_PATH
        ledger_file.parent.mkdir()
        ledger_file.write_bytes(raw)
        git("add", outcome_ledger.LEDGER_PATH)
        git("commit", "-qm", "ledger")
        if with_slug:
            git("remote", "add", "origin", "https://github.com/example/project.git")
        return ledger_file

    return tmp_path, commit_ledger


def _ledger_bytes(subjects):
    return json.dumps(
        {
            "schema": outcome_ledger.SCHEMA,
            "prev_ledger_sha256": "0" * 64,
            "covers": {"cutoff_at": _STAMP, "main_sha": _MAIN},
            "policy": {},
            "subjects": subjects,
        }
    ).encode()


def _subject(action, code, status, band, outcome, restore):
    return {
        "manifest_id": "example-manifest",
        "subject_path": "skills/example/SKILL.md",
        "since_commit": _MAIN,
        "approved": [{}],
        "demoted": [],
        "status": status,
        "band": band,
        "decision": {"action": action, "code": code, "decided_at": _STAMP, "reason": "<reviewed>"},
        "restore": restore,
        "trials": [
            {
                "sha": _MAIN,
                "outcome": outcome,
                "reason": "<measured>",
                "jobs": [{"run_id": 101, "job_id": 202, "attempt": 1, "conclusion": "success", "completed_at": _STAMP}],
            }
        ],
    }


def test_missing_ci_ledger_ignores_uncommitted_copy(ledger_repo):
    target, _ = ledger_repo
    ledger_file = target / outcome_ledger.LEDGER_PATH
    ledger_file.parent.mkdir()
    ledger_file.write_bytes(_ledger_bytes({}))

    fragment = outcome_rank.render(outcome_rank.fetch(target), "unused")

    assert "No CI outcome ledger in this repository" in fragment
    assert 'class="error"' not in fragment
    assert "Outcome ratchet report" in fragment


def test_empty_ci_ledger_displays_coverage(ledger_repo):
    target, commit_ledger = ledger_repo
    commit_ledger(_ledger_bytes({}))

    fragment = outcome_rank.render(outcome_rank.fetch(target), "unused")

    assert "No skill decisions in the CI outcome ledger" in fragment
    assert _STAMP in fragment
    assert _MAIN[:12] in fragment


@pytest.mark.parametrize("raw", [b"not JSON", _ledger_bytes({"skill:broken": {}})])
def test_malformed_ci_ledger_preserves_other_panels(ledger_repo, raw):
    target, commit_ledger = ledger_repo
    commit_ledger(raw)

    fragment = outcome_rank.render(outcome_rank.fetch(target), "unused")

    assert "CI outcome ledger is unreadable" in fragment
    assert "ranking is empty" in fragment
    assert "Outcome ratchet report" in fragment


@pytest.mark.parametrize("with_slug", [True, False])
def test_ci_ledger_renders_install_rollback_and_attest_jobs(ledger_repo, with_slug):
    target, commit_ledger = ledger_repo
    raw = _ledger_bytes(
        {
            "skill:alpha": _subject("install", "qualifying", "promoted", "promoted", "helped", None),
            "skill:beta": _subject(
                "rollback",
                "regression_restore",
                "candidate",
                "unseen",
                "hurt",
                {"from_commit": _MAIN, "to_commit": _RESTORE},
            ),
        }
    )
    commit_ledger(raw, with_slug=with_slug).write_bytes(b"dirty working tree")

    fragment = outcome_rank.render(outcome_rank.fetch(target), "unused")

    for value in (
        "alpha",
        "beta",
        "promoted",
        "candidate",
        "unseen",
        "install",
        "qualifying",
        "rollback",
        "regression_restore",
        _STAMP,
        _MAIN[:12],
        _RESTORE[:12],
        "&lt;reviewed&gt;",
        "helped",
        "hurt",
        "&lt;measured&gt;",
        "success",
        "Run 101, job 202",
    ):
        assert value in fragment
    assert "<reviewed>" not in fragment
    assert "<measured>" not in fragment
    link = 'href="https://github.com/example/project/actions/runs/101/job/202"'
    assert (link in fragment) == with_slug


def test_ci_ledger_omits_null_decision_code(ledger_repo):
    target, commit_ledger = ledger_repo
    subject = _subject("install", None, "promoted", "promoted", "helped", None)
    commit_ledger(_ledger_bytes({"skill:alpha": subject}))

    fragment = outcome_rank.render(outcome_rank.fetch(target), "unused")

    assert f"<strong>install</strong> · {_STAMP}" in fragment


def test_ci_ledger_rejects_oversized_blob_before_reading(ledger_repo, monkeypatch):
    target, commit_ledger = ledger_repo
    commit_ledger(_ledger_bytes({}).ljust(1024 * 1024 + 1, b" "))

    def unexpected_read(*args, **kwargs):
        pytest.fail("oversized ledger blob was read")

    monkeypatch.setattr(outcome_rank.Repo, "show", unexpected_read)

    fragment = outcome_rank.render(outcome_rank.fetch(target), "unused")

    assert "CI outcome ledger is unreadable" in fragment


def test_ci_ledger_rejects_committed_symlink(ledger_repo):
    target, _ = ledger_repo

    def git(*args, **kwargs):
        return subprocess.run(["git", "-C", str(target), *args], check=True, capture_output=True, **kwargs)

    # Stage symlink mode directly so this test works without OS symlink privileges.
    blob = git("hash-object", "-w", "--stdin", input=_ledger_bytes({})).stdout.decode().strip()
    git("update-index", "--add", "--cacheinfo", "120000", blob, outcome_ledger.LEDGER_PATH)
    git("commit", "-qm", "symlink ledger")

    fragment = outcome_rank.render(outcome_rank.fetch(target), "unused")

    assert "CI outcome ledger is unreadable" in fragment


def test_unborn_head_has_no_ci_ledger(tmp_path, monkeypatch):
    subprocess.run(["git", "-C", str(tmp_path), "init", "-q"], check=True, capture_output=True)
    monkeypatch.setattr(outcome_rank.data, "run_json", lambda *_: {"ranking": []})

    fragment = outcome_rank.render(outcome_rank.fetch(tmp_path), "unused")

    assert "No CI outcome ledger in this repository" in fragment
    assert 'class="error"' not in fragment


def test_unreadable_ci_ledger_preserves_other_panels(ledger_repo, monkeypatch):
    target, commit_ledger = ledger_repo
    commit_ledger(_ledger_bytes({}))

    def unreadable(*args, **kwargs):
        raise outcome_rank.GitError("cannot read committed blob")

    monkeypatch.setattr(outcome_rank.Repo, "show", unreadable)

    fragment = outcome_rank.render(outcome_rank.fetch(target), "unused")

    assert "CI outcome ledger is unreadable" in fragment
    assert "Outcome ratchet report" in fragment


def test_ci_ledger_with_malformed_trial_preserves_other_panels(ledger_repo):
    target, commit_ledger = ledger_repo
    subject = _subject("install", "qualifying", "promoted", "promoted", "helped", None)
    subject["trials"] = [None]
    commit_ledger(_ledger_bytes({"skill:alpha": subject}))

    fragment = outcome_rank.render(outcome_rank.fetch(target), "unused")

    assert "CI outcome ledger is unreadable" in fragment
    assert "Outcome ratchet report" in fragment
