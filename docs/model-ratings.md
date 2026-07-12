# Rating model seats for brigade run

A roster assigns each seat a CLI and a model. This page describes a way to rate
candidate models per task type with evidence Brigade can stand behind: scripted
graders, verify receipts, and no model grading its own work. Use it to decide
which model belongs in which seat instead of assigning by reputation.

## The scale

Each seat gets 0 to 100 per task type, graded by script. Speed is recorded as a
separate axis, never folded into the score. Read the bands as:

| Band | Meaning |
|---|---|
| 90-100 | trust the seat with this task type unsupervised |
| 70-89 | usable with review |
| 40-69 | needs a stronger seat or a tighter prompt |
| below 40 | do not assign this task type |

Treat gaps under 10 points as noise until you have re-runs.

## Task battery

Four task types cover the work brigade seats actually get. Every score traces
to an exit code or a scripted grader:

- **bug-hunt**: a module with N planted bugs and an answer key. Score is recall.
  Grade with regexes first (precision), then let a *different* model judge only
  the regex misses against the answer-key concepts (recall). Self-grading
  poisons the number. Route each worker's output to a judge from another family.
- **fix-tests**: a failing test suite the worker must make pass, in an isolated
  git copy, with a hash check that zeroes the score if the tests file was
  touched. The pytest exit code is the grade.
- **strict-json**: messy prose in, exact schema out, with a red-herring figure
  that must be recomputed. Field-level scoring.
- **constrained writing**: a short factual doc with hard limits (word cap,
  required examples). Deduct for every violation a linter or a grep can catch.

Run each task through `brigade work verify run --target . --command "<grader>"`
so the grading run itself leaves a receipt in `.brigade/work/verify-runs/`.

## Example: pilot run, 2026-07-11

Seven seats, one thinking tier per family, single run per cell:

| seat | bug-hunt | fix-tests | strict-json | writing | mean | total s |
|---|---:|---:|---:|---:|---:|---:|
| composer-2.5 | 100 | 100 | 100 | 100 | **100** | 71 |
| fable-5 | 100 | 100 | 100 | 100 | **100** | 119 |
| grok-4.5 | 100 | 100 | 100 | 100 | **100** | 83 |
| gpt-5.6-sol-medium | 100 | 100 | 100 | 100 | **100** | 219 |
| gpt-5.5 | 90 | 100 | 100 | 100 | **98** | 206 |
| gpt-5.6-luna-medium | 90 | 100 | 100 | 100 | **98** | 100 |
| gpt-5.6-terra-medium | 80 | 100 | 100 | 100 | **95** | 86 |

What this run actually showed:

- Three of four tasks hit the ceiling for every seat. When that happens the
  table ranks speed and the hardest task's tail, not overall ability. Harden
  the battery before drawing seat-assignment conclusions from it.
- Within the GPT-5.6 family at the same thinking tier, terra was fastest and
  missed the two subtlest bugs, sol found everything but took three times
  luna's wall clock, luna sat between. That is the shape thinking-level fans
  should quantify next: score per second, per variant, per tier.
- The one bug most seats missed was the quiet state bug (an item that is never
  dropped when its quantity goes negative), not the loud crash bugs. Weight
  planted bugs accordingly.

## Pitfalls that will skew your table

- **Regex-only answer keys under-credit.** One seat in the pilot initially
  scored 50 on bug-hunt from phrasing variance alone. The cross-model judge
  pass corrected it to 90. Keep regexes for precision and judge the misses.
- **Cursor composer models return empty text in plan mode** (issue #206), so
  benchmark them in write mode with do-not-modify instructions, or their
  read-only cells score a false 0.
- **Adapter gaps are not model gaps.** The plain `claude -p` adapter cannot
  edit files headless. Give the model the same permissions the other seats get
  when you benchmark, then note the adapter limit in your roster instead.
- **Deep working directories break app-server steering.** Unix socket paths cap
  near 108 characters. Brigade warns and the run completes without live control.

## Keeping it honest

One run per cell is a screening pass, not a verdict. Re-run before promoting or
demoting a seat, keep the receipts, and record roster changes the ratings caused
next to the run id that justified them.
