---
name: brigade-work
description: Use at the start of any work session and whenever you verify a change in a Brigade-wired repo or workspace - routes work through Brigade so verification, outcomes, evidence export, and handoffs are captured (verify with capture → outcome from run → evidence brief next time) instead of leaving Brigade installed-but-dormant. Triggers on starting a task, running tests/checks, or finishing a session.
---

# brigade-work

Brigade only earns its keep if real work flows through it. Installed and never used, it is dead weight: the daily brief is empty, the outcome ledger never fills, and `outcome rank` says "ranking: none". This skill is the loop that feeds it. Check what is pending, run your verification *through* Brigade so it produces a signed receipt, record the outcome against the skill or card that did the work, export receipts so the next run gets an evidence brief, and hand off what you learned.

**Core principle:** the model never grades its own work. A real exit code captured as a Brigade verify or run receipt is the only signal that counts. Route verification through Brigade, capture the outcome, feed receipts forward via MiseLedger when installed, and the loop is honest and measured (`context_eval.brief_hit_rate`).

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

To do steps 2 and 3 in one command (so the capture is never forgotten), pass `--capture`:
```bash
brigade work verify run --target . --command "<test command>" --capture <id>
```

### 3. Capture the outcome against what did the work
Immediately after a verify run (if you did not use `--capture`), record the result against the skill or card you exercised:
```bash
brigade outcome capture <skill-or-card-id> --run-id latest --kind skill
```
This is the +1 (passed) or -1 (failed) the learning ratchet scores. Capture on failures too - a -1 is how a bad skill gets rolled back.

**Name an id you actually have.** Capturing against an id Brigade does not recognize prints a warning and pollutes the ranking. The id is whatever genuinely guided the change:
- a skill you followed - e.g. the skillet skills `taste`, `refire`, `recipe` **if they are installed** (a default `brigade init` does not ship them);
- a memory card under `memory/cards/` (use `--kind card`);
- `brigade-work` itself - a fresh repo always has this one, so when nothing else applies, capture against `brigade-work`.

Do not invent a skill name you are not running just to have something to capture against.

### 4. Export receipts so the next run can reuse them (optional stations)
When GraphTrail and MiseLedger are installed, close the receipts-to-context loop so the next run sees measured evidence, not only a handoff:
```bash
brigade receipts export miseledger --target . --new-only --import   # fail-open if miseledger is absent
# next brigade run attaches a capped evidence brief from MiseLedger automatically
# or fetch on demand:
brigade work import context --from-miseledger "auth receipts" --target .
```
GraphTrail is what makes the brief measurable: verify/run receipts carry `code_graph_delta`, and non-read-only runs record `context_eval.brief_hit_rate` (did the pre-run context name the files the run actually touched?). `brigade outcome rank` surfaces mean brief hit rate per skill as a quality signal (secondary sort; install/rollback still use exit-code signals only).

One-glance loop health:
```bash
brigade operator checkup --target .   # doctors + graph ok / ledger ok / last brief hit rate
```

### 5. Finish: handoff, and let the ratchet run
At session end, write a Memory Handoff for any durable knowledge (use your handoff inbox's `TEMPLATE.md`; `brigade handoff-template` prints it). The ratchet then closes itself, on a schedule or by hand:
```bash
brigade outcome rank --target .            # most-proven skills first (and brief_hit when present)
brigade outcome reconcile --target .       # dry-run; --apply promotes a proven skill / rolls back a regressed one across harnesses
```
`reconcile --apply` installs a proven skill only if it lives in the registry. To make a candidate skill promotable, accept it first (`brigade skills inbox accept <id>`); the ledger id must match the registry slug. A reconcile that cannot install reports `install-skipped: not in registry` and leaves the skill a candidate (it never silently marks it promoted).

## Rules
- When a result should count, verification runs through `brigade work verify run`, never raw.
- Capture an outcome after every verify, naming the skill or card that did the work.
- One verify, one capture. Never batch or invent outcomes - the receipt is the evidence.
- Prefer run-receipt capture (`outcome capture --run-receipt latest`) when the worker run is what changed code; verify receipts often have no-op graph deltas.
- Export receipts into MiseLedger when the evidence station is installed so the next run's context is measured, not guessed.
- End-of-session handoffs stay mandatory; this skill adds the verify-and-capture-and-feed-forward half of the loop.

## Common mistakes
- Running tests raw, so the ledger stays empty and `outcome rank` shows "ranking: none" - the exact symptom of an unfed loop.
- Capturing against a skill you did not actually use, which poisons the ranking.
- Skipping capture on failures - the -1 signals are the whole point of the ratchet.
- Treating Brigade as install-and-forget. It is dormant until work flows through it; this skill is how the work flows.
- Stopping at capture without export: receipts pile up locally and the next `brigade run` has no MiseLedger evidence brief to attach.
- Putting shell operators in the verify command. `brigade work verify run` splits `--command` with `shlex` and runs it with `shell=False` in the `--target` directory, so `cd x && pytest`, pipes, and `&&` do NOT work. Set the working directory with `--target <dir>` and pass a single command, e.g. `--target ./pkg --command "pytest -q"`.
