"""Center Research view: registration, state mapping, schema gating, rendering."""

from __future__ import annotations

from pathlib import Path

import pytest

from brigade.center_cmd.dashboard import data
from brigade.center_cmd.dashboard.views import render_nav, research, view_by_name


def _status_payload(runs: list[dict]) -> dict:
    return {"schema": "brigade.research.status.v1", "schema_version": 1, "runs": runs}


def _doctor_payload(**overrides) -> dict:
    payload = {
        "schema": "brigade.research.doctor.v1",
        "schema_version": 1,
        "status": "ok",
        "profile": "grounded",
        "lanes": [
            {
                "capability": "synthesis",
                "seat": "gemini-browser",
                "cli": "oracle",
                "status": "ok",
                "auth_status": "authenticated",
                "requested_model": "gemini-web",
                "model_attestation": "unverified",
                "detail": "ready",
                "fallback_seat": "luna",
                "fallback_status": "ok",
            },
            {
                "capability": "review",
                "seat": "luna",
                "cli": "luna",
                "status": "warn",
                "auth_status": "unknown",
                "requested_model": None,
                "model_attestation": "unverified",
                "detail": "probe skipped",
            },
        ],
    }
    payload.update(overrides)
    return payload


def _show_payload(**overrides) -> dict:
    payload = {
        "schema": "brigade.research.show.v2",
        "schema_version": 2,
        "run": {
            "run_id": "run-1",
            "question": "How do receipts work?",
            "status": "completed",
            "profile": "grounded",
            "current_phase": "publishing",
            "phases": {name: {"status": "completed"} for name in research.RESEARCH_PHASES},
            "synthesis": {"seat": "gemini-browser", "observed_model": "unverified"},
            "fallbacks": [
                {
                    "phase": "synthesis",
                    "from_seat": "gemini-browser",
                    "to_seat": "luna",
                    "failure_kind": "browser-auth",
                }
            ],
            "blockers": [],
            "legacy": False,
        },
        "research": None,
        "findings": {"findings": [{"title": "Receipts are journaled", "trust": "local", "summary": "s"}]},
        "citation_audit": {
            "accepted": False,
            "citations": [{"token": "[source:src-1]", "source_ids": ["src-1"], "status": "resolved"}],
            "unresolved": ["[source:src-missing]"],
        },
        "sources": [
            {
                "source_id": "src-1",
                "origin": "web",
                "provider": "fetcher",
                "uri": "https://example.com",
                "trust": "web",
                "observed_model": None,
                "excerpt": "excerpt text",
                "excerpt_truncated": True,
                "content_chars": 999,
            }
        ],
        "sources_verification": "verified",
        "report": {
            "artifact": "report.md",
            "digest": "a" * 64,
            "verification": "verified",
            "content": "# Report body",
            "content_chars": 13,
            "truncated": False,
        },
        "artifact_verification": {"report_md": "verified"},
    }
    payload.update(overrides)
    return payload


def test_research_view_is_registered_in_navigation() -> None:
    module = view_by_name("research")
    assert module is research
    nav = render_nav("research")
    assert "/view/research" in nav
    assert 'aria-current="page"' in nav
    assert "Research" in nav


@pytest.mark.parametrize(
    ("rec", "expected"),
    [
        ({"status": "running", "blockers": []}, "active"),
        ({"status": "planning"}, "active"),
        ({"status": "running", "blockers": ["waiting on auth"]}, "blocked"),
        ({"status": "completed"}, "completed"),
        ({"status": "done"}, "completed"),
        ({"status": "failed"}, "failed"),
        ({"status": "error"}, "failed"),
        ({"status": "timeout"}, "failed"),
        ({"status": "incomplete"}, "failed"),
        ({"status": "cancelled", "blockers": ["stale"]}, "cancelled"),
        ({"status": "FAILED "}, "failed"),
    ],
)
def test_run_tile_state_mapping(rec: dict, expected: str) -> None:
    assert research.run_tile_state(rec) == expected


def test_run_tile_state_ignores_error_text() -> None:
    rec = {"status": "running", "error": "timeout while dialing provider", "blockers": []}
    assert research.run_tile_state(rec) == "active"


def test_fetch_uses_only_versioned_cli_contracts(monkeypatch, tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def fake_run_json(target, args, **kwargs):
        calls.append(list(args))
        return {}

    monkeypatch.setattr(data, "run_json", fake_run_json)
    payload = research.fetch(tmp_path, query={"run": "run-1"})
    assert ["research", "status", "--all"] in calls
    assert ["research", "doctor"] in calls
    assert ["research", "show", "run-1"] in calls
    assert payload["selected_run"] == "run-1"


@pytest.mark.parametrize(
    "run_id",
    ["../escape<script>", "--target", "--help", ".", "..", "-dash"],
)
def test_fetch_rejects_malformed_run_ids(monkeypatch, tmp_path: Path, run_id: str) -> None:
    calls: list[list[str]] = []

    def fake_run_json(target, args, **kwargs):
        calls.append(list(args))
        return {}

    monkeypatch.setattr(data, "run_json", fake_run_json)
    payload = research.fetch(tmp_path, query={"run": run_id})
    assert payload["selected_run"] == ""
    assert all(args[:2] != ["research", "show"] for args in calls)


def test_render_unsupported_status_schema_shows_bounded_panel() -> None:
    page = research.render(
        {
            "status": {"schema": "brigade.research.status.v99", "runs": []},
            "doctor": _doctor_payload(),
            "selected_run": "",
        },
        "nonce",
    )
    assert "unsupported schema: brigade.research.status.v99" in page
    assert "brigade.research.status.v1" in page
    assert "Provider health" in page


def test_render_command_failures_degrade_per_section() -> None:
    page = research.render(
        {
            "status": {"error": "command timed out"},
            "doctor": {"error": "research doctor failed"},
            "selected_run": "",
        },
        "nonce",
    )
    assert "command timed out" in page
    assert "research doctor failed" in page
    assert "Run inspector" in page


def test_render_empty_workspace_has_plain_empty_state() -> None:
    page = research.render(
        {"status": _status_payload([]), "doctor": _doctor_payload(), "selected_run": ""},
        "nonce",
    )
    assert "No research runs are on file yet." in page
    assert "No research runs yet." in page


def test_render_counts_and_pipeline_for_active_and_blocked_runs() -> None:
    runs = [
        {
            "run_id": "run-a",
            "question": "Active question",
            "status": "running",
            "current_phase": "extraction",
            "profile": "grounded",
            "phases": {
                "planning": {"status": "completed"},
                "discovery": {"status": "completed"},
                "extraction": {"status": "running"},
            },
            "blockers": [],
            "created_at": "2026-08-17T00:00:00+00:00",
        },
        {
            "run_id": "run-b",
            "question": "Blocked question",
            "status": "running",
            "blockers": ["browser auth expired"],
        },
        {"run_id": "run-c", "question": "Old cancelled", "status": "cancelled"},
    ]
    page = research.render(
        {"status": _status_payload(runs), "doctor": _doctor_payload(), "selected_run": ""},
        "nonce",
    )
    assert "Researching 2 questions right now (1 blocked on operator attention)" in page
    for phase in research.RESEARCH_PHASES:
        assert f">{phase}</li>" in page
    assert page.count('<ol class="rs-pipeline">') == 1
    assert "No phase map recorded for this run." in page
    assert "browser auth expired" in page
    assert "cancelled" in page
    assert "rs-state-neutral" in page
    assert "?run=run-a" in page


def test_render_provider_health_shows_fallback_and_unverified_model() -> None:
    page = research.render(
        {"status": _status_payload([]), "doctor": _doctor_payload(status="warn"), "selected_run": ""},
        "nonce",
    )
    assert "gemini-browser" in page
    assert "luna (ok)" in page
    assert "gemini-web (unverified)" in page
    assert "Providers are degraded but usable." in page


def test_render_recent_list_labels_legacy_missing_audit_and_fallback() -> None:
    runs = [
        {
            "run_id": "done-1",
            "question": "Completed with fallback",
            "status": "completed",
            "profile": "grounded",
            "current_phase": "publishing",
            "phases": {"review": {"status": "completed", "accepted": True}},
            "synthesis": {"seat": "gemini-browser", "observed_model": "unverified"},
            "fallbacks": [{"from_seat": "gemini-browser", "to_seat": "luna", "phase": "synthesis"}],
            "created_at": "2026-08-16T00:00:00+00:00",
        },
        {
            "run_id": "done-2",
            "question": "Completed without audit",
            "status": "completed",
            "phases": {},
            "fallbacks": [],
        },
        {"run_id": "old-1", "question": "Legacy run", "status": "done", "legacy": True},
    ]
    page = research.render(
        {"status": _status_payload(runs), "doctor": _doctor_payload(), "selected_run": ""},
        "nonce",
    )
    assert "gemini-browser -&gt; luna" in page
    assert "accepted" in page
    assert "no audit recorded" in page
    assert "legacy run" in page
    assert "gemini-browser (model unverified)" in page
    assert ">legacy<" in page


def test_render_escapes_malicious_text_everywhere() -> None:
    evil = "<script>alert(1)</script>"
    runs = [
        {
            "run_id": "evil-run",
            "question": evil,
            "status": "running",
            "blockers": [evil],
            "profile": evil,
        }
    ]
    doctor = _doctor_payload()
    doctor["lanes"][0]["detail"] = evil
    show = _show_payload()
    show["run"]["question"] = evil
    show["findings"]["findings"][0]["title"] = evil
    show["sources"][0]["uri"] = evil
    show["sources"][0]["excerpt"] = evil
    show["report"]["content"] = evil
    page = research.render(
        {"status": _status_payload(runs), "doctor": doctor, "selected_run": "evil-run", "show": show},
        "nonce",
    )
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page


def test_inspector_renders_summary_findings_sources_citations_report() -> None:
    page = research.render(
        {
            "status": _status_payload([]),
            "doctor": _doctor_payload(),
            "selected_run": "run-1",
            "show": _show_payload(),
        },
        "nonce",
    )
    assert "Run inspector: How do receipts work?" in page
    assert "Receipts are journaled" in page
    assert "https://example.com" in page
    assert "excerpt text" in page
    assert "<strong>1</strong> unresolved" in page
    assert "[source:src-missing]" in page
    assert "gemini-browser -&gt; luna" in page
    assert "browser-auth" in page
    assert "# Report body" in page
    assert "a" * 64 in page
    assert "Receipt and artifact references" in page


def test_inspector_accepts_show_v1_as_degraded_panel() -> None:
    show = _show_payload(schema="brigade.research.show.v1", schema_version=1)
    show.pop("sources", None)
    show.pop("sources_verification", None)
    show.pop("report", None)
    show.pop("artifact_verification", None)
    page = research.render(
        {
            "status": _status_payload([]),
            "doctor": _doctor_payload(),
            "selected_run": "run-1",
            "show": show,
        },
        "nonce",
    )
    assert "brigade.research.show.v1" in page
    assert "show.v2" in page
    assert "Receipts are journaled" in page
    assert "excerpt text" not in page
    assert "# Report body" not in page


def test_inspector_rejects_unsupported_show_schema() -> None:
    show = _show_payload(schema="brigade.research.show.v99", schema_version=99)
    page = research.render(
        {
            "status": _status_payload([]),
            "doctor": _doctor_payload(),
            "selected_run": "run-1",
            "show": show,
        },
        "nonce",
    )
    assert "unsupported schema: brigade.research.show.v99" in page
    assert "Receipts are journaled" not in page


def test_inspector_labels_missing_artifacts_and_withholds_report() -> None:
    show = _show_payload(
        sources=None,
        sources_verification="missing",
        report={
            "artifact": "report.md",
            "digest": None,
            "verification": "digest-mismatch",
            "content": None,
            "content_chars": 0,
            "truncated": False,
        },
        citation_audit=None,
    )
    page = research.render(
        {
            "status": _status_payload([]),
            "doctor": _doctor_payload(),
            "selected_run": "run-1",
            "show": show,
        },
        "nonce",
    )
    assert "No sources have been persisted for this run." in page
    assert "digest-mismatch" in page
    assert "Report content is withheld until the digest verifies." in page
    assert "No citation audit recorded for this run." in page


def test_inspector_labels_tampered_sources_distinct_from_unverified() -> None:
    show = _show_payload(
        sources=[
            {
                "source_id": "src-evil",
                "uri": "https://tampered.example/evil",
                "excerpt": "TAMPERED INJECTED CONTENT",
            }
        ],
        sources_verification="digest-mismatch",
    )
    page = research.render(
        {
            "status": _status_payload([]),
            "doctor": _doctor_payload(),
            "selected_run": "run-1",
            "show": show,
        },
        "nonce",
    )
    assert "digest-mismatch" in page
    assert "rs-state-serious" in page
    assert "Source content is withheld because the recorded digest does not match" in page
    assert "https://tampered.example/evil" not in page
    assert "TAMPERED INJECTED CONTENT" not in page
    assert "unverified" not in page.split("Source records are", 1)[1].split("</p>", 1)[0]


def test_inspector_handles_corrupt_show_records_without_raising() -> None:
    show = _show_payload(
        run={"run_id": None, "phases": "corrupt", "fallbacks": "corrupt", "blockers": "corrupt"},
        findings={"findings": "corrupt"},
        citation_audit={"accepted": True, "citations": "corrupt", "unresolved": "corrupt"},
        sources=["corrupt", 42],
        report="corrupt",
    )
    page = research.render(
        {
            "status": _status_payload([{"status": None}, "corrupt"]),
            "doctor": _doctor_payload(lanes=["corrupt"]),
            "selected_run": "run-1",
            "show": show,
        },
        "nonce",
    )
    assert "Run inspector" in page
    assert "No phase map recorded for this run." in page


def test_render_stale_snapshot_payload_shapes_do_not_raise() -> None:
    assert research.render({}, "nonce")
    assert research.render({"status": None, "doctor": None, "show": None, "selected_run": None}, "nonce")
    loading = research.render({"_center_loading": True}, "nonce")
    assert "Research run status is unavailable" in loading


def test_run_json_accepts_doctor_fail_exit_code(monkeypatch, tmp_path: Path) -> None:
    import subprocess
    from types import SimpleNamespace

    def fake_run(*args, **kwargs):
        return SimpleNamespace(
            returncode=1,
            stdout='{"schema": "brigade.research.doctor.v1", "status": "fail"}',
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert "error" in data.run_json(tmp_path, ["research", "doctor"])
    degraded = data.run_json(tmp_path, ["research", "doctor"], ok_codes=(0, 1))
    assert degraded["status"] == "fail"


def test_run_json_uses_json_error_field_not_first_pretty_line(monkeypatch, tmp_path: Path) -> None:
    import subprocess
    from types import SimpleNamespace

    def fake_run(*args, **kwargs):
        return SimpleNamespace(
            returncode=2,
            stdout='{\n  "error": "invalid run_id: \'..\'",\n  "failure_kind": "invalid-run-id"\n}',
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = data.run_json(tmp_path, ["research", "show", ".."])
    assert result["error"] == "invalid run_id: '..'"
    assert result["error"] != "{"


def test_fetch_tolerates_degraded_doctor_exit(monkeypatch, tmp_path: Path) -> None:
    captured: dict[str, tuple[int, ...]] = {}

    def fake_run_json(target, args, **kwargs):
        captured[tuple(args)[:2][-1]] = tuple(kwargs.get("ok_codes", (0,)))
        return {}

    monkeypatch.setattr(data, "run_json", fake_run_json)
    research.fetch(tmp_path)
    assert captured["doctor"] == (0, 1)
    assert captured["status"] == (0,)
