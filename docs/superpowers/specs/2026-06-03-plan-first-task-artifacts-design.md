# Plan-First: Task Plan Artifacts Design

> **In plain terms:** make planning a real, saved step. Today `brigade work task plan <id>` only prints a task's acceptance criteria. This turns it into a planning step that writes a local plan artifact, a plan.md plus a JSON receipt, capturing the source context, assumptions, acceptance criteria, risks, and the next safe command before any building happens. The operator reviews and accepts the plan; nothing executes from it automatically.

Slice 1 of the Plan-First Operator Loop phase. Inspiration: agentic-engineering "plan.md + plan-gate" workflows (Matt Van Horn, "Every Agentic Engineering Hack I Know"). Plan artifacts are the load-bearing primitive the rest of the phase (plan-for-the-plan, research-as-evidence, plan-state surfacing, plan-to-template promotion) builds on.

## Goal

Extend `brigade work task plan <id>` to optionally write a structured, human-readable plan artifact per task, and add a way to list plan artifacts. Keep the existing print behavior as the default (no breaking change).

## Background

- `work_cmd.task_plan(target, task_id, json_output)` (work_cmd.py:6382) calls `_task_plan_payload` (6804) and prints task type/priority/acceptance/suggested_command. It is read-only and writes nothing.
- CLI wiring: `cli.py:1277` (`task plan` parser, args `task_id`, `--target`, `--json`) and dispatch at `cli.py:3488`.
- Work artifacts live under `_work_root(target)` = `.brigade/work/` (already gitignored via the install gitignore block: `.brigade/work/`). Tasks live at `.brigade/work/tasks.json`.
- `_find_task`, `_task_summary`, `_task_acceptance` already resolve a task and its acceptance list.
- Existing list/show pattern to mirror: `work sweeps` / `work sweep-show` (`work_cmd.sweeps`/`sweep_show`, cli dispatch 3144-3147).

## Non-Goals

- Surfacing missing-plan warnings in `work brief` / `work doctor` / release readiness (that is slice 5).
- Plan-for-the-plan / meta-plan mode (slice 2).
- Feeding research reports into plans (slice 4).
- Promoting plans to templates/rules/skills (slice 6).
- Any execution from a plan. Plans are documents; `brigade work run` is unchanged.
- New dependencies. Standard library only.

## Architecture

### Plan artifact storage

Under `.brigade/work/plans/`:
- `<task-id>.json` - the structured receipt (the JSON contract).
- `<task-id>.plan.md` - the human-readable plan (the plan.md).

`<task-id>` is the task's full id (already filesystem-safe; tasks use slug/hash ids). A helper `_plans_dir(target)` returns `_work_root(target) / "plans"`; `_plan_paths(target, task_id)` returns the json + md paths.

### Plan receipt schema (JSON)

```json
{
  "task_id": "<id>",
  "title": "<task text or --title>",
  "status": "draft | accepted",
  "created_at": "<iso8601 utc>",
  "updated_at": "<iso8601 utc>",
  "source_context": ["<ref/link/note>", "..."],
  "assumptions": ["..."],
  "acceptance": ["<pulled from the task>", "..."],
  "risks": ["..."],
  "next_command": "<safe next command, default 'brigade work run'>",
  "receipt_paths": ["<.brigade/work/tasks.json>", "<plan json>", "<plan md>"]
}
```

- `acceptance` is sourced from the task (`_task_acceptance`) at write time; not hand-entered.
- `receipt_paths` are repo-relative strings, filled automatically.
- Timestamps use `datetime.now(timezone.utc)` (same as ingest/receipts elsewhere). On update, `created_at` is preserved and `updated_at` refreshed.

### plan.md rendering

A dependency-free markdown render:

```markdown
# Plan: <title>

- **Task:** <task_id>
- **Status:** draft
- **Updated:** <iso>

## Source context
- <item>            (or "_none recorded_")

## Assumptions
- <item>            (or "_none recorded_")

## Acceptance criteria
- <item>            (or "_none recorded_")

## Risks
- <item>            (or "_none recorded_")

## Next safe command
`<next_command>`

## Receipts
- <path>
```

### Commands

1. `brigade work task plan <id>` (unchanged default): prints the existing task/acceptance view, plus a new trailing `plan_artifact:` line stating whether an artifact exists and, if so, its status and path. The JSON payload (`--json`) gains a `plan_artifact` key: `null` if none, else `{status, path, updated_at}`. This is the only change to existing output and is purely additive.

2. `brigade work task plan <id> --write [flags]`: create or update the artifact for the task, then print a confirmation (or the receipt JSON with `--json`). Flags:
   - `--assumption TEXT` (repeatable, append)
   - `--risk TEXT` (repeatable, append)
   - `--source TEXT` (repeatable, append; source context refs/links/notes)
   - `--next-command TEXT` (default `brigade work run`)
   - `--title TEXT` (default: the task text)
   - `--accept` (set status to `accepted`; otherwise `draft`)
   On update, repeatable flags append to existing lists (deduped, order-preserving); `acceptance` is always re-pulled from the task; `--accept` flips status; omitting a scalar keeps the prior value.

3. `brigade work plans` (new): list plan artifacts (task_id, status, updated_at, path), newest first. `--json` returns the list.

## Data Flow

```
brigade work task plan T1 --write --source "issue #42" --assumption "API stable" --risk "rate limits"
   └─ _find_task -> _task_acceptance -> build/merge receipt -> write .json + .plan.md
brigade work task plan T1            -> existing view + "plan_artifact: draft (.brigade/work/plans/T1.plan.md)"
brigade work plans                   -> table of all plan artifacts
brigade work task plan T1 --write --accept   -> status: accepted
```

## Error Handling

- `--write` for an unknown task id: same `error: task not found` + exit 1 as the current `task_plan`.
- `--target` not a directory: exit 2 (existing behavior).
- A repeatable flag passed without `--write` is ignored with a one-line note on stderr (the flags only apply to writes); the read view still renders. (Argparse accepts them regardless; the handler enforces write-only semantics.)
- Malformed/partial existing plan json (hand-edited): on update, missing keys default to empty; never crash. On read/list, a json that fails to parse is reported as `status: unreadable` rather than crashing the listing.
- `work plans` with no `plans/` dir: prints "no plan artifacts" (text) / `[]` (json), exit 0.

## Testing

- `_plan_paths` / `_plans_dir` resolve under `.brigade/work/plans/`.
- `--write` creates both `<id>.json` and `<id>.plan.md`; json has all schema keys; acceptance equals the task's acceptance; `created_at == updated_at` on first write; `receipt_paths` includes tasks.json + both plan files.
- Second `--write` appends assumptions/risks/source (deduped), preserves `created_at`, bumps `updated_at`, and `--accept` sets `status: accepted`.
- plan.md contains the title, each section heading, and the entered items; empty sections render `_none recorded_`.
- `task plan <id>` (no --write) on a task with an artifact prints a `plan_artifact:` line and the JSON payload has a non-null `plan_artifact`; without an artifact it is `null` and output is otherwise unchanged (assert the pre-existing acceptance lines still print).
- `work plans` lists written artifacts newest-first; `--json` shape; empty case.
- Unknown task id with `--write` exits 1.
- Full suite stays green.

## Rollout

- Branch `feat/plan-first-task-artifacts` off main (roadmap section already committed there).
- Flip the ROADMAP Plan-First bullet "Treat `brigade work task plan <id>` as the visible planning step..." to note it is implemented (plan artifacts: write/list, plan.md + JSON receipt). Leave the other bullets `proposed`.
- CHANGELOG: Unreleased / Added. README: document `work task plan --write` / `work plans` if the work commands are enumerated. Regenerate `docs/command-inventory.md` (new `work plans` command + `task plan` flags) via `python3 -m brigade roadmap commands --write` if it tracks commands.
- No release.
