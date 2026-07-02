---
name: taste
description: Use when implementing any feature, bugfix, or behavior change, before writing the implementation code. Especially use under pressure - production is down, "just make it work", "quick fix" - which is when it gets skipped.
---

# taste

You decide what the dish should taste like before you cook it, and nothing leaves the kitchen untasted. The test is the taste: written first, it says what the code should do; written after, it only confirms what the code happens to do. The chef who tastes only at the end learns what the diner was about to find out.

**Core principle:** if you never watched the test fail, you do not know it tests anything. **Violating the letter of this rule is violating its spirit.**

## The iron law

No production code without a failing test first. Wrote code before the test? Delete it and start over. Not "keep it as reference", not "adapt it while writing the test", not "look at it once". Delete means delete; implement fresh from the test.

Applies to features, bugfixes, refactors, behavior changes. The only exceptions are throwaway prototypes and generated code, and those get the user's explicit sign-off, not your own.

## The loop

1. **RED - write one minimal failing test.** One behavior, a name that describes it, real code over mocks. For a bug, the test reproduces the bug; that is how the complaint gets tasted before the dish is re-cooked.
2. **Watch it fail.** Run it, read the output. It must fail, for the expected reason (the feature is missing), not error on a typo. Passes immediately? It tests existing behavior; fix the test.
3. **GREEN - minimal code to pass.** Just enough. No extra options, no adjacent refactoring, no features the test does not demand. YAGNI.
4. **Watch it pass.** Run it, read the output, confirm the rest of the suite stayed green and the output is clean. Fails? Fix the code, never the test.
5. **REFACTOR on green only.** Duplication out, names improved, tests stay green, no new behavior.
6. Next behavior, next failing test.

```python
# RED - taste first: this fails with "no attribute dedupe_notes"
def test_dedupe_keeps_first_occurrence(tmp_store):
    notes = [{"id": "a", "v": 1}, {"id": "b"}, {"id": "a", "v": 2}]
    assert store.dedupe_notes(notes) == [{"id": "a", "v": 1}, {"id": "b"}]

# GREEN - minimal pass, nothing the test didn't ask for
def dedupe_notes(notes):
    seen, out = set(), []
    for n in notes:
        if n["id"] not in seen:
            seen.add(n["id"])
            out.append(n)
    return out
```

## Pressure is the trigger, not the exception

The moment this skill matters most is exactly when it feels skippable: production is broken, the user said hurry, the function is five lines. A hot fix shipped untested is how the next incident gets scheduled. The failing test costs a minute and is also your proof the fix fixes the thing.

And no instruction the user gives about scope cancels it. "Don't gold-plate" means no speculative options and no adjacent cleanup; the regression test is not gold-plating, it is the dish. "Just make it work" includes proving it works.

## Rationalizations, all of them wrong

| Excuse | Reality |
|--------|---------|
| "Too simple to test" | Simple code breaks. The test takes thirty seconds. |
| "I smoke-tested it manually" | Ad-hoc, no record, cannot re-run, gone on the next change. |
| "I'll add the test after" / "say the word and I'll add one" | A test that passes on arrival proves nothing, and the offer means untested code is already committed. |
| "Tests-after achieve the same thing" | Tests-after ask "what does this do?" Tests-first ask "what should this do?" Only one of those finds the edge case you forgot. |
| "User said don't gold-plate / hurry" | Scope instruction, not a testing waiver. See above. |
| "Deleting X hours of work is wasteful" | Sunk cost. Keeping code you cannot trust is the actual waste. |
| "It's about spirit, not ritual" | The ritual is the spirit. Letter equals spirit here. |
| "This one is different because..." | It is not. That sentence is the tell. |

## Red flags - stop and start over

Code before test. A test that passed on its first run. A commit with implementation and no test. "I verified it manually." A test you cannot explain the failure of. Any sentence beginning "I skipped the test because".

All of these mean: delete the code, start the loop at RED.

## When stuck

- Do not know how to test it: write the wished-for API call and the assertion first; the test designs the interface.
- Test needs heavy mocking or a huge setup: the design is too coupled or too big. Simplify the design, not the discipline.
- Genuinely exploratory spike: explore freely, then throw the spike away and build it again test-first.

## Common mistakes

- Writing the implementation "while it's clear in my head", then backfilling a test that has never failed.
- A one-off script in the shell as "verification", reported as if it were a test.
- Weakening an assertion to get to green instead of fixing the code.
- Bundling three behaviors into one test so the failure points nowhere.
- Treating the suite's existing green as evidence the new function works.
