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

`verify plan` inspects the repo and proposes local verification commands. It recognizes Python test layouts and package.json projects, and it reports blockers without running anything. When GraphTrail is installed and indexed, it also ranks affected-test candidates from changed files (`--file`, or `git diff --name-only HEAD`) with graph evidence and confidence; the ranking is advisory and the worker still chooses the command. Without GraphTrail the plan degrades cleanly and keeps the default command list.

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
