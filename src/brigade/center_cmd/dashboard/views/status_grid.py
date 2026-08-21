"""Status dashboard view: what needs my attention across handoffs, memory
care, verify loop, and inbox hygiene right now?

Follows the Memory Operations baseline: a plain-sentence summary strip plus
health tiles whose chips pair an icon with a plain-word status (never color
alone). Raw ids stay in ``<details>`` expanders. Missing or slow sub-fetches
degrade to readable tiles rather than an empty table.
"""

from __future__ import annotations

from pathlib import Path

from brigade.center_cmd.dashboard import data
from brigade.center_cmd.dashboard import render as html

NAME = "status"
TITLE = "Status"
ORDER = 1

# Status palette (dataviz reserved): always paired with icon + word, never color alone.
_STATUS_GOOD = "#0ca30c"
_STATUS_WARNING = "#fab219"
_STATUS_SERIOUS = "#ec835a"
_TEXT_PRIMARY = "#0b0b0b"
_TEXT_SECONDARY = "#52514e"
_SURFACE = "#fcfcfb"
_BORDER = "rgba(11,11,11,0.10)"

_TILES = (
    ("handoff_issues", "Handoffs"),
    ("memory_care", "Memory care"),
    ("outcome_loop", "Verify loop"),
    ("inbox_hygiene", "Inbox hygiene"),
    ("pending_tasks", "Pending tasks"),
)


def fetch(target: Path) -> dict:
    return data.run_json(target, ["work", "brief"], timeout=60.0)


def render(payload: dict, nonce: str) -> str:
    if payload.get("error"):
        return html.error_panel(TITLE, str(payload["error"]))
    if not payload:
        return html.panel(html.esc(TITLE), f"<p>{html.esc('Nothing here.')}</p>")

    parts = [
        _stylesheet(nonce),
        _summary_strip(payload),
        html.panel(html.esc(TITLE), _health_tiles(payload)),
    ]
    return "".join(parts)


def _section(payload: dict, name: str) -> dict:
    value = payload.get(name)
    return value if isinstance(value, dict) else {}


def _int(section: dict, name: str) -> int | None:
    value = section.get(name)
    return value if isinstance(value, int) else None


def _pending_task_count(payload: dict) -> int | None:
    tasks = payload.get("pending_tasks")
    return len(tasks) if isinstance(tasks, list) else None


def _tile_counts(payload: dict, key: str) -> tuple[int | None, dict]:
    """Headline count + raw section for a tile; missing keys degrade, never raise."""
    if key == "handoff_issues":
        section = _section(payload, key)
        count = _int(section, "count")
        return (_int(section, "total_count") if count is None else count), section
    if key == "memory_care":
        section = _section(payload, key)
        return _int(section, "issue_count"), section
    if key == "inbox_hygiene":
        section = _section(payload, key)
        return _int(section, "issue_count"), section
    if key == "outcome_loop":
        section = _section(payload, key)
        runs = _int(section, "verify_run_count")
        records = _int(section, "record_count")
        if runs is not None:
            return runs, section
        return records, section
    if key == "pending_tasks":
        return _pending_task_count(payload), {}
    return None, {}


def _status_word(section: dict, count: int | None) -> str:
    """Plain operator word for a chip: OK / ATTENTION / MISSING / TIMED OUT."""
    if bool(section.get("timed_out")):
        return "TIMED OUT"
    if count is None:
        return "MISSING"
    return "ATTENTION" if count > 0 else "OK"


def _status_role(word: str) -> tuple[str, str, str]:
    """Map a plain status word to (role, icon, color); never color alone."""
    if word == "OK":
        return ("good", "\u2713", _STATUS_GOOD)
    if word == "TIMED OUT":
        return ("serious", "!", _STATUS_SERIOUS)
    if word == "MISSING":
        return ("serious", "?", _STATUS_SERIOUS)
    return ("warning", "!", _STATUS_WARNING)


def _plural(n: int, one: str, many: str) -> str:
    noun = one if n == 1 else many
    return f"{n} {noun}"


def _top_issue_detail(section: dict) -> str:
    top = section.get("top_issue") if isinstance(section.get("top_issue"), dict) else {}
    detail = str(top.get("detail") or "").strip()
    return detail


def _tile_meaning(key: str, section: dict, count: int | None) -> str:
    if key == "handoff_issues":
        if count is None:
            return "Handoff issue counts are not available."
        known = _int(section, "known_count")
        suffix = ""
        if isinstance(known, int) and known > 0:
            suffix = f" {_plural(known, 'known issue', 'known issues')} already tracked."
        if count <= 0:
            return "No new handoff issues." + suffix
        return f"{_plural(count, 'new handoff issue needs', 'new handoff issues need')} review.{suffix}"
    if key == "memory_care":
        if count is None:
            return "Memory-care health is not available."
        if count <= 0:
            return "No memory cards are waiting on care."
        base = f"{_plural(count, 'card needs', 'cards need')} memory care."
        detail = _top_issue_detail(section)
        return f"{base} Top issue: {detail}." if detail else base
    if key == "outcome_loop":
        if count is None:
            return "Verify-loop counts are not available."
        eligible = _int(section, "eligible_receipt_count")
        ineligible = _int(section, "ineligible_receipt_count")
        if isinstance(eligible, int) and isinstance(ineligible, int) and eligible + ineligible > 0:
            rate = ineligible * 100 // (eligible + ineligible)
            return f"{_plural(count, 'verify run', 'verify runs')}; {rate}% of receipts were ineligible."
        return f"{_plural(count, 'verify run', 'verify runs')} recorded."
    if key == "inbox_hygiene":
        if count is None:
            return "Inbox hygiene status is not available."
        if count <= 0:
            return "The work inbox is clean."
        base = f"{_plural(count, 'inbox hygiene issue found', 'inbox hygiene issues found')}."
        detail = _top_issue_detail(section)
        return f"{base} Top issue: {detail}." if detail else base
    if key == "pending_tasks":
        if count is None:
            return "Task ledger state is not available."
        if count <= 0:
            return "No pending tasks."
        return f"{_plural(count, 'task is', 'tasks are')} pending in the ledger."
    return "See details below."


def _raw_details(section: dict) -> str:
    bits: list[str] = []
    for sort_key in sorted(section):
        value = section[sort_key]
        if isinstance(value, (dict, list)):
            continue
        if value not in (None, ""):
            bits.append(f"{sort_key}={value}")
    if not bits:
        return ""
    return (
        f'<details class="sg-debug"><summary>{html.esc("Raw signal")}</summary>'
        f"<code>{html.esc(', '.join(bits))}</code></details>"
    )


def _raw_ids_details(items: list) -> str:
    ids = [str(t.get("id")) for t in items if isinstance(t, dict) and t.get("id") is not None]
    if not ids:
        return ""
    shown = ", ".join(ids[:10]) + (f", +{len(ids) - 10} more" if len(ids) > 10 else "")
    return f'<details class="sg-debug"><summary>{html.esc("Raw ids")}</summary><code>{html.esc(shown)}</code></details>'


def _health_tiles(payload: dict) -> str:
    tiles = []
    for key, label in _TILES:
        count, section = _tile_counts(payload, key)
        word = _status_word(section, count)
        role, icon, color = _status_role(word)
        meaning = _tile_meaning(key, section, count)
        debug = ""
        if key == "pending_tasks":
            tasks = payload.get("pending_tasks")
            if isinstance(tasks, list):
                debug = _raw_ids_details(tasks)
        elif section:
            debug = _raw_details(section)
        tiles.append(
            f'<article class="sg-tile sg-tile-{html.esc(role)}" data-sg-tile="{html.esc(key)}">'
            '<div class="sg-tile-head">'
            '<span class="sg-chip-icon" aria-hidden="true" '
            f'style="background:{html.esc(color)}">{html.esc(icon)}</span>'
            f'<span class="sg-tile-label">{html.esc(label)}</span>'
            f'<span class="sg-chip-word">{html.esc(word)}</span>'
            "</div>"
            f'<p class="sg-tile-meaning">{html.esc(meaning)}</p>'
            f"{debug}"
            "</article>"
        )
    return f'<div class="sg-tiles">{"".join(tiles)}</div>'


def _summary_strip(payload: dict) -> str:
    sentences: list[str] = []

    new_handoffs = _tile_counts(payload, "handoff_issues")[0]
    if new_handoffs is None:
        sentences.append("Handoff issue counts are unavailable.")
    elif new_handoffs > 0:
        noun = "issue is" if new_handoffs == 1 else "issues are"
        sentences.append(f"{new_handoffs} new handoff {noun} waiting.")
    else:
        sentences.append("No new handoff issues.")

    care_issues = _tile_counts(payload, "memory_care")[0]
    if care_issues is None:
        sentences.append("Memory-care health is unavailable.")
    elif care_issues > 0:
        noun = "card is" if care_issues == 1 else "cards are"
        sentences.append(f"{care_issues} {noun} waiting on care review.")
    else:
        sentences.append("No cards are waiting on care review.")

    inbox_issues = _tile_counts(payload, "inbox_hygiene")[0]
    if inbox_issues is None:
        sentences.append("Inbox hygiene status is unavailable.")
    elif inbox_issues > 0:
        noun = "issue" if inbox_issues == 1 else "issues"
        sentences.append(f"The work inbox has {inbox_issues} hygiene {noun} open.")
    else:
        sentences.append("The work inbox is clean.")

    body = " ".join(sentences[:3])
    return (
        f'<section class="sg-summary" data-sg-summary="1" aria-label="{html.esc("Attention summary")}">'
        f"<p>{html.esc(body)}</p>"
        "</section>"
    )


def _stylesheet(nonce: str) -> str:
    return f"""<style nonce="{html.esc(nonce)}">
.sg-summary {{
  margin: 0 0 1rem;
  padding: 0.85rem 1rem;
  border: 1px solid {_BORDER};
  border-radius: 0.35rem;
  background: {_SURFACE};
  color: {_TEXT_PRIMARY};
}}
.sg-summary p {{ margin: 0; color: {_TEXT_PRIMARY}; }}
.sg-tiles {{
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(14rem, 1fr));
  gap: 0.75rem;
}}
.sg-tile {{
  border: 1px solid {_BORDER};
  border-radius: 0.35rem;
  padding: 0.75rem;
  background: {_SURFACE};
}}
.sg-tile-head {{
  display: flex;
  align-items: center;
  gap: 0.45rem;
  margin-bottom: 0.35rem;
  color: {_TEXT_PRIMARY};
}}
.sg-tile-label {{
  font-weight: 600;
  color: {_TEXT_PRIMARY};
  flex: 1;
}}
.sg-tile-meaning {{
  margin: 0;
  color: {_TEXT_SECONDARY};
  font-size: 0.92rem;
}}
.sg-chip-word {{
  color: {_TEXT_PRIMARY};
  font-size: 0.75rem;
  font-weight: 700;
  letter-spacing: 0.02em;
}}
.sg-chip-icon {{
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 1.1rem;
  height: 1.1rem;
  border-radius: 999px;
  color: #fff;
  font-size: 0.7rem;
  line-height: 1;
}}
.sg-debug {{
  margin-top: 0.35rem;
  color: {_TEXT_SECONDARY};
  font-size: 0.85rem;
}}
.sg-debug summary {{ cursor: pointer; color: {_TEXT_SECONDARY}; }}
.sg-debug code {{ color: {_TEXT_PRIMARY}; word-break: break-word; }}
</style>"""
