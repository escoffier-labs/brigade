# Work Verification And Closeout

Brigade can write local verification receipts and work closeout records for completed work sessions. This gives the operator one reviewable answer for task acceptance, test command results, scanner sweep state, code review closeout state, handoff draft state, and session evidence.

## Commands

```bash
brigade work verify plan
brigade work verify run
brigade work verify run --command "PYTHONPATH=src python3 -m pytest -q"
brigade work verify runs
brigade work verify show <run-id>
brigade work closeout latest
brigade work closeout <session-id>
```

`verify plan` inspects the repo and proposes local verification commands. It recognizes Python test layouts and package.json projects, and it reports blockers without running anything. When the code map is installed and indexed, it also ranks affected-test candidates from changed files (`--file`, or `git diff --name-only HEAD`) with graph evidence and confidence. The ranking is advisory and the worker still chooses the command. Without the code map the plan degrades cleanly and keeps the default command list.

`verify run` executes only explicit local commands, directly with `shell=False`. It supports simple leading environment assignments such as `PYTHONPATH=src`, rejects high-risk shell-like commands, captures stdout and stderr logs locally, and writes a receipt under:

```text
.brigade/work/verify-runs/
```

`verify runs` and `verify show` inspect those receipts.

`work closeout` writes a closeout record under:

```text
.brigade/work/closeouts/
```

The closeout includes the selected work session, task acceptance criteria when present, latest verification receipt, latest scanner sweep state, code review closeout state, handoff draft queue state, and blockers. It also stores a compact closeout reference on the session `session.json`.

## Abridged receipt example (`schema_version` 2)

New verify receipts use integer `schema_version: 2`. `commands` is an array of command objects. Each object carries `exit_code` (integer or `null` when interrupted or timed out without a child status). The sample below is abridged to show the shape. A valid on-disk receipt also includes the required target, timestamps, run path, baseline commit, tree fingerprint, and patch digest fields listed in the schema reference.

```json
{
  "schema_version": 2,
  "run_id": "20260813-133700-work-verify-example",
  "status": "completed",
  "duration_seconds": 15.18,
  "commands": [
    {
      "command": "pytest -q",
      "argv": ["pytest", "-q"],
      "status": "completed",
      "exit_code": 0
    }
  ],
  "code_graph_delta": { "changed_symbol_count": 0 },
  "git": { "branch": "main", "dirty_files": 0 }
}
```

Full field list: [receipt schemas](receipt-schemas.md).

## Receipt boundaries

- A receipt is written when you run `brigade work verify run` (and by other mutating flows that document their own receipt paths).
- Not every Brigade command writes a receipt. Read-only status surfaces and some care commands update local artifacts without a verify-style receipt.
- Ad hoc shell tests outside `work verify run` leave no Brigade verify receipt.
- Declared verification contracts and budgets attach when the verify manifest or runbook carries them. Hard wall-clock and worker-dispatch ceilings apply only when those budgets are declared. See [receipt schemas](receipt-schemas.md) and the run-control notes in [technical guide](technical-guide.md).

## Ready State

A closeout is ready when:

- the work session is ended
- the latest verification receipt completed
- the consumed task has acceptance criteria when task evidence is present
- the latest scanner sweep has no unresolved review issue
- code review has no unclosed review run and no unresolved imported finding
- handoff draft health has no open issue

Blocked closeouts are still written, so the operator has a local record of what remains.

## Boundary

Verification and closeout are local and explicit. Brigade does not mutate CI, GitHub, reviewers, scanner imports, handoff drafts, canonical memory, daemons, schedulers, or remotes. Verification commands run only when the operator asks for `brigade work verify run`.
