# Design: Wire agentpantry into Brigade as the `pantry` station

Date: 2026-06-01
Status: Approved (design phase)

## Problem

`agentpantry` (browser-session auth sync, source -> sink) was extracted into its
own repo alongside the other satellites, but unlike `memory-doctor` and
`bootstrap-doctor` it was never registered with Brigade. Nothing in Brigade can
install, wire, or health-check it, and it does not map onto any existing station
(`core`, `memory`, `guard`, `tokens`, `security`).

We want agentpantry to behave like the established satellites: its own repo,
independently usable (`go install`), but registered with Brigade as a managed
tool so `brigade add`/`brigade doctor` know about it and the two move together at
release time.

## Established pattern (for reference)

Brigade is organized into **stations**; each station may own **managed tools** -
external CLIs Brigade installs/wires/health-checks by shelling out via
`brigade.proc`. The core never imports them. Absent tools report `MANUAL`, never a
hard failure.

- `memory-doctor` / `bootstrap-doctor`: Python, own repos under the
  `escoffier-labs` org, `pipx`-installable, depend on `brigade-cli` as a *library*
  (e.g. `from brigade.budgets import ...`), registered on the `memory` station.
- Doctor loop: for each station, run `station.doctor(ctx)`, then for each managed
  tool `for_station(...)`, if `tool.detect()`, run `tool.doctor(ctx)`
  (`doctor.py:160-163`).
- `brigade add <station>` installs each station tool (`install_args`) and runs its
  `wire` (`add.py`).

## Key constraint: agentpantry is Go

The Python satellites couple to Brigade two ways:

1. **Brigade -> tool:** shell-out for health checks - language-agnostic, fine for Go.
2. **tool -> Brigade:** a *library* dependency (`brigade-cli`) to consume canonical
   definitions - **impossible for a Go binary.**

agentpantry's cookie/auth concern does not need Brigade's memory/bootstrap budgets,
so we deliberately add **no** tool -> Brigade coupling. "Moves with Brigade" means a
coordinated managed-tool registration and release coordination, not shared source.

## Decisions

- **Repo model:** agentpantry stays its own repo (option #1). No subtree, no
  submodule. Independently `go install`-able.
- **Station:** new `pantry` station, single tool `agentpantry`. Minimal
  station-check fn (like `tokens_station_checks`, returns `[]`); the managed-tool
  doctor carries the signal.
- **Install:** `go install github.com/escoffier-labs/agentpantry/cmd/agentpantry@latest`.
  Requires moving agentpantry to the `escoffier-labs` org.
- **Advisory scope:** agentpantry health checks are operator/host-scoped and never
  FAIL a workspace doctor run (same as the memory satellites).

## The cross-repo contract: `agentpantry status --json`

agentpantry today prints human text and errors if no config. We add a `--json`
flag and an exit-code convention mirroring memory-doctor.

JSON payload (stdout):

```json
{
  "role": "source|sink|",
  "configured": true,
  "peer": "host:port",
  "key_present": true,
  "surfaces": ["sidecar", "chrome", "secrets"],
  "browsers": 2,
  "allow": ["..."],
  "deny": ["..."]
}
```

Exit codes:

- `0` - config present and loaded (healthy)
- `2` - installed but unwired (`config.toml` missing) - mirrors memory-doctor
- `1` - real error (unreadable/invalid config)

Human `status` output is unchanged when `--json` is absent. Config path is
`config.Dir()/config.toml`; a missing file is the unwired state.

## Work breakdown

### agentpantry repo (escoffier-labs)

1. Move repo to the `escoffier-labs` org. Rewrite `go.mod` module path
   `github.com/solomonneas/agentpantry` -> `github.com/escoffier-labs/agentpantry`
   and every internal import path.
2. Add `--json` to `cmdStatus` plus the exit-code convention. Use a distinct
   sentinel for "config missing" so `main` can map it to exit 2 (vs exit 1 for
   other errors). `key_present` = `os.Stat(c.KeyPath)` succeeds.
3. Rebuild; `go install` smoke test; CHANGELOG entry.

### Brigade repo

4. `registry.py`: add `PANTRY` station (`name="pantry"`,
   `summary="agent session auth sync"`, `aliases=("larder",)`,
   `doctor=_doctor.pantry_station_checks`, `tools=("agentpantry",)`); add to
   `_BUILTIN`.
5. `doctor.py`: add `pantry_station_checks(ctx) -> []` (minimal, like tokens).
6. `managed.py`: add the `agentpantry` `ManagedTool` (station `pantry`,
   `install_args=["go", "install",
   "github.com/escoffier-labs/agentpantry/cmd/agentpantry@latest"]`, `_noop_wire`,
   `_agentpantry_doctor`). The doctor shells `agentpantry status --json`: exit 2 ->
   WARN "installed but unwired (no config)"; unreadable JSON -> WARN; else OK/WARN
   with role/surfaces/peer summary. Advisory only (never FAIL).
   **Also fix** the stale `github.com/solomonneas/...` install URLs for
   `memory-doctor` and `bootstrap-doctor` -> `escoffier-labs`.
7. Tests: `test_registry.py` (pantry station present; `tools == ("agentpantry",)`),
   `test_managed.py` (`resolve("agentpantry")`; parse a fake `status --json`;
   exit-2 unwired path).
8. Docs: CHANGELOG `[Unreleased]`; README station table; ROADMAP if it enumerates
   stations.

## Out of scope (YAGNI)

- No git subtree/submodule.
- No `brigade-cli` dependency added to agentpantry.
- No release-binary download pipeline (using `go install`).
- No Brigade-driven wiring config for agentpantry (it is configured by its own
  `init`/`keygen`).

## Risks / notes

- `go install` requires a Go toolchain on the agent box. If absent, the tool
  reports `MANUAL` (install hint), which is the intended graceful degradation.
- The org move changes the module path; downstream anyone pinning the old path
  breaks. agentpantry is pre-1.0 / internal, so acceptable.
- Cross-repo contract must stay in lockstep: the `status --json` shape is consumed
  by `_agentpantry_doctor`; the test in Brigade should pin the shape it expects.
