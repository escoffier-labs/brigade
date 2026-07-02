---
name: reduce
description: Use when asked to simplify, clean up, tidy, or refactor code for clarity without changing what it does, or when the user says "simplify this", "clean this up", "make it readable", "reduce the complexity", or "tidy this". Behavior-preserving only; not a bug or security audit (use bug-hunt or security-sweep for those).
---

# reduce

A reduction simmers a sauce to drive off the water and concentrate the flavor, and the dish tastes the same, only sharper. You do that to code: boil off the excess, concentrate the intent, and the behavior comes out identical. The cook who reduces too hard scorches the pan and changes the dish; the discipline is knowing when to pull it off the heat.

**Core principle:** same inputs, same outputs, same side effects, same errors, same public interface, same evaluation order. If the behavior moved, it was not a reduction, it was a rewrite. **No behavior-preservation evidence, no applied simplification.** Violating the letter of that is violating its spirit.

## The behavior lock

Before you change a single line, establish the lock. This is the whole safety story; everything else is taste.

1. **Pin the scope.** Default to the code that recently changed (see Scope). Know exactly which files and functions you are reducing before you start.
2. **Find the tests that cover it.** Run them and confirm they exercise the code you are about to touch. If coverage is thin, write characterization tests first ([taste](../taste/SKILL.md)) that capture the *current* behavior, edge cases included, only when that is safe and in scope. If you cannot lock the behavior with tests, drop to report mode.
3. **Baseline-verify green.** Run the suite and read the output before touching anything. You are proving the starting point, not assuming it.
4. **Reduce in small steps,** one category of simplification at a time (see What to reduce).
5. **Re-run the same verification after each meaningful change.** Evidence in hand before any "behavior unchanged" claim ([check](../check/SKILL.md)). A reduction you did not re-run is a hypothesis.
6. **Cannot lock it?** Switch to report mode or ask the user. Never apply a simplification you cannot prove safe.

## Two modes

**Apply** is the default for recently changed code that has tests, or where characterization tests can be added safely. You make the edits, one category per commit, with the verification evidence.

**Report** is for broad scope, under-tested code, anything architectural or design-level, public-interface shifts, or when the user explicitly wants review before edits. You produce the simplification plan and stop. Auto-escalate from apply to report the moment the behavior lock cannot be established. When in doubt, report.

## What to reduce

Highest value first. These are local, mechanical, structure-preserving moves.

- **Flatten control flow.** Guard clauses and early returns over deep nesting; collapse arrow code.
- **Reveal intent through naming.** A clear name retires a comment that only restated the code.
- **Remove dead weight.** Unused locals, parameters, imports, and unreachable branches, once usage analysis confirms nothing references them.
- **Consolidate genuine duplication.** Same logic with the same reason to change, in three or more real places. Extract the shared intent, not merely the matching text.
- **Replace clever with boring.** Dense one-liners and trick expressions become plain, local, readable code.
- **Collapse needless intermediate state.** Drop temp variables, redundant data-shape conversions, and pass-through wrappers that earn nothing.
- **Reuse what the repo already has.** If a helper already exists, call it instead of inlining the logic a fourth time.

## What NOT to reduce

This is the discipline that separates a reduction from a scorched pan.

- **No new abstraction without three or more real call sites or a clear domain concept.** Premature DRY is its own debt; functions diverge the moment requirements do.
- **DRY past readability.** Two snippets that look alike but change for different reasons are not duplication. Leave them.
- **No dense one-liners or nested ternaries** sold as "simpler". Fewer lines is not the goal; lower cognitive load is.
- **No vague generic utilities.** A `utils.process()` that hides three unrelated jobs is worse than the code it replaced.
- **Never strip load-bearing redundancy.** Validation, defensive null checks, logging, retries, error fallbacks, compatibility shims, and intentional comments look redundant and are not. Looks redundant does not mean is redundant; when unsure, leave it and note it.
- **No style churn outside the scope.** Reformatting a wide area is how a semantic change hides in a diff nobody can read.

## Tidy, do not redesign

`reduce` tidies: local, mechanical, behavior-preserving cleanup. It does not move module boundaries, reshape data models, or pick new abstractions. Those are design decisions; surface them in report mode and hand off, never auto-apply. Tidying first earns the trust that makes the larger refactor possible later.

## Scope

Default: the code that recently changed, from `git diff` against the merge base or the working tree. That keeps the reduction relevant and prevents repo-wide churn.

Allow an explicit scope on request: a file, a directory, a symbol, or a commit or PR range. Whole-repo reduction only when the user asks for it, and report-first.

## One category per commit

Dead code in one commit, duplication in the next, guard clauses in the next. Each commit names its effect, stays reviewable, and reverts alone. One sweep that bundles five kinds of change is an unreviewable rewrite wearing a cleanup label.

## Boundaries

`reduce` applies changes; it is not an audit and it does not hunt for defects.

- Borrows from [taste](../taste/SKILL.md) for characterization tests, [check](../check/SKILL.md) for evidence before claims, and [refire](../refire/SKILL.md) when a reduction surfaces a real bug: stop, do not "simplify" the bug away, switch to debugging.
- Hands accepted report findings to [expedite](../expedite/SKILL.md) to drive to done.
- Hands correctness issues to [bug-hunt](../bug-hunt/SKILL.md), security issues to [security-sweep](../security-sweep/SKILL.md), and repo-wide quality to [line-check](../line-check/SKILL.md). Finding a bug is not in scope here; routing it is.

## Output shape

`reduce` has its own artifact, not the shared audit contract:

```markdown
## reduce: <scope> (<date>)
Mode: apply | report

### Simplification plan
- [category] what will change and why it is behavior-preserving

### Change log   (apply mode)
- [category] what changed (<commit>)

### Verification evidence
Baseline: <command> -> <result>
After:    <command> -> <result>   (must match)

### Handed off
- correctness/security/design items routed to bug-hunt / security-sweep / line-check
```

In report mode, findings may also be written in the [audit report format](../../../docs/audit-report-format.md) so [expedite](../expedite/SKILL.md) can consume them later.

## Common mistakes

| Mistake | Reality |
|---------|---------|
| "Cleaner" refactor that alters an edge case | If you did not re-run the tests, you changed behavior and do not know it. |
| Abstracting from two superficially similar snippets | Looks-alike is not duplication. Wait for three real call sites and a shared reason to change. |
| Deleting a defensive check that "can never fail" | That is the load-bearing redundancy. It fails the day you remove it. |
| Reformatting the whole file "while I was in there" | A semantic change now hides in the noise. One category per commit, scope held. |
| Fewer lines reported as simpler | Cognitive load is the metric, not line count. Dense is not simple. |
| "Behavior unchanged" with nothing run | A claim, not evidence. Lock, run, then claim. |
