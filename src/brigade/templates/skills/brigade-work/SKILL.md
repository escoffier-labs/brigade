---
name: brigade-work
description: Use at the start of any work session and whenever you verify a change in a Brigade-wired repo or workspace - routes your work through Brigade so verification, outcomes, and handoffs are actually captured instead of leaving Brigade installed-but-dormant. Triggers on starting a task, running tests/checks, or finishing a session.
---

# brigade-work

Brigade only earns its keep if real work flows through it. Installed and never used, it is dead weight: the daily brief is empty, the outcome ledger never fills, and `outcome rank` says "ranking: none". This skill is the loop that feeds it. Check what is pending, run your verification *through* Brigade so it produces a signed receipt, record the outcome against the skill or card that did the work, and hand off what you learned.

**Core principle:** the model never grades its own work. A real exit code captured as a Brigade verify receipt is the only signal that counts. Route verification through Brigade, capture the outcome, and the loop is honest.

## When this applies

The repo or workspace is Brigade-wired: a `.brigade/` directory exists, or `brigade status --target .` succeeds. If neither is true, this skill does not apply (run `brigade init` first only if the user wants Brigade here).

## The loop

### 1. Start: see what is pending
At the start of a work session, before deciding what to do:
```bash
brigade work brief --target .        # pending work, imports, warnings, suggested next command
```
In a memory/operator workspace, `brigade daily status --target .` is the lighter equivalent. Read it first.

### 2. Verify THROUGH Brigade, not raw
When you would run tests or a check to confirm a change, run it through Brigade so it records the exit code:
```bash
brigade work verify run --target . --command "<your test command>"   # e.g. "pytest -q", "npm test", "go test ./..."
```
This writes a receipt under `.brigade/work/verify-runs/<run-id>/` with the real status. Running tests raw produces no signal and the ledger stays empty.

### 3. Capture the outcome against what did the work
Immediately after a verify run, record the result against the skill (or memory card) you exercised:
```bash
brigade outcome capture <skill-or-card-id> --run-id latest --kind skill
```
The id is the skill you followed (e.g. `taste`, `refire`, `recipe`) or the card that guided the change. This is the +1 (passed) or -1 (failed) the learning ratchet scores. Capture on failures too - a -1 is how a bad skill gets rolled back.

### 4. Finish: handoff, and let the ratchet run
At session end, write a Memory Handoff for any durable knowledge (use your handoff inbox's `TEMPLATE.md`; `brigade handoff-template` prints it). The ratchet then closes itself, on a schedule or by hand:
```bash
brigade outcome rank --target .            # most-proven skills first
brigade outcome reconcile --target .       # dry-run; --apply promotes a proven skill / rolls back a regressed one across harnesses
```

## Rules
- When a result should count, verification runs through `brigade work verify run`, never raw.
- Capture an outcome after every verify, naming the skill or card that did the work.
- One verify, one capture. Never batch or invent outcomes - the receipt is the evidence.
- End-of-session handoffs stay mandatory; this skill adds the verify-and-capture half of the loop.

## Common mistakes
- Running tests raw, so the ledger stays empty and `outcome rank` shows "ranking: none" - the exact symptom of an unfed loop.
- Capturing against a skill you did not actually use, which poisons the ranking.
- Skipping capture on failures - the -1 signals are the whole point of the ratchet.
- Treating Brigade as install-and-forget. It is dormant until work flows through it; this skill is how the work flows.
- Putting shell operators in the verify command. `brigade work verify run` splits `--command` with `shlex` and runs it with `shell=False` in the `--target` directory, so `cd x && pytest`, pipes, and `&&` do NOT work. Set the working directory with `--target <dir>` and pass a single command, e.g. `--target ./pkg --command "pytest -q"`.
