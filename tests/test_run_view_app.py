from __future__ import annotations

from html.parser import HTMLParser

from brigade.run_view_app import render_document


class _DocumentParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tags: list[tuple[str, dict[str, str | None]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, dict(attrs)))


def _document() -> str:
    return render_document("test-nonce-123")


def test_render_document_is_a_nonce_protected_self_contained_document() -> None:
    document = _document()
    parser = _DocumentParser()
    parser.feed(document)

    assert "<!doctype html>" in document.lower()
    assert any(tag == "style" and attrs.get("nonce") == "test-nonce-123" for tag, attrs in parser.tags)
    assert any(tag == "script" and attrs.get("nonce") == "test-nonce-123" for tag, attrs in parser.tags)
    assert not any(tag in {"link", "iframe", "object", "embed"} for tag, _ in parser.tags)
    assert "http://" not in document
    assert "https://" not in document
    assert "//cdn" not in document


def test_document_uses_safe_dom_rendering_for_malicious_text() -> None:
    document = _document()
    malicious_task = "<img src=x onerror=alert(1)>"
    malicious_evidence = '<svg/onload=alert("evidence")>'

    assert malicious_task not in document
    assert malicious_evidence not in document
    assert "document.createElement" in document
    assert ".textContent" in document
    for unsafe_sink in (
        "innerHTML",
        "outerHTML",
        "insertAdjacentHTML",
        "document.write",
        "eval(",
        "Function(",
    ):
        assert unsafe_sink not in document
    assert "encodeURIComponent(runId)" in document


def test_document_includes_run_list_and_detail_contract_fields() -> None:
    document = _document()

    for label in (
        "Active phase",
        "Started",
        "Duration",
        "Failure phase",
        "Status filter",
        "Text filter",
        "Roster",
        "Model",
        "Effort",
        "CLI",
        "Assignment",
        "Timeout",
        "Result",
        "Plan",
        "Worker order",
        "Phase timeline",
        "Failure kind",
        "Failure detail",
        "Verification",
        "Receipt",
        "Command",
        "Exit code",
        "Code Intelligence brief",
        "Drift brief",
        "Evidence Ledger brief",
        "Untrusted evidence",
    ):
        assert label in document


def test_document_populates_status_filter_from_allowed_run_summary_statuses() -> None:
    document = _document()

    assert '<select id="status-filter"></select>' in document
    assert "function renderStatusFilter()" in document
    assert "allowedList(payload.runs, SUMMARY_FIELDS)" in document
    assert 'allOption.value = "all";' in document
    assert 'allOption.textContent = "All statuses";' in document
    assert "option.value = status;" in document
    assert "option.textContent = status;" in document
    assert '<option value="running">' not in document


def test_document_filters_run_text_across_the_local_summary_fields() -> None:
    document = _document()

    assert '<label>Text filter <input id="text-filter"' in document
    assert 'const filterText = byId("text-filter").value.trim().toLowerCase();' in document
    assert 'const TEXT_FILTER_FIELDS = ["run_id", "active_phase", "task_summary", "failure_phase"];' in document
    assert 'TEXT_FILTER_FIELDS.some((field) => String(run[field] || "").toLowerCase().includes(filterText))' in document
    assert 'byId("text-filter").addEventListener("input", renderRunList);' in document


def test_document_renders_list_diagnostic_and_skipped_invalid_count_safely() -> None:
    document = _document()

    assert 'payload.schema === "brigade.runs-list.v1"' in document
    assert 'payload.diagnostic === "runs directory is unavailable"' in document
    assert 'node("summary", "", "Run list diagnostic")' in document
    assert 'node("p", "empty", listDiagnostic)' in document
    assert '"Skipped invalid runs: " + text(skippedInvalid)' in document


def test_document_includes_status_and_finished_timestamps_in_run_detail() -> None:
    document = _document()

    assert '["Status started at", text(run.status_started_at)]' in document
    assert '["Finished at", text(run.finished_at)]' in document


def test_document_roster_joins_safe_plan_and_worker_records_by_seat_name() -> None:
    document = _document()

    assert "function matchingPlanEntry(detail, seat)" in document
    assert "function matchingWorker(detail, seat)" in document
    assert "entry.worker === seat.name" in document
    assert "worker.worker === seat.name" in document
    assert 'return plan ? text(plan.task_summary) : "Not available";' in document
    assert '["Assignment", rosterAssignment(detail, seat)]' in document
    assert '["CLI", text(seat.cli)]' in document
    assert '["Result", rosterResult(detail, seat)]' in document
    assert "See worker state" not in document


def test_document_roster_result_includes_matched_worker_status_detail_and_exit_code() -> None:
    document = _document()

    assert "function rosterResult(detail, seat)" in document
    assert '"Status: " + text(worker.status)' in document
    assert '"Detail: " + text(worker.detail)' in document
    assert '"Exit code: " + text(worker.exit_code)' in document


def test_document_merges_summary_watch_runs_without_losing_detail_only_failures() -> None:
    document = _document()

    assert 'const DETAIL_RUN_FIELDS = SUMMARY_FIELDS.concat(["failure_kind", "failure_detail"]);' in document
    assert "function mergeRunSummary(detailRun, summaryRun)" in document
    assert "currentDetail.run = mergeRunSummary(currentDetail.run, record.run);" in document
    assert "failure_kind" in document
    assert "failure_detail" in document


def test_document_refreshes_detail_once_for_terminal_watch_records() -> None:
    document = _document()

    assert "function isTerminalStatus(status)" in document
    assert "function shouldRefreshDetail(record)" in document
    assert 'if (record.type === "final") return true;' in document
    assert "requestDetailRefresh(selectedRunId);" in document
    assert "const refreshRequired = shouldRefreshDetail(record);" in document
    assert "if (refreshRequired) requestDetailRefresh(selectedRunId);" in document
    assert "let detailRefreshInFlight = false;" in document
    assert "let pendingDetailRunId = null;" in document


def test_document_bounds_diagnostics_and_recovers_from_stream_errors() -> None:
    document = _document()

    assert "MAX_DIAGNOSTIC_RECORDS = 50" in document
    assert "MAX_DIAGNOSTIC_CHARACTERS = 4000" in document
    assert "slice(-MAX_DIAGNOSTIC_RECORDS)" in document
    assert "slice(0, MAX_DIAGNOSTIC_CHARACTERS)" in document
    assert "<details" in document
    assert "EventSource" in document
    assert "unknown record type" in document
    assert "setTimeout" in document
    assert "Math.min(retryDelay * 2, 30000)" in document
    assert "Stale data" in document
    assert "refreshDetail(runId)" in document


def test_document_includes_keyboard_and_responsive_accessibility_support() -> None:
    document = _document()

    for required in (
        "button",
        "aria-live",
        ":focus-visible",
        "@media (max-width: 700px)",
        "@media (prefers-reduced-motion: reduce)",
        "@media (prefers-color-scheme: dark)",
        "color-scheme: light dark",
        "Status:",
    ):
        assert required in document
