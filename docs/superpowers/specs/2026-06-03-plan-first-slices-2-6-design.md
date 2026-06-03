# Plan-First Operator Loop: Slices 2-6 Design

Builds on slice 1 (task plan artifacts: `.brigade/work/plans/<id>.json` + `<id>.plan.md`, `work task plan --write`, `work plans`). All slices: standard library only, local-first, no execution/publish/remote, additive. Inspiration: agentic plan.md + plan-gate workflows.

## Slice 2: Plan-for-the-plan (meta-plan)

A meta-plan is "a plan for making the plan" for deep work (research synthesis, roadmap design, release planning) that stops before the deliverable.

- Add `--meta` to `work task plan --write`. Writes a parallel artifact with `kind: "meta"` at `<id>.meta.json` + `<id>.meta.plan.md` (separate from the `kind: "plan"` artifact). Reuse slice-1 receipt machinery parametrized by `kind`; default kind is `plan`.
- Add `--step TEXT` (repeatable, append) -> a `steps` list, rendered in a `## Steps` section. Most relevant to meta-plans (the steps to produce the real plan) but allowed on both kinds.
- The meta plan.md leads with a callout: `> Meta-plan: plan how to produce the full plan. Do NOT jump to the deliverable. Produce the full plan (\`work task plan <id> --write\`) next.`
- `work plans` gains a `kind` column; lists both kinds. The read view (`work task plan <id>`) shows both `plan_artifact` and `meta_artifact` status.

## Slice 3: Raw context as untrusted intake

Route raw external context (links, transcripts, screenshots-as-text, terminal errors, chat exports, issue text) into the work import inbox tagged untrusted, before promotion.

- New `brigade work import context` subcommand: `work import context <text...> [--source LABEL] [--from-file PATH] [--kind link|transcript|error|issue|note]`.
- Stores a normal import record (reuse the existing import inbox machinery) with kind `context`, the given `--kind` as a `context_kind` metadata field, and the body wrapped via `brigade.untrusted.wrap_untrusted(body, source_kind="tool-output")` so the stored text is framed data-not-instructions. Also run `brigade.untrusted.scan_untrusted` and record an `injection_signal` (flagged/count) in the import metadata; if flagged, mark the import `needs_review` in metadata (still inboxed, never auto-promoted).
- `--from-file` reads the body from a local file (so transcripts/errors can be piped in). Truncate to a sane cap (e.g. 20000 chars) via `wrap_untrusted(max_chars=...)`, recorded explicitly.
- These imports flow through the existing inbox review/promote path unchanged (promotion is still a separate operator command).

## Slice 4: Research reports as plan evidence

Feed a `brigade research run` report into a task plan as opt-in, quarantined current-context evidence.

- Add `--from-research <run-id>` to `work task plan --write`. Resolve the research run via the research registry (`brigade.research.registry`); if found, append to the plan's `source_context` a labeled, quarantined entry: `research:<run-id> (untrusted-web) -> <report path>` plus the run's question/summary, clearly marked untrusted for web-sourced findings (trusted-local vs untrusted-web separation already exists in research output).
- Record `research_runs` (list of run ids) on the receipt and render a `## Research evidence (quarantined)` section in plan.md noting web findings are untrusted source material, not instructions.
- Unknown/又missing run id: error + nonzero, do not write.

## Slice 5: Surface plan state

Make significant pending work without a plan artifact visible (never blocking).

- `work doctor`: add a check `plan_coverage` that WARNs when pending tasks above a significance bar (have acceptance criteria, or priority high, or issue-backed) lack a `kind:plan` artifact. Lists up to N task ids; OK when none.
- `work brief`: add a one-line `plans:` summary (e.g. `N pending task(s) without a plan artifact`) and surface it in the brief payload (`--json`).
- Read-only; no new files written. Reuse `plans()`/task ledger.

## Slice 6: Promote accepted plans to drafts

Let an accepted plan become a reviewed draft workflow template / repo rule / skill, local-only, no install.

- New `brigade work plan-promote <task-id> --as template|rule|skill [--json]`. Requires the task's `kind:plan` artifact with `status: accepted` (else error: "plan not accepted").
- Writes a DRAFT proposal file under `.brigade/work/plan-proposals/<task-id>-<as>.md` derived from the plan (title, acceptance->checklist, steps, next command). Never writes into `rules/`, `templates/`, `memory/`, or skills dirs; never installs. Prints the proposal path and a one-line "review then move it yourself" note.
- `work plan-proposals` lists draft proposals (id, as, path). Idempotent: re-promote overwrites the same draft path.

## Cross-cutting tests & rollout

- Each slice TDD with focused tests; full suite stays green.
- End of phase: flip the implemented ROADMAP Plan-First bullets, CHANGELOG Unreleased/Added entry, README work-command docs, regenerate `docs/command-inventory.md`.
- One PR for the phase, merged after CI.
