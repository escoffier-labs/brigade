"""Which skills and cards are actually helping vs hurting the loop?"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from brigade import outcome_ratchet_report
from brigade.center_cmd.dashboard import data
from brigade.center_cmd.dashboard import render as html

NAME = "outcomes"
TITLE = "Outcomes"
ORDER = 4

_EMPTY_MESSAGE = (
    "The outcome ranking is empty. The outcome loop is not being fed: "
    "run verification through a registered verifier and capture outcomes."
)
_RATCHET_EMPTY_MESSAGE = (
    "No outcome ratchet report yet. The weekly ratchet writes one with "
    "`brigade outcome reconcile --target . --report --proposals`."
)
_RATCHET_STALE_DAYS = 7


def fetch(target: Path) -> dict:
    payload = data.run_json(target, ["outcome", "rank"])
    payload["ratchet"] = data.run_json(target, ["outcome", "ratchet"])
    return payload


def render(payload: dict, nonce: str) -> str:
    del nonce
    if payload.get("error"):
        return html.error_panel(TITLE, str(payload["error"]))

    return _ranking_panel(payload) + _ratchet_panel(payload.get("ratchet"))


def _ranking_panel(payload: dict) -> str:
    ranking = payload.get("ranking")
    if not isinstance(ranking, list):
        ranking = []
    entries = [entry for entry in ranking if isinstance(entry, dict)]
    if not entries:
        return html.panel(html.esc(TITLE), f"<p>{html.esc(_EMPTY_MESSAGE)}</p>")

    headers = [
        "Artifact",
        "Score",
        "Helped",
        "Hurt",
        "Local trials",
        "Attested trials",
        "Capability score",
        "Capability helped",
        "Capability hurt",
        "Stale records",
        "Legacy records",
    ]
    rows = []
    for entry in entries:
        rows.append(
            [
                html.esc(entry.get("artifact_id", "-")),
                html.esc(_fmt(entry.get("score"))),
                html.esc(_fmt(entry.get("helped"))),
                html.esc(_fmt(entry.get("hurt"))),
                html.esc(_fmt(entry.get("local_trials", 0))),
                html.esc(_fmt(entry.get("attested_trials", 0))),
                html.esc(_fmt(entry.get("capability_score"))),
                html.esc(_fmt(entry.get("capability_helped"))),
                html.esc(_fmt(entry.get("capability_hurt"))),
                html.esc(_fmt(entry.get("stale_records"))),
                html.esc(_fmt(entry.get("legacy_records"))),
            ]
        )
    escaped_headers = [html.esc(header) for header in headers]
    strip = _summary_strip(entries)
    return html.panel(html.esc(TITLE), f"<p>{strip}</p>" + html.table(escaped_headers, rows))


def _proposal_label(summary: dict) -> str:
    """Summarize the inbox proposals the last ratchet run filed (counts persisted in the report)."""
    counts = outcome_ratchet_report.proposal_counts(summary)
    if counts is None:
        return "not requested"
    return ", ".join(f"{key} {counts[key]}" for key in ("filed", "existing", "flagged", "skipped"))


def _ratchet_panel(raw: object) -> str:
    """Render the advisory weekly ratchet report, including how old it is."""
    title = html.esc("Outcome ratchet report")
    ratchet = raw if isinstance(raw, dict) else {}
    if ratchet.get("error"):
        return html.error_panel("Outcome ratchet report", str(ratchet["error"]))
    if not outcome_ratchet_report.has_ratchet(ratchet):
        # No reconcile summary yet, but the scheduled drift check may have stored its result.
        return html.panel(title, f"<p>{html.esc(_RATCHET_EMPTY_MESSAGE)}</p>" + _drift_html(ratchet.get("skill_drift")))

    summary_raw = ratchet.get("summary")
    summary = summary_raw if isinstance(summary_raw, dict) else {}
    generated_at = str(ratchet.get("generated_at") or "-")
    stamp = f"generated_at {generated_at} ({_age_label(ratchet.get('generated_at'))})"
    mode = outcome_ratchet_report.mode_label(ratchet.get("apply"))
    local, attested = outcome_ratchet_report.reported_trial_counts(summary)
    strip = (
        f"{stamp}; mode={mode}; decisions {summary.get('decisions', '-')}, "
        f"regression holds {summary.get('regression_holds', '-')}, "
        f"regression rollbacks {summary.get('regression_rollbacks', '-')}, "
        f"pending physical actions {summary.get('pending_physical_actions') or 0}"
        + (f" (stale {summary['stale_physical_actions']})" if summary.get("stale_physical_actions") else "")
        + f"; inbox proposals {_proposal_label(summary)}; "
        f"ineligibility_rate {_fmt(ratchet.get('ineligibility_rate'))}"
        f", trials local {local} attested {attested} (from report file)"
    )
    leading = ratchet.get("leading_ineligibility_reason")
    if leading:
        strip += f", leading reason {leading}"
    if ratchet.get("truncated"):
        strip += f"; {ratchet.get('regressions_omitted', 0)} regression row(s) omitted by the size cap"

    drift_html = _drift_html(ratchet.get("skill_drift"))
    regressions = ratchet.get("regressions")
    if not isinstance(regressions, list):
        regressions = []
    rows = [row for row in regressions if isinstance(row, dict)]
    if not rows:
        return html.panel(
            title,
            f"<p>{html.esc(strip)}</p><p>{html.esc('No regression holds or rollbacks in the last run.')}</p>"
            + drift_html,
        )

    headers = [
        "Artifact",
        "Prior state",
        "Reason code",
        "Reason",
        "Helped",
        "Hurt",
        "Reported local trials",
        "Reported attested trials",
    ]
    body = []
    for row in rows:
        row_local, row_attested = outcome_ratchet_report.reported_trial_counts(row)
        body.append(
            [
                html.esc(row.get("artifact_id", "-")),
                html.esc(row.get("prior_state", "-")),
                html.esc(row.get("reason_code", "-")),
                html.esc(row.get("reason", "-")),
                html.esc(_fmt(row.get("helped"))),
                html.esc(_fmt(row.get("hurt"))),
                html.esc(row_local),
                html.esc(row_attested),
            ]
        )
    escaped_headers = [html.esc(header) for header in headers]
    return html.panel(title, f"<p>{html.esc(strip)}</p>" + html.table(escaped_headers, body) + drift_html)


def _drift_html(raw: object) -> str:
    """Render the scheduled installed-skill drift result: counts, then every flagged copy with its path."""
    drift = outcome_ratchet_report.drift_payload(raw)
    if drift is None:
        return f"<p>{html.esc('Skill drift: no scheduled check recorded yet.')}</p>"
    counts = drift["counts"]
    generated_at = str(drift.get("generated_at") or "-")
    strip = (
        f"Skill drift: generated_at {generated_at} ({_age_label(drift.get('generated_at'))}); "
        f"current {counts['current']}, refreshed {counts['refreshed']}, flagged {counts['flagged']}"
    )
    if not drift["flagged"]:
        return f"<p>{html.esc(strip)}</p>"
    body = [
        [html.esc(row["skill_id"]), html.esc(row["harness"]), html.esc(row["path"]), html.esc(row["reason"])]
        for row in drift["flagged"]
    ]
    if drift["flagged_omitted"]:
        strip += f"; {drift['flagged_omitted']} flagged row(s) not listed"
    headers = [html.esc(header) for header in ("Skill", "Harness", "Path", "Reason")]
    return f"<p>{html.esc(strip)}</p>" + html.table(headers, body)


def _age_label(generated_at: object) -> str:
    """Describe report age so a stale weekly run reads as stale, never current."""
    if not isinstance(generated_at, str):
        return "age unknown"
    try:
        stamp = datetime.fromisoformat(generated_at)
    except ValueError:
        return "age unknown"
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    hours = (datetime.now(timezone.utc) - stamp).total_seconds() / 3600
    if hours < 0:
        return "age unknown"
    days = int(hours // 24)
    if days >= _RATCHET_STALE_DAYS:
        return f"stale, {days}d old"
    return f"{days}d ago" if days else f"{int(hours)}h ago"


def _summary_strip(entries: list[dict]) -> str:
    def net(entry: dict) -> int:
        try:
            return int(entry.get("helped", 0)) - int(entry.get("hurt", 0))
        except (TypeError, ValueError):
            return 0

    best = max(entries, key=net)
    worst = min(entries, key=net)
    best_id = html.esc(best.get("artifact_id", "-"))
    worst_id = html.esc(worst.get("artifact_id", "-"))
    if best is worst:
        return f"Top entry: {best_id} (net {net(best):+d})."
    return (
        f"{best_id} is the biggest net help (net {net(best):+d}); "
        f"{worst_id} is the biggest net drag (net {net(worst):+d})."
    )


def _fmt(value: object) -> object:
    """Format a cell value; floats get 3 decimals, missing keys a dash."""
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.3f}"
    return value
