# Issue 444 Parts 3 and 4 Plan

Parts 1 and 2 ship packaged presets plus a pure `resolve_capabilities()` helper. Parts 3 and 4 consume that resolver without changing dispatch behavior in this branch.

## Part 3: Best-of-available assembly command

- Add `brigade roster suggest` (or equivalent) that loads a chosen preset, calls `HostCapabilityProbe` and `resolve_capabilities()`, and prints every `SeatResolution` entry with requested seat, outcome, resolved seat, and reason.
- Check `result.usable` before emission. When the orchestrator was dropped, print the report and refuse to emit an adoptable roster.
- Emit the resolved roster TOML (or a concrete path) when the result is usable so operators can adopt the assembled result.
- Reuse the same report in `brigade roster doctor` to warn when a seat's declared requirements no longer hold on the host.

## Part 4: Evidence-backed stats overlay

- Add `brigade roster stats` that reads worker receipt durations and statuses from `.brigade/runs/`.
- Compute per-seat medians and failure rates from local evidence.
- Overlay those stats in suggest/doctor output without mutating the packaged preset files; shipped `stats.source` remains the author default until local receipts override it in rendered output.
