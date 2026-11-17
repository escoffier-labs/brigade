---
status: active
proof: drive
surfaces: t3 open
drive: t3-enrollment
last-drive: 2026-11-17 ee4ce03b0
---

# Native T3 enrollment (`brigade t3 open`)

`brigade t3 open <batch.json> --target <workspace>` validates a
`brigade.run_batch.v1` batch, binds every task seat to a declared
`[t3_targets.<seat>]` roster binding, retains each task's exact delegation
input as an owner-only asset, and records reference-only enrollment facts. It
launches nothing and reserves no budget. A T3 parent session calls it before
it delegates the tasks itself.

## Sub-features

- A valid two-task batch on one seat enrolls both tasks. The report names
  `task_id`, `seat`, `state` and the target summary only.
- The run directory is `0700` and each retained asset is `0600` on POSIX.
  Journal facts carry digests and relative names, never task text.
- `run.json` records status `awaiting-native-parent` and the lifecycle journal
  is active.
- Re-running the same request is idempotent. It replays the same journal facts
  and reports the same `run_id`.
- Refusals: a dirty workspace, an unbound seat, a non-worktree task, batch
  synthesis, and unsupported target bindings all exit non-zero with one
  bounded stderr line and write nothing.

## How to get to it (user POV)

A parent session with a batch file and a roster binding runs:

```bash
brigade t3 open batch.json --target .
```

It reads the JSON report, keeps `run_id` as the correlation handle for later
verbs, and then delegates each task with its own native tool. Running the same
command again is safe.

## Driving it with control-brigade

```bash
C=registry/skills/verify-brigade/control-brigade.py
TARGET=$($C new-target | python3 -c 'import json,sys; print(json.load(sys.stdin)["target"])')
$C t3-enrollment --target "$TARGET"
$C evidence --target "$TARGET" --label t3-enrollment
$C cleanup
```

The drive creates a clean nested git workspace inside the temp target, writes
a roster with one bound seat, opens a two-task worktree batch, and opens it a
second time. Observed on the drive that owns this file: `ok: true`, two tasks
`enrolled`, `run.json` status `awaiting-native-parent`, two assets at `0600`,
task text absent from the lifecycle journal, and the replay returning the same
`run_id`.

## Gotchas

- The workspace must be clean. The drive commits its nested workspace first
  because `.brigade/` is ignored but other untracked files are not.
- Only `worktree` task isolation is accepted, and synthesis refuses. Both
  refusals happen before the run lock is taken.
- Custody reads refuse a symlink, a hardlink, or a widened POSIX mode at the
  asset name. On Windows the mode check does not apply because POSIX mode bits
  do not exist there, while the regular, single-link and no-follow checks do.
- `brigade runs resume` and `brigade runs recover` refuse this run. Its next
  transition belongs to the parent session.
