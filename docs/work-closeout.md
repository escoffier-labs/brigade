# Work Verification And Closeout

Brigade can write local verification receipts and work closeout records for completed work sessions. This gives the operator one reviewable answer for task acceptance, test command results, scanner sweep state, code review closeout state, handoff draft state, and session evidence.

## Commands

```bash
brigade work verify plan
brigade work verify run --command "python -m pytest -q tests/test_example.py"
brigade work verify runs
brigade work verify ci
brigade work verify show <run-id>
brigade work closeout latest
brigade work closeout <session-id>
```

`verify plan` checks an explicit command or verifier manifest without running it.
When the code map is installed and indexed, the plan ranks affected tests from
changed files (`--file`, or `git diff --name-only HEAD`) and can suggest a
focused command. With no command, manifest, or ranked suggestion, the plan
asks for a focused test selector or a manifest.

`verify run` executes an explicit `--command`, `--argv-json`, or `--manifest`.
Commands run with `shell=False` and support leading environment assignments
such as `PYTHONPATH=src`. Brigade rejects high-risk shell-like commands and
stores stdout, stderr, and the receipt under the following directory.

```text
.brigade/work/verify-runs/
```

`verify runs` and `verify show` inspect those receipts.

`work closeout` writes a closeout record under:

```text
.brigade/work/closeouts/
```

The closeout reports local checks for the work session and the CI result for
the current commit and tree. The Markdown summary and JSON record include the
number of command records in the local receipt. Closeout also includes the
work session, task acceptance criteria when present, scanner sweep and review
state, handoff draft state, and blockers.
The session's `session.json` stores a compact closeout reference. See
[CI evidence for HEAD](verify-ci.md).

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
      "command": "pytest -q tests/test_example.py",
      "argv": ["pytest", "-q", "tests/test_example.py"],
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
- a completed verification receipt belongs to the work session, matches the
  current tree fingerprint, and did not record a tree change during the run
- the consumed task has acceptance criteria when task evidence is present
- the latest scanner sweep has not failed and has no unresolved review issue
- code review has no unclosed review run and no unresolved imported finding
- handoff draft health has no open issue

Blocked closeouts are still written, so the operator has a local record of what remains.

Run local checks before ending the work session so the receipt carries that
session's ID. When work start records a harness session fingerprint, closeout
also checks that fingerprint. An older receipt with only the matching harness
fingerprint can count if its run took place within the work session. If several
receipts qualify, closeout uses the newest one.

When no receipt qualifies, the blockers explain the missing session evidence,
tree mismatch, tree change, or incomplete run. If there is a receipt for the
session, its summary is included for diagnosis.

## Boundary

Local verification commands run only on an explicit `brigade work verify run` invocation. Closeout reads local records. `brigade work verify ci` contacts GitHub to refresh the saved CI result.
