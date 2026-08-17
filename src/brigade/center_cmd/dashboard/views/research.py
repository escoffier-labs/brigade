"""Research dashboard view.

Operator question: what are we researching, what has it found, and can I
trust the evidence?

Contract-only: ``brigade research status --all --json``,
``brigade research doctor --json``, and ``brigade research show <id> --json``.
No direct reads of ``.brigade/research/``, ``.brigade/runs/``, Oracle
sessions, or browser profiles.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from brigade.center_cmd.dashboard import data
from brigade.center_cmd.dashboard import render as html
from brigade.research.registry import validate_run_id

NAME = "research"
TITLE = "Research"
ORDER = 5.5

STATUS_SCHEMAS = frozenset({"brigade.research.status.v1"})
DOCTOR_SCHEMAS = frozenset({"brigade.research.doctor.v1"})
SHOW_SCHEMAS = frozenset({"brigade.research.show.v1", "brigade.research.show.v2"})
SHOW_V1_SCHEMA = "brigade.research.show.v1"

RESEARCH_PHASES = (
    "planning",
    "discovery",
    "extraction",
    "synthesis",
    "review",
    "repair",
    "publishing",
)

_RECENT_LIMIT = 30
_FINDINGS_LIMIT = 20
_QUESTION_MAX_CHARS = 160
_COMPLETED_STATUSES = frozenset({"completed", "done"})
_FAILED_STATUSES = frozenset({"failed", "error", "timeout", "incomplete"})

_STATUS_GOOD = "#0ca30c"
_STATUS_WARNING = "#fab219"
_STATUS_SERIOUS = "#ec835a"
_TEXT_PRIMARY = "#0b0b0b"
_TEXT_SECONDARY = "#52514e"
_SURFACE = "#fcfcfb"
_BORDER = "rgba(11,11,11,0.10)"


def _valid_run_query(run_id: str) -> bool:
    """Accept inspector run ids the registry would also accept.

    Rejects leading dashes (so the value cannot become an argv flag) and the
    ``.`` / ``..`` segments ``validate_run_id`` already refuses.
    """
    if not run_id or run_id.startswith("-"):
        return False
    try:
        validate_run_id(run_id)
    except ValueError:
        return False
    return True


def run_tile_state(rec: Mapping[str, Any]) -> str:
    """Map one status record to a tile bucket.

    ``active``, ``blocked``, ``completed``, ``failed``, or ``cancelled``.
    Only the recorded status and blockers list participate; error text never
    decides a bucket.
    """
    status = str(rec.get("status") or "").strip().lower()
    if status == "cancelled":
        return "cancelled"
    if status in _COMPLETED_STATUSES:
        return "completed"
    if status in _FAILED_STATUSES:
        return "failed"
    blockers = rec.get("blockers")
    if isinstance(blockers, list) and blockers:
        return "blocked"
    return "active"


def fetch(target: Path, query: dict[str, str] | None = None) -> dict:
    payload: dict[str, Any] = {
        "status": data.run_json(target, ["research", "status", "--all"]),
        # `research doctor` exits 1 for an overall "fail" while still printing
        # its versioned payload; the page must render that degraded state.
        "doctor": data.run_json(target, ["research", "doctor"], ok_codes=(0, 1)),
        "selected_run": "",
    }
    requested = (query or {}).get("run", "").strip()
    if requested and _valid_run_query(requested):
        payload["selected_run"] = requested
        payload["show"] = data.run_json(target, ["research", "show", requested])
    return payload


def render(payload: dict, nonce: str) -> str:
    status = payload.get("status") if isinstance(payload.get("status"), dict) else {}
    doctor = payload.get("doctor") if isinstance(payload.get("doctor"), dict) else {}
    show = payload.get("show") if isinstance(payload.get("show"), dict) else None
    selected = str(payload.get("selected_run") or "")
    now = datetime.now(timezone.utc)
    runs = _runs_from_status(status)
    return "".join(
        [
            _stylesheet(nonce),
            _summary_section(status, doctor, runs),
            _provider_section(doctor),
            _pipeline_section(runs),
            _recent_section(status, runs, now),
            _inspector_section(show, selected, now),
        ]
    )


def _schema_problem(payload: Mapping[str, Any], accepted: frozenset[str]) -> str | None:
    """Return an operator-facing problem string, or None when usable."""
    if payload.get("error"):
        return str(payload["error"])
    schema = payload.get("schema")
    if not isinstance(schema, str) or schema not in accepted:
        got = schema if isinstance(schema, str) else "none"
        return f"unsupported schema: {got} (this page understands {', '.join(sorted(accepted))})"
    return None


def _runs_from_status(status: Mapping[str, Any]) -> list[dict[str, Any]] | None:
    if _schema_problem(status, STATUS_SCHEMAS) is not None:
        return None
    raw = status.get("runs")
    if not isinstance(raw, list):
        return None
    return [item for item in raw if isinstance(item, dict)]


def _tile_counts(runs: list[dict[str, Any]]) -> dict[str, int]:
    counts = {"active": 0, "blocked": 0, "completed": 0, "failed": 0, "cancelled": 0}
    for rec in runs:
        counts[run_tile_state(rec)] += 1
    return counts


def _summary_section(
    status: Mapping[str, Any],
    doctor: Mapping[str, Any],
    runs: list[dict[str, Any]] | None,
) -> str:
    sentence = _summary_sentence(status, doctor, runs)
    strip = (
        f'<section class="rs-summary" aria-label="{html.esc("Research at a glance")}">'
        f"<p>{html.esc(sentence)}</p></section>"
    )
    if runs is None:
        return strip
    counts = _tile_counts(runs)
    tiles = _tiles(
        [
            (
                "Active",
                str(counts["active"]),
                "good" if counts["active"] else "neutral",
                "Runs moving through the pipeline with no recorded blocker.",
            ),
            (
                "Blocked",
                str(counts["blocked"]),
                "serious" if counts["blocked"] else "good",
                "Runs stopped on a recorded blocker.",
            ),
            (
                "Completed",
                str(counts["completed"]),
                "good" if counts["completed"] else "neutral",
                "Runs that published a report.",
            ),
            (
                "Failed",
                str(counts["failed"]),
                "serious" if counts["failed"] else "good",
                "Runs that ended in failure, error, timeout, or incomplete.",
            ),
        ]
    )
    return strip + tiles


def _summary_sentence(
    status: Mapping[str, Any],
    doctor: Mapping[str, Any],
    runs: list[dict[str, Any]] | None,
) -> str:
    if runs is None:
        problem = _schema_problem(status, STATUS_SCHEMAS) or "run list unavailable"
        return f"Research run status is unavailable ({problem}). {_provider_sentence(doctor)}"
    if not runs:
        return f"No research runs are on file yet. {_provider_sentence(doctor)}"
    counts = _tile_counts(runs)
    if counts["active"] or counts["blocked"]:
        working = counts["active"] + counts["blocked"]
        noun = "question" if working == 1 else "questions"
        first = f"Researching {working} {noun} right now"
        if counts["blocked"]:
            first += f" ({counts['blocked']} blocked on operator attention)"
    else:
        first = "Nothing is being researched right now"
    on_file: list[str] = []
    if counts["completed"]:
        on_file.append(f"{counts['completed']} completed")
    if counts["failed"]:
        on_file.append(f"{counts['failed']} failed")
    if counts["cancelled"]:
        on_file.append(f"{counts['cancelled']} cancelled")
    second = f"; {', '.join(on_file)} run(s) are on file" if on_file else ""
    return f"{first}{second}. {_provider_sentence(doctor)}"


def _provider_sentence(doctor: Mapping[str, Any]) -> str:
    problem = _schema_problem(doctor, DOCTOR_SCHEMAS)
    if problem is not None:
        return "Provider health is unavailable."
    overall = str(doctor.get("status") or "unknown")
    if overall == "ok":
        return "Providers look healthy."
    if overall == "warn":
        return "Providers are degraded but usable."
    if overall == "fail":
        return "A required provider is failing."
    return "Provider health is unknown."


def _provider_section(doctor: Mapping[str, Any]) -> str:
    problem = _schema_problem(doctor, DOCTOR_SCHEMAS)
    if problem is not None:
        return html.error_panel("Provider health", problem)
    lanes = [lane for lane in (doctor.get("lanes") or []) if isinstance(lane, dict)]
    overall = str(doctor.get("status") or "unknown")
    profile = str(doctor.get("profile") or "-")
    head = (
        f'<p>Overall <span class="rs-state rs-state-{html.esc(_doctor_css(overall))}">{html.esc(overall)}</span>'
        f" for profile <strong>{html.esc(profile)}</strong>.</p>"
    )
    reasons = [str(item) for item in (doctor.get("reasons") or []) if isinstance(item, str)]
    reason_html = ""
    if reasons:
        items = "".join(f"<li>{html.esc(item)}</li>" for item in reasons)
        reason_html = f'<ul class="rs-reasons">{items}</ul>'
    if not lanes:
        body = head + reason_html + f"<p>{html.esc('No provider lanes are configured.')}</p>"
        return html.panel(html.esc("Provider health"), body)
    rows = []
    for lane in lanes:
        rows.append(
            [
                html.esc(lane.get("capability") or "-"),
                html.esc(lane.get("seat") or "not configured"),
                _state_chip(str(lane.get("status") or "unknown"), _doctor_css(str(lane.get("status") or ""))),
                html.esc(lane.get("auth_status") or "-"),
                html.esc(_lane_model_text(lane)),
                html.esc(_lane_fallback_text(lane)),
                html.esc(lane.get("detail") or "-"),
            ]
        )
    table = html.table(
        [
            html.esc("Capability"),
            html.esc("Seat"),
            html.esc("Status"),
            html.esc("Auth"),
            html.esc("Model"),
            html.esc("Fallback"),
            html.esc("Detail"),
        ],
        rows,
    )
    return html.panel(html.esc("Provider health"), head + reason_html + table)


def _doctor_css(status: str) -> str:
    if status == "ok":
        return "good"
    if status == "warn":
        return "warning"
    if status == "fail":
        return "serious"
    return "neutral"


def _lane_model_text(lane: Mapping[str, Any]) -> str:
    requested = lane.get("requested_model")
    label = str(requested) if isinstance(requested, str) and requested else "-"
    attestation = lane.get("model_attestation")
    if attestation != "verified":
        return f"{label} (unverified)"
    return label


def _lane_fallback_text(lane: Mapping[str, Any]) -> str:
    seat = lane.get("fallback_seat")
    if not isinstance(seat, str) or not seat:
        return "none"
    status = lane.get("fallback_status")
    suffix = f" ({status})" if isinstance(status, str) and status else ""
    return f"{seat}{suffix}"


def _pipeline_section(runs: list[dict[str, Any]] | None) -> str:
    if runs is None:
        return ""
    working = [rec for rec in runs if run_tile_state(rec) in {"active", "blocked"}]
    if not working:
        return ""
    parts: list[str] = []
    for rec in working:
        question = _question_text(rec)
        state = run_tile_state(rec)
        header = (
            f'<p class="rs-run-head"><strong>{html.esc(question)}</strong> {_state_chip(state, _state_css(state))}</p>'
        )
        parts.append(header + _phase_pipeline(rec))
        blockers = rec.get("blockers")
        if state == "blocked" and isinstance(blockers, list):
            items = "".join(f"<li>{html.esc(str(item))}</li>" for item in blockers[:10])
            parts.append(f'<ul class="rs-reasons">{items}</ul>')
    return html.panel(html.esc("Phase pipeline"), "".join(parts))


def _phase_pipeline(rec: Mapping[str, Any]) -> str:
    phases = rec.get("phases")
    if not isinstance(phases, Mapping):
        return f'<p class="rs-muted">{html.esc("No phase map recorded for this run.")}</p>'
    chips: list[str] = []
    for name in RESEARCH_PHASES:
        entry = phases.get(name)
        status = str(entry.get("status") or "pending") if isinstance(entry, Mapping) else "pending"
        css = _phase_css(status)
        chips.append(f'<li class="rs-phase rs-phase-{html.esc(css)}" title="{html.esc(status)}">{html.esc(name)}</li>')
    return f'<ol class="rs-pipeline">{"".join(chips)}</ol>'


def _phase_css(status: str) -> str:
    if status == "completed":
        return "done"
    if status == "running":
        return "running"
    if status in {"failed", "cancelled"}:
        return "failed"
    return "pending"


def _recent_section(
    status: Mapping[str, Any],
    runs: list[dict[str, Any]] | None,
    now: datetime,
) -> str:
    if runs is None:
        problem = _schema_problem(status, STATUS_SCHEMAS) or "run list unavailable"
        return html.error_panel("Recent runs", problem)
    if not runs:
        body = f"<p>{html.esc('No research runs yet. Start one with: brigade research run <question>.')}</p>"
        return html.panel(html.esc("Recent runs"), body)
    shown = runs[:_RECENT_LIMIT]
    note = ""
    if len(runs) > len(shown):
        note = f'<p class="rs-muted">{html.esc(f"Showing the {len(shown)} newest of {len(runs)} runs.")}</p>'
    rows = []
    for rec in shown:
        state = run_tile_state(rec)
        rows.append(
            [
                _question_cell(rec),
                _state_chip(state, _state_css(state)),
                html.esc(rec.get("current_phase") or "-"),
                html.esc(_profile_text(rec)),
                html.esc(_synthesis_cell(rec)),
                html.esc(_fallback_text(rec)),
                html.esc(_citation_text(rec, state)),
                html.esc(_age_text(rec, now)),
            ]
        )
    table = html.table(
        [
            html.esc("Question"),
            html.esc("State"),
            html.esc("Phase"),
            html.esc("Profile"),
            html.esc("Synthesis"),
            html.esc("Fallback"),
            html.esc("Citations"),
            html.esc("Age"),
        ],
        rows,
    )
    return html.panel(html.esc("Recent runs"), note + table)


def _question_text(rec: Mapping[str, Any]) -> str:
    question = str(rec.get("question") or "(no question recorded)")
    if len(question) > _QUESTION_MAX_CHARS:
        question = question[:_QUESTION_MAX_CHARS] + "..."
    return question


def _question_cell(rec: Mapping[str, Any]) -> str:
    question = html.esc(_question_text(rec))
    run_id = str(rec.get("run_id") or "")
    legacy = f' <span class="rs-state rs-state-neutral">{html.esc("legacy")}</span>' if rec.get("legacy") else ""
    if run_id and _valid_run_query(run_id):
        href = html.esc(f"/view/{NAME}?run={run_id}")
        link = f'<a href="{href}">{question}</a>'
    else:
        link = question
    details = (
        f'<details class="rs-debug"><summary>{html.esc("Run id")}</summary>'
        f"<code>{html.esc(run_id or '-')}</code></details>"
    )
    return f"{link}{legacy}{details}"


def _profile_text(rec: Mapping[str, Any]) -> str:
    profile = rec.get("profile")
    if isinstance(profile, str) and profile:
        return profile
    return "legacy" if rec.get("legacy") else "-"


def _synthesis_seat(rec: Mapping[str, Any]) -> str:
    synthesis = rec.get("synthesis")
    if isinstance(synthesis, Mapping) and synthesis.get("seat"):
        return str(synthesis["seat"])
    lanes = rec.get("resolved_lanes")
    if isinstance(lanes, Mapping):
        entry = lanes.get("synthesis")
        if isinstance(entry, Mapping) and entry.get("primary"):
            return str(entry["primary"])
    return "-"


def _synthesis_cell(rec: Mapping[str, Any]) -> str:
    seat = _synthesis_seat(rec)
    if seat == "-":
        return "-"
    synthesis = rec.get("synthesis")
    observed = synthesis.get("observed_model") if isinstance(synthesis, Mapping) else None
    if not isinstance(observed, str) or observed in {"", "unverified"}:
        return f"{seat} (model unverified)"
    return seat


def _fallback_text(rec: Mapping[str, Any]) -> str:
    fallbacks = rec.get("fallbacks")
    if not isinstance(fallbacks, list):
        return "-"
    if not fallbacks:
        return "none"
    parts: list[str] = []
    for item in fallbacks:
        if isinstance(item, Mapping):
            parts.append(f"{item.get('from_seat') or '?'} -> {item.get('to_seat') or '?'}")
    return "; ".join(parts) if parts else "recorded"


def _citation_text(rec: Mapping[str, Any], state: str) -> str:
    if rec.get("legacy"):
        return "legacy run"
    phases = rec.get("phases")
    review = phases.get("review") if isinstance(phases, Mapping) else None
    if isinstance(review, Mapping) and "accepted" in review:
        return "accepted" if review.get("accepted") else "rejected"
    if state == "completed":
        return "no audit recorded"
    return "-"


def _age_text(rec: Mapping[str, Any], now: datetime) -> str:
    stamp = rec.get("created_at") or rec.get("started_at")
    if not isinstance(stamp, str):
        return "-"
    try:
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return "-"
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    seconds = max(0.0, (now - parsed).total_seconds())
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)}m ago"
    if seconds < 172800:
        return f"{int(seconds // 3600)}h ago"
    return f"{int(seconds // 86400)}d ago"


def _inspector_section(show: dict[str, Any] | None, selected: str, now: datetime) -> str:
    if not selected:
        body = f"<p>{html.esc('Select a run from the recent list to inspect its evidence.')}</p>"
        return html.panel(html.esc("Run inspector"), body)
    if show is None:
        return html.error_panel("Run inspector", "no inspector data was fetched for this run")
    problem = _schema_problem(show, SHOW_SCHEMAS)
    if problem is not None:
        return html.error_panel("Run inspector", problem)
    rec = show.get("run") if isinstance(show.get("run"), Mapping) else {}
    state = run_tile_state(rec)
    if show.get("schema") == SHOW_V1_SCHEMA:
        parts = [
            _inspector_summary(rec, state, now),
            _phase_pipeline(rec),
            _inspector_findings(show),
            _inspector_citations(show, state),
            _inspector_fallbacks(rec),
            _sub(
                "Sources and report",
                (
                    "<p>"
                    + html.esc(
                        "This inspector received brigade.research.show.v1. "
                        "Sources and the digest-verified report require show.v2; "
                        "findings and citations below are the v1 subset."
                    )
                    + "</p>"
                ),
            ),
            _inspector_receipt(show, rec),
        ]
    else:
        parts = [
            _inspector_summary(rec, state, now),
            _phase_pipeline(rec),
            _inspector_findings(show),
            _inspector_sources(show),
            _inspector_citations(show, state),
            _inspector_fallbacks(rec),
            _inspector_report(show),
            _inspector_receipt(show, rec),
        ]
    title = html.esc(f"Run inspector: {_question_text(rec)}")
    return html.panel(title, "".join(parts))


def _inspector_summary(rec: Mapping[str, Any], state: str, now: datetime) -> str:
    legacy_note = " This is a legacy run; artifact and phase detail is limited." if rec.get("legacy") else ""
    sentence = (
        f"{_question_text(rec)} is {state}, profile {_profile_text(rec)}, "
        f"synthesis by {_synthesis_cell(rec)}, started {_age_text(rec, now)}.{legacy_note}"
    )
    discovery = rec.get("discovery_mode")
    extra = ""
    if isinstance(discovery, str) and discovery:
        extra = f'<p class="rs-muted">{html.esc(f"Discovery mode: {discovery}.")}</p>'
    return f"<p>{html.esc(sentence)}</p>{extra}"


def _inspector_findings(show: Mapping[str, Any]) -> str:
    payload = show.get("findings")
    findings = payload.get("findings") if isinstance(payload, Mapping) else None
    if not isinstance(findings, list) or not findings:
        return _sub("Findings", f"<p>{html.esc('No findings recorded.')}</p>")
    records = [item for item in findings if isinstance(item, Mapping)]
    shown = records[:_FINDINGS_LIMIT]
    items = []
    for item in shown:
        title = str(item.get("title") or item.get("claim") or "(untitled finding)")
        trust = str(item.get("trust") or "-")
        summary = str(item.get("summary") or "")
        items.append(
            f"<li><strong>{html.esc(title)}</strong> "
            f'<span class="rs-muted">[{html.esc(trust)}]</span> {html.esc(summary)}</li>'
        )
    note = ""
    if len(records) > len(shown):
        note = f'<p class="rs-muted">{html.esc(f"Showing {len(shown)} of {len(records)} findings.")}</p>'
    return _sub("Findings", f'<ul class="rs-list">{"".join(items)}</ul>{note}')


def _verification_css(verification: str) -> str:
    if verification == "verified":
        return "good"
    if verification == "digest-mismatch":
        return "serious"
    return "warning"


def _inspector_sources(show: Mapping[str, Any]) -> str:
    verification = str(show.get("sources_verification") or "unknown")
    sources = show.get("sources")
    css = html.esc(_verification_css(verification))
    label = f'<p>Source records are <span class="rs-state rs-state-{css}">{html.esc(verification)}</span>.</p>'
    if verification == "digest-mismatch":
        reason = "Source content is withheld because the recorded digest does not match the file on disk."
        return _sub("Sources", label + f"<p>{html.esc(reason)}</p>")
    if not isinstance(sources, list) or not sources:
        reason = {
            "unavailable": "Legacy runs do not carry source envelopes.",
            "missing": "No sources have been persisted for this run.",
        }.get(verification, "No source records are available.")
        return _sub("Sources", label + f"<p>{html.esc(reason)}</p>")
    rows = []
    for item in sources:
        if not isinstance(item, Mapping):
            continue
        excerpt = str(item.get("excerpt") or "")
        truncated = " (truncated)" if item.get("excerpt_truncated") else ""
        excerpt_html = (
            f'<details class="rs-debug"><summary>{html.esc("Excerpt" + truncated)}</summary>'
            f"<pre>{html.esc(excerpt)}</pre></details>"
            if excerpt
            else "-"
        )
        rows.append(
            [
                html.esc(item.get("provider") or "-"),
                html.esc(item.get("origin") or "-"),
                html.esc(item.get("trust") or "-"),
                html.esc(item.get("uri") or "-"),
                html.esc(item.get("observed_model") or "unverified"),
                excerpt_html,
            ]
        )
    table = html.table(
        [
            html.esc("Provider"),
            html.esc("Origin"),
            html.esc("Trust"),
            html.esc("URI"),
            html.esc("Model"),
            html.esc("Content"),
        ],
        rows,
    )
    return _sub("Sources", label + table)


def _inspector_citations(show: Mapping[str, Any], state: str) -> str:
    audit = show.get("citation_audit")
    if not isinstance(audit, Mapping):
        message = (
            "No citation audit recorded for this run."
            if state == "completed"
            else "No citation audit yet; the run has not reached review."
        )
        return _sub("Citations", f'<p class="rs-muted">{html.esc(message)}</p>')
    accepted = bool(audit.get("accepted"))
    citations = audit.get("citations") if isinstance(audit.get("citations"), list) else []
    unresolved = audit.get("unresolved") if isinstance(audit.get("unresolved"), list) else []
    chip = _state_chip("accepted" if accepted else "rejected", "good" if accepted else "serious")
    body = [
        f"<p>Citation audit {chip}: {html.esc(str(len(citations)))} citation(s), "
        f"<strong>{html.esc(str(len(unresolved)))}</strong> unresolved.</p>"
    ]
    if unresolved:
        items = "".join(f"<li><code>{html.esc(str(token))}</code></li>" for token in unresolved[:20])
        body.append(f'<ul class="rs-list">{items}</ul>')
    return _sub("Citations", "".join(body))


def _inspector_fallbacks(rec: Mapping[str, Any]) -> str:
    fallbacks = rec.get("fallbacks")
    if not isinstance(fallbacks, list) or not fallbacks:
        return _sub("Fallbacks", f"<p>{html.esc('No provider fallbacks were recorded.')}</p>")
    items = []
    for item in fallbacks:
        if not isinstance(item, Mapping):
            continue
        phase = str(item.get("phase") or "-")
        pair = f"{item.get('from_seat') or '?'} -> {item.get('to_seat') or '?'}"
        kind = str(item.get("failure_kind") or "unspecified")
        items.append(
            f"<li>{html.esc(pair)} <span class='rs-muted'>during {html.esc(phase)} ({html.esc(kind)})</span></li>"
        )
    return _sub("Fallbacks", f'<ul class="rs-list">{"".join(items)}</ul>')


def _inspector_report(show: Mapping[str, Any]) -> str:
    report = show.get("report") if isinstance(show.get("report"), Mapping) else {}
    verification = str(report.get("verification") or "unknown")
    css = "good" if verification == "verified" else "warning"
    label = f'<p>Final report is <span class="rs-state rs-state-{html.esc(css)}">{html.esc(verification)}</span>.</p>'
    content = report.get("content")
    body = ""
    if isinstance(content, str) and content:
        truncated = " (truncated)" if report.get("truncated") else ""
        body = (
            f'<details class="rs-debug"><summary>{html.esc("Report body" + truncated)}</summary>'
            f"<pre>{html.esc(content)}</pre></details>"
        )
    elif verification != "verified":
        body = f'<p class="rs-muted">{html.esc("Report content is withheld until the digest verifies.")}</p>'
    digest = report.get("digest")
    digest_html = ""
    if isinstance(digest, str) and digest:
        digest_html = (
            f'<details class="rs-debug"><summary>{html.esc("Report digest")}</summary>'
            f"<code>{html.esc(digest)}</code></details>"
        )
    return _sub("Report", label + body + digest_html)


def _inspector_receipt(show: Mapping[str, Any], rec: Mapping[str, Any]) -> str:
    blob = {
        "run": dict(rec),
        "artifact_verification": show.get("artifact_verification"),
        "sources_verification": show.get("sources_verification"),
    }
    try:
        raw = json.dumps(blob, indent=2, sort_keys=True, default=str)
    except (TypeError, ValueError):
        raw = "receipt not serializable"
    return (
        f'<details class="rs-debug"><summary>{html.esc("Receipt and artifact references")}</summary>'
        f"<pre>{html.esc(raw)}</pre></details>"
    )


def _sub(title: str, inner: str) -> str:
    return f'<section class="rs-sub"><h3 class="rs-sub-title">{html.esc(title)}</h3>{inner}</section>'


def _state_css(state: str) -> str:
    if state in {"active", "completed"}:
        return "good"
    if state == "blocked":
        return "warning"
    if state == "failed":
        return "serious"
    return "neutral"


def _state_chip(text: str, css: str) -> str:
    return f'<span class="rs-state rs-state-{html.esc(css)}">{html.esc(text)}</span>'


def _tiles(items: list[tuple[str, str, str, str]]) -> str:
    tiles: list[str] = []
    for label, word, role, meaning in items:
        icon = "✓" if role == "good" else "-" if role == "neutral" else "!"
        tiles.append(
            f'<article class="rs-tile rs-tile-{html.esc(role)}">'
            f'<div class="rs-tile-head">'
            f'<span class="rs-chip-icon rs-chip-{html.esc(role)}" aria-hidden="true">{html.esc(icon)}</span>'
            f'<span class="rs-tile-label">{html.esc(label)}</span>'
            f'<span class="rs-tile-word">{html.esc(word)}</span>'
            f"</div>"
            f'<p class="rs-tile-meaning">{html.esc(meaning)}</p>'
            "</article>"
        )
    return f'<div class="rs-tiles">{"".join(tiles)}</div>'


def _stylesheet(nonce: str) -> str:
    return f"""<style nonce="{html.esc(nonce)}">
.rs-summary {{
  margin: 0 0 1rem;
  padding: 0.85rem 1rem;
  border: 1px solid {_BORDER};
  border-radius: 0.35rem;
  background: {_SURFACE};
  color: {_TEXT_PRIMARY};
  max-width: 72rem;
}}
.rs-summary p {{ margin: 0; }}
.rs-tiles {{
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(13rem, 1fr));
  gap: 0.75rem;
  margin-bottom: 1rem;
}}
.rs-tile {{
  border: 1px solid {_BORDER};
  border-radius: 0.35rem;
  padding: 0.75rem;
  background: {_SURFACE};
}}
.rs-tile-head {{
  display: flex;
  align-items: center;
  gap: 0.45rem;
  margin-bottom: 0.35rem;
}}
.rs-tile-label {{ font-weight: 600; flex: 1; }}
.rs-tile-word {{ font-size: 0.95rem; font-weight: 700; }}
.rs-tile-meaning {{ margin: 0; color: {_TEXT_SECONDARY}; font-size: 0.92rem; }}
.rs-chip-icon {{
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 1.1rem;
  height: 1.1rem;
  border-radius: 999px;
  color: #fff;
  font-size: 0.7rem;
}}
.rs-chip-good {{ background: {_STATUS_GOOD}; }}
.rs-chip-warning {{ background: {_STATUS_WARNING}; }}
.rs-chip-serious {{ background: {_STATUS_SERIOUS}; }}
.rs-chip-neutral {{ background: {_TEXT_SECONDARY}; }}
.rs-state {{
  display: inline-block;
  padding: 0 0.4rem;
  border-radius: 999px;
  font-size: 0.8rem;
  font-weight: 700;
  color: #fff;
}}
.rs-state-good {{ background: {_STATUS_GOOD}; }}
.rs-state-warning {{ background: {_STATUS_WARNING}; }}
.rs-state-serious {{ background: {_STATUS_SERIOUS}; }}
.rs-state-neutral {{ background: {_TEXT_SECONDARY}; }}
.rs-pipeline {{
  list-style: none;
  display: flex;
  flex-wrap: wrap;
  gap: 0.35rem;
  margin: 0 0 0.75rem;
  padding: 0;
}}
.rs-phase {{
  padding: 0.15rem 0.6rem;
  border-radius: 999px;
  border: 1px solid {_BORDER};
  font-size: 0.85rem;
  background: {_SURFACE};
  color: {_TEXT_SECONDARY};
}}
.rs-phase-done {{ border-color: {_STATUS_GOOD}; color: {_TEXT_PRIMARY}; }}
.rs-phase-running {{ border-color: #0066cc; color: #0066cc; font-weight: 700; }}
.rs-phase-failed {{ border-color: {_STATUS_SERIOUS}; color: {_STATUS_SERIOUS}; font-weight: 700; }}
.rs-run-head {{ margin: 0 0 0.35rem; }}
.rs-reasons {{ margin: 0 0 0.75rem; padding-left: 1.2rem; color: {_TEXT_SECONDARY}; }}
.rs-list {{ margin: 0; padding-left: 1.2rem; }}
.rs-list li {{ margin-bottom: 0.3rem; }}
.rs-muted {{ color: {_TEXT_SECONDARY}; font-size: 0.92rem; }}
.rs-sub {{ margin: 0 0 1rem; }}
.rs-sub-title {{ margin: 0 0 0.4rem; font-size: 0.95rem; }}
.rs-debug {{ margin: 0.15rem 0 0; font-size: 0.85rem; color: {_TEXT_SECONDARY}; }}
.rs-debug pre {{
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  max-height: 24rem;
  overflow-y: auto;
}}
</style>"""
