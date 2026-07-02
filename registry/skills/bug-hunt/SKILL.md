---
name: bug-hunt
description: Use when asked to find bugs, hunt for correctness issues, sweep a codebase for defects, or verify a repo behaves as intended. Not for style or architecture review; this is defect-finding only.
---

# bug-hunt

A correctness sweep that only reports bugs it failed to refute. Finders generate candidates; verifiers try to kill them; survivors make the report. The single biggest failure mode of agent bug-hunting is plausible-but-wrong findings, so verification is not optional.

**Read-only.** Finding bugs and fixing them are separate engagements.

## Lenses

Sweep with each lens. With parallel subagents available, one finder per lens; otherwise sequential passes.

| Lens | Hunting for |
|------|-------------|
| Logic | Inverted conditions, off-by-one, wrong operator, unreachable branches, broken invariants |
| Error handling | Swallowed exceptions, missing error paths, errors that corrupt state before propagating, misleading messages |
| Edge cases | Empty/nil/zero inputs, unicode, huge inputs, boundary values, first/last iteration |
| Concurrency | Races, missing locks, shared mutable state, TOCTOU, async ordering assumptions |
| API misuse | Contract violations against libraries and the project's own interfaces, ignored return values, resource leaks, lifecycle errors |

Focus finders on code that is reachable and load-bearing: entry points, hot paths, recently changed files (`git log --since` is a good prior). A bug in dead code is info, not a finding.

## Verification (mandatory)

Every candidate gets an adversarial pass before it may appear in the report. The verifier's job is to REFUTE the finding, default skeptical:

1. Read the actual code path end to end, including callers and guards the finder may have missed.
2. Trace a concrete input that triggers the bug. No trigger, no bug.
3. Check whether a test, type system, or runtime check already prevents it.
4. Verdict: confirmed (with the triggering scenario), refuted (drop silently), or unverifiable (report downgraded one severity, marked `(unverified)`).

When tests can be run safely (no external dependencies, sandboxed), a failing reproduction test is the gold standard for confirmation and should be included in the finding as a sketch, not committed.

## Report contract

Same spine as line-check so findings compose. Severity: **critical** (data loss, corruption, security-adjacent) / **high** (wrong results on common inputs, crashes) / **medium** (wrong on edge cases) / **low** (latent, needs unlikely conditions) / **info**. Effort is the fix cost: S / M / L.

```markdown
# bug-hunt report: <repo> (<date>)

## Verdict
Paragraph: overall correctness posture, the scariest confirmed bug.

## Scorecard
| Lens | Score (0-5) | Summary |

## Findings
### [SEVERITY] Short imperative title
- **Lens:** which lens found it
- **Where:** file:line
- **What:** the defect, concretely
- **Trigger:** the concrete input or sequence that hits it
- **Why it matters:** consequence
- **Fix:** specific action
- **Effort:** S / M / L

## Backlog
Numbered, leverage-sorted: `N. [SEVERITY/EFFORT] title (lens)`

## Not checked
Lenses or areas skipped and why; candidates that were refuted (count only).
```

## Common mistakes

- Reporting finder output without verification. Half of plausible candidates die under a skeptical read.
- "This could be a problem if..." findings with no trigger. A bug without a triggering input is a hypothesis.
- Treating style issues as bugs. Wrong formatting never corrupted data.
- Stopping at the first confirmed bug in a file. Bugs cluster; finish the file.
