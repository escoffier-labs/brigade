---
name: refire
description: Use when anything misbehaves - a failing test, a production bug, a build break, flaky or unexpected behavior - before proposing or attempting any fix. Especially under pressure ("CI is blocking everyone", "just get it green") and after a previous fix didn't hold.
---

# refire

A plate comes back to the kitchen. The line's instinct is to fire the same dish again, faster; the chef's job is to find out why it came back first, because a refire of the same mistake comes back twice and now the diner is angrier. This skill is the chef's version of debugging: no fix until the cause of the sendback is known.

**Core principle:** no fixes without root cause first. A fix that makes the symptom disappear without explaining it is not a fix, it is a scheduled second incident. **Violating the letter of this process is violating its spirit.**

## Find out why it came back

Before any fix, in order:

1. **Read the error, all of it.** The full traceback, the line numbers, the actual message. It frequently names the cause outright.
2. **Reproduce it.** Trigger it reliably with the smallest input you can. Cannot reproduce? Gather more evidence; do not guess.
3. **Check what changed.** `git log` and `git diff` around when it broke: recent commits, new dependencies, config or environment drift. Most bugs are young.
4. **Check the documented contract.** Schema docs, interface comments, the spec. The difference between the documented behavior and the actual behavior usually points straight at the cause, and the fix must satisfy the contract, not just the failing test.
5. **Trace to the source.** Where does the bad value originate? Who called this with it? Walk up the stack until the origin; the fix lands there, not at the crash site. In a multi-component system, instrument each boundary (what enters, what exits) and run once to see which layer breaks, rather than theorizing across all of them.
6. **Compare against what works.** Find the nearest working sibling (same pattern, same codebase) and list every difference, including the ones that "can't matter".

## Then fix it, once

1. **State the hypothesis.** "X is the root cause because Y", written, specific. If you cannot finish that sentence, return to investigation; do not pretend.
2. **Pin it with a failing test first.** The test reproduces the bug and asserts the violated contract, not just the absence of the crash. This is [taste](../taste/SKILL.md)'s loop applied to a bug; a fix without a test that failed is a fix that cannot prove itself and a regression nobody will catch.
3. **One minimal change.** Test the hypothesis with the smallest change that fixes the cause. One variable. No bundled cleanups, no "while I'm here".
4. **Verify and read the output.** The pinned test passes, the whole suite stays green, the original symptom is actually gone.

If the fix did not work: stop, do not stack a second fix on top. New evidence, new hypothesis, back to investigation. **After three failed fixes, stop fixing entirely**: three misses on one bug means the architecture or the pattern is wrong, and that conversation happens with the user before attempt four.

## Rationalizations, all of them wrong

| Excuse | Reality |
|--------|---------|
| "Quick fix now, investigate later" | Later never comes; the mask becomes load-bearing. |
| "CI is blocking everyone, no time for process" | Investigation on a reproduced bug takes minutes. Thrashing takes hours and ships masks. |
| "Just try X and see" | Guess-and-check is how one bug becomes three. |
| "It's probably X" | Probably is not a hypothesis with a because. |
| "The test passes now, ship it" | Green that you cannot explain is the most dangerous color. |
| "Several candidate fixes at once, to be safe" | Now you cannot tell which one worked or what the others broke. |
| "Too simple to need the process" | Simple bugs have causes too, and the process is fast on them. |
| "One more attempt" (after two failures) | Third miss means the question is wrong, not the answer. |

## Red flags - stop and return to investigation

Proposing fixes before reproducing. A fix at the crash site when the bad value came from upstream. Catching-and-ignoring, widening a timeout, or `default=`-ing an error away to get green. A fix with no new failing test attached. Explaining the fix without being able to explain the bug. Any sentence starting "I don't fully understand it, but this works".

## Common mistakes

- Silencing the symptom (swallow the exception, loosen the assertion, skip the test) and calling CI green a fix.
- Fixing the right cause but skipping the pinning test, so the contract that broke still has no guard and the next regression sails through.
- Never opening the docs or schema, so the "fix" satisfies the failing test while quietly violating the documented contract.
- Stacking fix on fix without reverting the misses, then being unable to say which change did what.
- Treating a correct guess as a validated hypothesis. Luck is not process; it just hasn't cost you yet.
- Skipping the "what changed recently" check and reverse-engineering from first principles what `git log` would have said in ten seconds.
