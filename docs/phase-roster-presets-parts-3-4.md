# Roster Presets Follow-ups (Issue #444 Parts 3–4)

Parts 1–2 ship bundled roster presets, per-seat metadata (`purpose`, `spec`, `requires`, `fallback`, `stats`, `caveats`), and `roster_resolution.resolve_seats()` for capability-aware fallback chains. This note plans only the remaining slices.

## Part 3: `brigade roster suggest`

Probe the host with the same CLI and auth preflights used by seat resolution, load a chosen bundled preset, and emit a concrete roster TOML plus a report of dropped or substituted seats and why.

- Input: preset id (`minimal`, `budget-open-weight`, `review-heavy`, `full-multi-lane`) and optional output path.
- Behavior: call `resolve_seats()` for every non-orchestrator seat in preset order; write the resolved seat names (or omit dropped seats) into a generated roster; print the resolution report to stderr or `--json`.
- Out of scope here: mutating the operator's live `~/.brigade/roster.toml` without an explicit write flag.

## Part 4: `brigade roster stats`

Refresh preset `stats` annotations from local evidence instead of author defaults.

- Input: optional roster path and receipt root (default `.brigade/runs/`).
- Behavior: scan worker run receipts for per-seat duration medians, failure rates, and sample counts; emit a table or JSON summary; optionally write refreshed `stats.source` values back into a working copy of a preset or user roster.
- Preset defaults (`source = "author-receipts-2026-07"`) remain the shipped baseline until local receipts override them.

## Separately owned (excluded from this issue)

- **`brigade roster doctor` / `doctor.py`**: subscription and seat health warnings stay in the doctor command; this issue does not add preset-aware doctor checks.
- **`build_context`**: context assembly for harness runs is unrelated to preset loading or resolution.
