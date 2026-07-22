# Eval cell identity and resume semantics

This document is the contract for `brigade.eval_cell.v1` receipts produced by
`src/brigade/model_trials.py`. It freezes the rules that become compatibility
surfaces at the stable cut: how `cell_id` is derived, what `execute --resume`
does for every recorded state, and how stale cells are handled.

## Cell identity

`cell_id` is the sha256 hex digest of a canonical JSON identity payload
(`json.dumps` with sorted keys, `(",", ":")` separators, UTF-8) built in
`expand_cells`. Exactly these fields participate, and nothing else:

- `schema` — the `CELL_SCHEMA` tag (`brigade.eval_cell.v1`).
- `case.id` — the case identifier from the manifest.
- `case.prompt` — the inlined prompt text, after line-ending normalization
  (see below).
- `seat.seat` — the seat name.
- `seat.cli` — the agent's CLI adapter.
- `seat.model` — the pinned model.
- `seat.reasoning` — the reasoning setting.
- `seat.transport` — the transport, if any.
- `seat.transport_version` — the transport version, if any.
- `seat.env` — the agent's environment map, if any.
- `seat.codex_transport` — the roster codex transport; set for codex seats
  only, otherwise `null`.
- `trial` — the 1-based trial number.
- `graders` — the grader list for the case.
- `execution_mode` — `read-only` or `writable-worktree`.

The `coordinate` (`case:seat:trial`) is the human-stable axis and is **not**
part of the identity payload; it is how staleness is detected (below).

### Line-ending normalization

Before prompt text (inline or from `prompt_file`) enters the identity payload,
`\r\n` and bare `\r` are normalized to `\n`, so the same logical manifest
hashes identically across checkouts with different line-ending conventions.
This changes `cell_id` only for manifests that contained CRLF or CR line
endings; that breakage is accepted because it lands before the stable cut.

### Changing the identity payload

Any change to the field set above — adding, removing, or reinterpreting a
field — requires bumping `CELL_SCHEMA` and writing a migration note in this
document. The identity lock test in `tests/test_model_trials.py` snapshots the
exact payload keys and the resulting digest, so an accidental change fails CI.

## Resume semantics

`execute --resume` rebuilds the plan from the current manifest and decides per
cell from the recorded `cell.json`:

- `accepted`, `rejected`, `unscored`, `execution_error`, `adapter_error`,
  `grader_error` (the terminal states): the cell is **skipped**; the existing
  receipt stands.
- `running`: the cell **re-runs as a new attempt**. `running` means the
  previous process died mid-run (or, without a lock, is still executing in
  another process). Resume treats it as a crash and starts the next attempt,
  preserving the original `started_at`.
- Missing, unreadable, or corrupt `cell.json`: the cell runs as a new attempt.
- Any other state value: the cell re-runs (only exact terminal-state
  membership causes a skip).

Attempt numbers are `max(existing attempt numbers under attempts/) + 1`,
tolerating gaps and non-`attempt-NNN` directories; a deleted attempt directory
never causes a number to be reused.

Every `cell.json` — both the `running` marker written before the run and the
final receipt — records `manifest_digest`, the canonical digest of the
manifest that produced the plan, so each cell stays attributable to its
generation even after later manifest edits.

## Stale cells

A cell is stale when its `coordinate` exists in the previous `plan.json` with
a different `cell_id` (for example after a manifest edit). Staleness is
computed against the immediately previous `plan.json` only.

The policy is **keep and report, no pruning**:

- Stale cell directories are left on disk untouched.
- The new `plan.json` lists them under `stale_cells` with the previous and
  current ids.
- `summarize` excludes them from the headline counts and reports them under
  `stale_counts`.
- On `execute --resume` with stale cells present, a one-line stale count is
  printed to stderr.

## Concurrency

Two concurrent `execute` processes on one output directory are not guarded:
both see a `running` cell and interleave writes. A lockfile-based guard is
tracked separately and deliberately not part of this freeze; until it lands,
do not run concurrent executes against the same output directory.
