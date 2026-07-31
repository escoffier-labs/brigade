"""Embedded, read-only Run View document."""

from __future__ import annotations

from html import escape


def render_document(nonce: str) -> str:
    """Return the complete browser document for the local Run View."""
    escaped_nonce = escape(nonce, quote=True)
    return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="Content-Security-Policy" content="default-src 'self'; connect-src 'self'; style-src 'nonce-__BRIGADE_NONCE__'; script-src 'nonce-__BRIGADE_NONCE__'; base-uri 'none'; frame-ancestors 'none'">
  <title>Brigade Run View</title>
  <style nonce="__BRIGADE_NONCE__">
    :root {
      color-scheme: light dark;
      --surface: #ffffff;
      --panel: #f5f7fa;
      --ink: #17212b;
      --muted: #59636f;
      --line: #c4cbd4;
      --accent: #0759b0;
      --warning: #8a4b00;
    }
    * { box-sizing: border-box; }
    body { background: var(--surface); color: var(--ink); font: 16px/1.45 system-ui, sans-serif; margin: 0; }
    header { border-bottom: 1px solid var(--line); padding: 1rem; }
    h1, h2, h3 { line-height: 1.2; margin: 0 0 .6rem; }
    h1 { font-size: 1.35rem; }
    h2 { font-size: 1.1rem; }
    h3 { font-size: 1rem; }
    main { display: grid; gap: 1rem; grid-template-columns: minmax(17rem, 24rem) minmax(0, 1fr); margin: auto; max-width: 90rem; padding: 1rem; }
    section, details { background: var(--panel); border: 1px solid var(--line); border-radius: .4rem; padding: .8rem; }
    .controls { display: grid; gap: .55rem; grid-template-columns: 1fr 1fr; margin-bottom: .8rem; }
    label { display: grid; font-size: .88rem; gap: .2rem; }
    input, select, button { color: inherit; font: inherit; }
    input, select { background: var(--surface); border: 1px solid var(--line); border-radius: .25rem; padding: .4rem; }
    button { background: var(--surface); border: 1px solid var(--line); border-radius: .25rem; cursor: pointer; padding: .55rem; text-align: left; width: 100%; }
    button:hover { border-color: var(--accent); }
    button:focus-visible, input:focus-visible, select:focus-visible, summary:focus-visible { outline: 3px solid var(--accent); outline-offset: 2px; }
    .run-row, .collection-item { border-top: 1px solid var(--line); padding: .65rem 0; }
    .run-row:first-child, .collection-item:first-child { border-top: 0; }
    .status { font-weight: 700; }
    .stale { color: var(--warning); font-weight: 700; min-height: 1.5rem; }
    dl { display: grid; gap: .25rem .75rem; grid-template-columns: max-content minmax(0, 1fr); margin: .3rem 0; }
    dt { color: var(--muted); font-weight: 650; }
    dd { margin: 0; overflow-wrap: anywhere; }
    .empty { color: var(--muted); margin: .5rem 0; }
    pre { overflow-wrap: anywhere; white-space: pre-wrap; }
    @media (max-width: 700px) { main { grid-template-columns: 1fr; padding: .65rem; } .controls { grid-template-columns: 1fr; } }
    @media (prefers-reduced-motion: reduce) { *, *::before, *::after { scroll-behavior: auto !important; transition-duration: 0.01ms !important; animation-duration: 0.01ms !important; } }
    @media (prefers-color-scheme: dark) { :root { --surface: #10151b; --panel: #18212a; --ink: #edf1f5; --muted: #b4bec8; --line: #536170; --accent: #86beff; --warning: #ffbf69; } }
  </style>
</head>
<body>
  <header><h1>Brigade Run View</h1><p>Local, read-only run state.</p></header>
  <main>
    <section aria-labelledby="runs-heading">
      <h2 id="runs-heading">Recent runs</h2>
      <div class="controls">
        <label>Status filter
          <select id="status-filter"></select>
        </label>
        <label>Text filter <input id="text-filter" type="search" autocomplete="off"></label>
      </div>
      <nav id="run-list" aria-label="Run list" aria-live="polite"></nav>
    </section>
    <section aria-labelledby="detail-heading">
      <h2 id="detail-heading">Run detail</h2>
      <p id="connection" class="stale" role="status" aria-live="polite">Loading run list.</p>
      <div id="run-detail"></div>
      <details>
        <summary>Event diagnostics (safe, bounded)</summary>
        <div id="diagnostics"></div>
      </details>
    </section>
  </main>
  <script nonce="__BRIGADE_NONCE__">
    "use strict";
    const MAX_DIAGNOSTIC_RECORDS = 50;
    const MAX_DIAGNOSTIC_CHARACTERS = 4000;
    const SUMMARY_FIELDS = ["run_id", "status", "active_phase", "task_summary", "started_at", "status_started_at", "finished_at", "duration_seconds", "failure_phase", "mode", "stale", "resume_available"];
    const DETAIL_RUN_FIELDS = SUMMARY_FIELDS.concat(["failure_kind", "failure_detail"]);
    const TEXT_FILTER_FIELDS = ["run_id", "active_phase", "task_summary", "failure_phase"];
    const NONTERMINAL_STATUSES = new Set(["started", "planning", "dispatching", "result-processing", "synthesizing", "handoff", "artifact-collection", "running"]);
    const KNOWN_RECORD_TYPES = new Set(["watch", "run", "plan", "event", "workers", "synthesis", "final", "summary"]);
    const diagnosticRecords = [];
    const timeline = [];
    let summaries = [];
    let listDiagnostic = null;
    let skippedInvalid = 0;
    let currentDetail = null;
    let selectedRunId = null;
    let source = null;
    let retryDelay = 1000;
    let detailRefreshInFlight = false;
    let pendingDetailRunId = null;

    const byId = (id) => document.getElementById(id);

    function clear(node) {
      while (node.firstChild) node.removeChild(node.firstChild);
    }

    function text(value, fallback = "Not available") {
      return value === null || value === undefined || value === "" ? fallback : String(value);
    }

    function node(tag, className, value) {
      const element = document.createElement(tag);
      if (className) element.className = className;
      if (value !== undefined) element.textContent = text(value);
      return element;
    }

    function fieldList(entries) {
      const list = node("dl", "");
      entries.forEach(([label, value]) => {
        list.append(node("dt", "", label), node("dd", "", value));
      });
      return list;
    }

    function duration(seconds) {
      if (typeof seconds !== "number" || !Number.isFinite(seconds)) return "Not available";
      if (seconds < 60) return Math.round(seconds) + " seconds";
      if (seconds < 3600) return Math.round(seconds / 60) + " minutes";
      return (seconds / 3600).toFixed(1) + " hours";
    }

    function relativeTime(value) {
      const started = Date.parse(value);
      if (Number.isNaN(started)) return text(value);
      const seconds = Math.max(0, Math.round((Date.now() - started) / 1000));
      return seconds < 60 ? seconds + " seconds ago" : Math.round(seconds / 60) + " minutes ago";
    }

    function setConnection(message) {
      byId("connection").textContent = message;
    }

    function appendCollection(parent, title, items, entriesForItem) {
      const heading = node("h3", "", title);
      parent.append(heading);
      if (!Array.isArray(items) || items.length === 0) {
        parent.append(node("p", "empty", "No " + title.toLowerCase() + " available."));
        return;
      }
      items.forEach((item) => {
        const row = node("div", "collection-item");
        row.append(fieldList(entriesForItem(item || {})));
        parent.append(row);
      });
    }

    function matchingPlanEntry(detail, seat) {
      if (!seat || typeof seat.name !== "string" || !Array.isArray(detail.plan)) return null;
      return detail.plan.find((entry) => entry && typeof entry === "object" && entry.worker === seat.name) || null;
    }

    function matchingWorker(detail, seat) {
      if (!seat || typeof seat.name !== "string" || !Array.isArray(detail.workers)) return null;
      return detail.workers.find((worker) => worker && typeof worker === "object" && worker.worker === seat.name) || null;
    }

    function rosterAssignment(detail, seat) {
      const plan = matchingPlanEntry(detail, seat);
      return plan ? text(plan.task_summary) : "Not available";
    }

    function rosterResult(detail, seat) {
      const worker = matchingWorker(detail, seat);
      if (!worker) return "Not available";
      return [
        "Status: " + text(worker.status),
        "Detail: " + text(worker.detail),
        "Exit code: " + text(worker.exit_code),
      ].join("; ");
    }

    function renderStatusFilter() {
      const filter = byId("status-filter");
      const selected = filter.value;
      const statuses = Array.from(new Set(summaries.map((run) => run.status).filter((status) => typeof status === "string" && status !== ""))).sort();
      clear(filter);
      const allOption = document.createElement("option");
      allOption.value = "all";
      allOption.textContent = "All statuses";
      filter.append(allOption);
      statuses.forEach((status) => {
        const option = document.createElement("option");
        option.value = status;
        option.textContent = status;
        filter.append(option);
      });
      filter.value = statuses.includes(selected) ? selected : "all";
    }

    function filteredSummaries() {
      const status = byId("status-filter").value;
      const filterText = byId("text-filter").value.trim().toLowerCase();
      return summaries.filter((run) => {
        const statusMatches = status === "all" || String(run.status || "") === status;
        const textMatches = !filterText || TEXT_FILTER_FIELDS.some((field) => String(run[field] || "").toLowerCase().includes(filterText));
        return statusMatches && textMatches;
      });
    }

    function renderListNotices(container) {
      if (skippedInvalid > 0) container.append(node("p", "empty", "Skipped invalid runs: " + text(skippedInvalid)));
      if (!listDiagnostic) return;
      const disclosure = node("details", "");
      disclosure.append(node("summary", "", "Run list diagnostic"), node("p", "empty", listDiagnostic));
      container.append(disclosure);
    }

    function renderRunList() {
      const container = byId("run-list");
      clear(container);
      const visible = filteredSummaries();
      if (visible.length === 0) {
        container.append(node("p", "empty", summaries.length === 0 ? "No runs available." : "No runs match the local filters."));
        renderListNotices(container);
        return;
      }
      visible.forEach((run) => {
        const row = node("div", "run-row");
        const select = node("button", "", "Open run " + text(run.run_id));
        select.type = "button";
        select.addEventListener("click", () => selectRun(String(run.run_id || "")));
        row.append(select, fieldList([
          ["Status", text(run.status)], ["Active phase", text(run.active_phase)],
          ["Task", text(run.task_summary)], ["Started", relativeTime(run.started_at)],
          ["Duration", duration(run.duration_seconds)], ["Failure phase", text(run.failure_phase)],
        ]));
        container.append(row);
      });
      renderListNotices(container);
    }

    function renderBrief(parent, title, brief, isEvidence) {
      const data = brief && typeof brief === "object" ? brief : {};
      const panel = node("details", "collection-item");
      panel.append(node("summary", "", title), fieldList([
        ["Attached", text(data.attached)], ["Size", text(data.size_bytes)], ["Count", text(data.count)],
        ["Summary", text(data.summary)], ["Untrusted evidence", isEvidence ? "Yes" : "No"],
      ]));
      parent.append(panel);
    }

    function renderDetail(detail) {
      const container = byId("run-detail");
      clear(container);
      if (!detail || !detail.run) {
        container.append(node("p", "empty", "Select a run to inspect its safe detail snapshot."));
        return;
      }
      const run = detail.run;
      container.append(node("h3", "", "Run " + text(run.run_id)), fieldList([
        ["Status", text(run.status)], ["Mode", text(run.mode)], ["Active phase", text(run.active_phase)],
        ["Started", text(run.started_at)], ["Status started at", text(run.status_started_at)], ["Finished at", text(run.finished_at)], ["Duration", duration(run.duration_seconds)],
        ["Failure phase", text(run.failure_phase)], ["Failure kind", text(run.failure_kind)], ["Failure detail", text(run.failure_detail)],
      ]));
      appendCollection(container, "Roster", detail.roster, (seat) => [
        ["Seat", text(seat.name)], ["Model", text(seat.model)], ["Effort", text(seat.reasoning)],
        ["CLI", text(seat.cli)], ["Assignment", rosterAssignment(detail, seat)],
        ["Timeout", duration(seat.timeout_seconds)], ["Result", rosterResult(detail, seat)],
      ]);
      appendCollection(container, "Plan", detail.plan, (stage) => [
        ["Stage", text(stage.stage)], ["Worker order", text(stage.order)], ["Worker", text(stage.worker)], ["Assignment", text(stage.task_summary)],
      ]);
      appendCollection(container, "Workers", detail.workers, (worker) => [
        ["Worker", text(worker.worker)], ["Assignment", text(worker.task_summary)], ["Result", text(worker.status)],
        ["Detail", text(worker.detail)], ["Duration", duration(worker.duration_seconds)], ["Exit code", text(worker.exit_code)],
      ]);
      appendCollection(container, "Verification", detail.verification, (receipt) => [
        ["Receipt", text(receipt.receipt_id)], ["Status", text(receipt.status)], ["Command", text(receipt.command_label)],
        ["Duration", duration(receipt.duration_seconds)], ["Exit code", text(receipt.exit_code)],
      ]);
      appendCollection(container, "Phase timeline", timeline, (event) => [
        ["Phase event", text(event.item_type)], ["Worker", text(event.worker)], ["Method", text(event.method)],
      ]);
      const briefs = detail.briefs && typeof detail.briefs === "object" ? detail.briefs : {};
      container.append(node("h3", "", "Brief summaries"));
      renderBrief(container, "Code Intelligence brief", briefs.code_graph, false);
      renderBrief(container, "Drift brief", briefs.drift_impact, false);
      renderBrief(container, "Evidence Ledger brief", briefs.evidence, true);
    }

    function primitive(value) {
      return value === null || ["string", "number", "boolean"].includes(typeof value);
    }

    function allowedObject(value, fields) {
      const safe = {};
      if (!value || typeof value !== "object" || Array.isArray(value)) return safe;
      fields.forEach((key) => { if (primitive(value[key])) safe[key] = value[key]; });
      return safe;
    }

    function allowedList(value, fields) {
      return Array.isArray(value) ? value.slice(0, MAX_DIAGNOSTIC_RECORDS).map((item) => allowedObject(item, fields)) : [];
    }

    function mergeRunSummary(detailRun, summaryRun) {
      return Object.assign({}, allowedObject(detailRun, DETAIL_RUN_FIELDS), allowedObject(summaryRun, SUMMARY_FIELDS));
    }

    function isTerminalStatus(status) {
      return typeof status === "string" && !NONTERMINAL_STATUSES.has(status);
    }

    function shouldRefreshDetail(record) {
      if (record.type === "final") return true;
      if (record.type === "run") return isTerminalStatus(record.run && record.run.status);
      return record.type === "summary" && isTerminalStatus(record.status);
    }

    function safeDiagnosticRecord(record) {
      const safe = { schema: "brigade.run-watch.v1", type: record.type };
      if (record.type === "run") safe.run = allowedObject(record.run, SUMMARY_FIELDS);
      if (record.type === "plan") safe.assignments = allowedList(record.assignments, ["stage", "order", "worker", "task_summary"]);
      if (record.type === "event") Object.assign(safe, allowedObject(record, ["worker", "method", "item_type"]));
      if (record.type === "workers") safe.workers = allowedList(record.workers, ["worker", "task_summary", "status", "ok", "detail", "duration_seconds", "exit_code"]);
      if (record.type === "synthesis") safe.synthesis = allowedObject(record.synthesis, ["orchestrator", "ok", "detail"]);
      if (record.type === "summary") Object.assign(safe, allowedObject(record, ["run_id", "status", "duration_seconds", "failure_phase", "resume_available"]));
      if (record.type === "final") Object.assign(safe, allowedObject(record, ["available", "size_bytes"]));
      return safe;
    }

    function renderDiagnostics() {
      const container = byId("diagnostics");
      clear(container);
      diagnosticRecords.forEach((record) => {
        const entry = node("pre", "");
        entry.textContent = JSON.stringify(record).slice(0, MAX_DIAGNOSTIC_CHARACTERS);
        container.append(entry);
      });
    }

    function addDiagnostic(record) {
      diagnosticRecords.push(safeDiagnosticRecord(record));
      const boundedRecords = diagnosticRecords.slice(-MAX_DIAGNOSTIC_RECORDS);
      diagnosticRecords.splice(0, diagnosticRecords.length, ...boundedRecords);
      renderDiagnostics();
    }

    function handleWatchRecord(record) {
      if (!record || record.schema !== "brigade.run-watch.v1" || !KNOWN_RECORD_TYPES.has(record.type)) {
        // Ignore an unknown record type so future contracts do not stop live updates.
        return;
      }
      addDiagnostic(record);
      const refreshRequired = shouldRefreshDetail(record);
      if (!currentDetail) {
        if (refreshRequired) requestDetailRefresh(selectedRunId);
        return;
      }
      if (record.type === "run") currentDetail.run = mergeRunSummary(currentDetail.run, record.run);
      if (record.type === "plan") currentDetail.plan = allowedList(record.assignments, ["stage", "order", "worker", "task_summary"]);
      if (record.type === "workers") currentDetail.workers = allowedList(record.workers, ["worker", "task_summary", "status", "ok", "detail", "duration_seconds", "exit_code"]);
      if (record.type === "synthesis") currentDetail.synthesis = allowedObject(record.synthesis, ["orchestrator", "ok", "detail"]);
      if (record.type === "summary") currentDetail.run = mergeRunSummary(currentDetail.run, record);
      if (record.type === "event") {
        timeline.push(allowedObject(record, ["worker", "method", "item_type"]));
        timeline.splice(0, Math.max(0, timeline.length - MAX_DIAGNOSTIC_RECORDS));
      }
      renderDetail(currentDetail);
      if (refreshRequired) requestDetailRefresh(selectedRunId);
    }

    async function requestJson(path) {
      const response = await fetch(path, { credentials: "same-origin", headers: { Accept: "application/json" } });
      if (!response.ok) throw new Error("The local Run View request failed.");
      return response.json();
    }

    async function loadRuns() {
      try {
        const payload = await requestJson("/api/runs?limit=50");
        summaries = allowedList(payload.runs, SUMMARY_FIELDS);
        listDiagnostic = payload && payload.schema === "brigade.runs-list.v1" && payload.diagnostic === "runs directory is unavailable" ? payload.diagnostic : null;
        skippedInvalid = typeof payload.skipped_invalid === "number" && Number.isFinite(payload.skipped_invalid) && payload.skipped_invalid > 0 ? Math.floor(payload.skipped_invalid) : 0;
        renderStatusFilter();
        renderRunList();
        if (summaries.length === 0) setConnection("No runs are currently available.");
      } catch (_) {
        setConnection("Stale data: the local run list could not be refreshed.");
      }
    }

    function requestDetailRefresh(runId) {
      if (!runId || selectedRunId !== runId) return;
      if (detailRefreshInFlight) {
        pendingDetailRunId = runId;
        return;
      }
      void refreshDetail(runId);
    }

    async function refreshDetail(runId) {
      if (!runId || selectedRunId !== runId) return;
      if (detailRefreshInFlight) {
        pendingDetailRunId = runId;
        return;
      }
      detailRefreshInFlight = true;
      try {
        const detail = await requestJson("/api/runs/" + encodeURIComponent(runId));
        if (selectedRunId !== runId) return;
        currentDetail = detail;
        renderDetail(detail);
        setConnection("Live detail connected. Status: " + text(detail.run && detail.run.status));
      } catch (_) {
        if (selectedRunId === runId) setConnection("Stale data: retaining the last good run snapshot.");
      } finally {
        detailRefreshInFlight = false;
        const pendingRunId = pendingDetailRunId;
        pendingDetailRunId = null;
        if (pendingRunId && selectedRunId === pendingRunId) requestDetailRefresh(pendingRunId);
      }
    }

    function connectEvents(runId) {
      if (source) source.close();
      const stream = new EventSource("/api/runs/" + encodeURIComponent(runId) + "/events");
      source = stream;
      stream.onopen = () => { if (selectedRunId === runId) retryDelay = 1000; };
      stream.onmessage = (message) => {
        try { handleWatchRecord(JSON.parse(message.data)); }
        catch (_) { setConnection("Stale data: a live update was invalid; retaining the last good snapshot."); }
      };
      stream.onerror = () => {
        if (source !== stream || selectedRunId !== runId) return;
        stream.close();
        setConnection("Stale data: reconnecting to live updates.");
        const delay = retryDelay;
        retryDelay = Math.min(retryDelay * 2, 30000);
        window.setTimeout(() => {
          if (selectedRunId !== runId) return;
          requestDetailRefresh(runId);
          connectEvents(runId);
        }, delay);
      };
    }

    function selectRun(runId) {
      if (!runId) return;
      selectedRunId = runId;
      timeline.length = 0;
      requestDetailRefresh(runId);
      connectEvents(runId);
    }

    byId("status-filter").addEventListener("change", renderRunList);
    byId("text-filter").addEventListener("input", renderRunList);
    loadRuns();
  </script>
</body>
</html>
""".replace("__BRIGADE_NONCE__", escaped_nonce)
