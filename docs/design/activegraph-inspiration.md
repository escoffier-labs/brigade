# ActiveGraph inspiration: drift oracle + fork/diff

Part of a fleet-wide effort inspired by Yohei Nakajima's
[ActiveGraph](https://github.com/yoheinakajima/activegraph). Master notes:
`~/notes/activegraph-credits.md`. ActiveGraph is a reference architecture only;
no code from it is vendored or depended on here.

## What we borrowed

- **State is a projection of an append-only log.** ActiveGraph's module-level
  `apply_event` (`activegraph/core/graph.py`) is the only mutator, and current
  state is recoverable by replaying the event log. Brigade's outcome ledger
  already has the logs (`records.jsonl` for signals, `decisions/*.json` for
  transitions) but treated `status.json` as an independently-written blob.
- **Fork/diff.** ActiveGraph forks a run by replaying its log under a new run id
  and diffs two runs structurally (`compute_diff`, `runtime/diff.py`).

## What landed in brigade

Three additive `outcome` subcommands, no change to the existing write path:

- `outcome rebuild-status [--check]` — the **drift oracle**. Folds the decision
  receipts (`core.fold_status`) into the status map they should produce and
  diffs it against the persisted `status.json`. `--check` exits non-zero on any
  drift. Read-only; it never rewrites `status.json`.
- `outcome fork --out <file> [--install-min-helped N ...]` — replays the signal
  log through the scorer and ratchet from a clean baseline under a hypothetical
  config (`core.project_statuses`) and writes the resulting per-artifact
  projection. Never touches live state.
- `outcome diff <a> <b>` — structural comparison of two fork projections: which
  artifacts land on a different status/action under different rules.

The payoff: `status.json` is now provably a projection of the transition log
(drift from a hand-edit or a partial write is detectable), and you can answer
"how would raising `install_min_helped` change what promotes?" by forking twice
and diffing, without disturbing the live ledger.

## What we did differently, and why

- **Co-canonical logs, not one.** ActiveGraph has a single event log. Brigade's
  `status.json` is a projection of the **decisions** log, while the signal counts
  come from the **records** log. Signals alone cannot reproduce `status.json`
  because the ratchet is stateful (cooldown via `last_action_ts`, forward-only
  transitions in `decide()`); the decision receipts are the transition log that
  carries that state. So the honest framing here is two co-canonical logs, and
  the drift oracle folds the transition log specifically.
- **Fork is a config-sensitivity replay, not a divergent run.** ActiveGraph forks
  to let a run continue along a new branch. Brigade's ratchet is a pure function
  of (scores, config), so the useful fork is "replay the same log under different
  rules" rather than "continue from a cut point." That is what makes fork/diff a
  what-if tool for the promotion thresholds.
- **Canonical flip deferred.** We deliberately did not demote `status.json` to a
  regenerated cache. The drift oracle makes that flip *safe to consider later*
  (run `--check` in CI for a while first); doing it now would change a
  correctness-critical write path for no immediate gain.

## Deferred (next brigade slice)

The **evidence provenance graph** — unifying the five scattered evidence surfaces
(ledger `evidence_ref`, card `evidence/sources/refs` frontmatter, handoff
`## Evidence` + `scanner_provenance`, verify-run receipts) under one node identity
keyed on receipt path — is the sprawliest piece and touches surfaces owned by
in-flight work. It is scoped as a follow-up read-only projection rather than
rushed here.

## Feedback worth sending Yohei

Brigade is a case where a single event log is not enough: the promotion state is
a stateful ratchet, so reproducing it needs a dedicated **transition** log
(decision receipts) distinct from the **signal** log. ActiveGraph's single-log
model would need a second canonical stream to represent this cleanly, or the
transitions would have to be reconstructable by re-running the ratchet
deterministically over the signals plus a clock — which is exactly the coupling
the separate decision receipts avoid.
