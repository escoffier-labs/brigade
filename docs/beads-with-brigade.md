# Beads for planning, Brigade for verification

Teams that already plan in Beads (`bd`) can keep it as their planning tool and
add Brigade for verification receipts, memory, and fleet coordination. Brigade
reads a bd project and mirrors its issues and dependency edges into the local
work ledger. `brigade work ready` and `brigade work claim` then honor bd's
blockers. When work lands, Brigade writes a JSONL file of outcomes per bd issue
id that a bd user can attach or import.

Brigade does not adopt bd as its task store. The reasons are recorded in
[the #770 re-check](research/beads-770-recheck.md). The adapter works at the
edge: bd stays the planning source, and the Brigade ledger and hub records stay
the source for claims, receipts, and completion.

## Read-only on bd

Brigade only reads bd. It runs exactly two bd commands, `bd version --json` and
`bd export` (to stdout), and refuses any other bd invocation before it starts a
process. It never writes to the bd database, its Dolt store, or anything under
`.beads/`. The outcomes export writes only the file you name, and refuses a
path inside a `.beads` directory.

bd is optional. Brigade looks for it on `PATH`, or at `--bd-bin`. If bd is
missing, the command prints a clear message and exits 2.

## Supported bd releases

bd export rows carry no schema marker, so Brigade pins the producer release.
The supported series is 1.3.x. Exports from bd 1.3.1 and 1.3.2 were generated
from a scratch `bd init` and are tested as fixtures. Any other release fails
closed with a readable error before anything is written. So do an unknown
issue field, an unknown bd status, an unknown edge type, a dependency on an
issue outside the export, and a dependency cycle.

## Mirror the ready set in

```bash
brigade work import bd --target .
brigade work import bd --target . --bd-repo ../planning-repo
brigade work import bd --target . --from-jsonl export.jsonl --bd-version 1.3.2
brigade work import bd --target . --dry-run --json
```

Each bd issue becomes one ledger task with source `bd`, keyed by its bd id in
`metadata.bd.id`. Re-running is idempotent: an unchanged export writes nothing
to the ledger, and timestamp or counter changes alone are ignored. Issues
missing from an export are left alone, so a filtered export never deletes
tasks. Every run except a dry run leaves a receipt under `.brigade/work/bd-sync/`
with the bd version and the export's SHA-256.

| bd status | Brigade status |
| --- | --- |
| `open` | `pending` |
| `closed` | `done` |
| `deferred` | `deferred` |
| `in_progress`, `blocked`, `hooked`, `pinned` | `blocked` |

The mirror changes a status only when the task still holds the status the
mirror last set and nobody holds a Brigade claim on it. If an agent claimed the
task or Brigade finished it, Brigade's state wins and the run reports the
difference under `kept_brigade_state`. Text, priority, and acceptance criteria
follow bd while the task is not claimed or finished. Text passes through the
same secret redaction as other imports.

bd dependencies map onto the ledger's edge model:

| bd dependency | Brigade edge |
| --- | --- |
| `blocks` | prerequisite `blocks` the dependent |
| `parent-child` | parent `parent-child` child |
| `discovered-from` | provenance only, never blocks |

The mirror owns the edges it created. When bd drops a dependency, the matching
owned edge is removed. Edges added by hand with `brigade work task edge add`
are never removed by a sync, even when they match a bd dependency. A sync that
would create a cycle together with existing ledger edges is rejected whole.

One difference from `bd ready`: Brigade treats a parent as not startable work,
so an open epic with children does not appear in `brigade work ready`. Its
children do, once their own blockers clear.

Use one path per bd project. `brigade work import --from-bd-jsonl` stages a
bd snapshot as inbox proposals for review instead of mirroring it. Promoting
those proposals and also running the mirror creates two tasks for one bd
issue.

## Write outcomes out

```bash
brigade work export bd-outcomes --target . --output bd-outcomes.jsonl
```

The file has one JSON record per bd issue id, sorted by id, with schema
`brigade.bd-outcomes.v1`:

- `tasks`: the ledger tasks for that bd id, with status and completion time.
- `receipts`: work closeout receipts for those tasks, with the verification
  run id and status.
- `commits` and `pr_links`: commits in the target repository that name the bd
  id, and the pull request number from a squash or merge subject when `origin`
  is a GitHub remote.

Re-running the export on unchanged state produces identical bytes. Brigade does
not push the file into bd. A bd user decides whether to import it, attach it,
or read it.

## Done means a PR, a receipt, or a hub record

Treat bd as session scratch for planning. Nothing tracked only in bd counts as
done until it shows up as a PR, a Brigade receipt, or a hub work record.
A bd issue closed without one of those still needs that evidence before it
counts.

The hub work view does not show the mirrored ready set yet. That depends on
hub dependency edges, tracked in
[#2210](https://github.com/escoffier-labs/brigade/issues/2210).
