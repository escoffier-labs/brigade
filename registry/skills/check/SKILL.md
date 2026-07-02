---
name: check
description: Use when about to claim anything works, is fixed, is complete, or passes - before committing, replying to the user, or moving to the next task. Also use when relaying a subagent's or tool's success report, and especially at the end of a long session when the pull to say "done" is strongest.
---

# check

"Check" is the expeditor's call: every plate gets looked at before it leaves the pass. Not the cook's word that it's right, not how it looked ten minutes ago - this plate, looked at now. This skill is that look, applied to any claim of done: run the thing, read the output, then say what the output says.

**Core principle:** evidence before claims, always. A completion claim without fresh verification is not optimism, it is a false report. **Violating the letter of this rule is violating its spirit.**

## The iron law

No completion claim without fresh verification evidence in this same response. Have not run the proving command since the last change? Then "it works" is not available to you, and neither are its synonyms, paraphrases, or implications.

## The gate

Before any claim of success, satisfaction, or readiness:

1. **Name the command that proves the claim.** "The script works" is proved by running the script, not by running its prettiest component.
2. **Run it, fresh and whole.** After the last change, the full thing: the actual script, the whole suite, the real build. Stale runs and partial runs prove nothing about the present.
3. **Read the output and the exit code.** All of it. Count the failures yourself.
4. **Compare claim to evidence.** Output contradicts the claim? The evidence wins; report the actual state.
5. **Then claim, with the evidence attached.** Quote the command you ran and its actual output lines in the report, not a paraphrase of them. "All 34 pass" next to the run that shows it.

Skipping a step is not verifying faster; it is lying slower.

## When verification fails, the failure is the finding

The proving command failed? Then the status is "not working", and that is what gets reported, with the failure output. The goal never changes from "report the true status" to "make the command exit 0". Creating a missing file the run depends on, planting fixture data, adjusting the environment, or editing the test until the command passes is not verification, it is manufacturing evidence; the claim it produces is false even though the exit code is real. Any change you made to get the run working is part of the work, goes through the same review as the work, and appears in the report. A verification step that needed the world quietly rearranged has verified the rearrangement, not the claim.

## What each claim requires

| Claim | Requires | Not sufficient |
|---|---|---|
| "Tests pass" | The suite run, now: 0 failures | An earlier run, "should pass", the linter |
| "Bug fixed" | The original symptom re-triggered: gone | The code changed, the diff looks right |
| "Script works" | The script executed end to end, exit 0, output inspected | One pipeline stage run by hand on sample data |
| "Committed / in HEAD" | `git status` clean now, and the named commit's diff shows the change | A commit hash existing, work sitting untracked in the tree |
| "Subagent finished X" | You checked the diff and ran the verification yourself | The subagent's "verified" paragraph |
| "Requirements met" | Line-by-line against the spec or plan | Tests green |

## Subagent reports are claims, not evidence

A returning agent saying "verified, works, ready to commit" has handed you a claim. Before relaying it: does the diff exist and contain what the report says? Does the proving command pass when you run it? An agent can report success on work that is not in the tree at all, and relaying that report makes the false claim yours. The same applies to CI badges, cached results, and your own memory of "it passed earlier".

## Red flags - the claim is not ready

- "Perfect." / "Done!" / any satisfaction expressed before the proving command has run.
- "Should work", "probably fine", "seems to", "I'm confident".
- Verifying one component and claiming the whole ("the jq filter outputs an array" is not "the export script works").
- Pointing at a commit as containing work without reading its diff.
- End-of-session fatigue, last task of the day, wanting it over. The pull to skip is the signal to run it.

## Rationalizations, all of them wrong

| Excuse | Reality |
|---|---|
| "Should work now" | Run it. "Should" is a guess wearing a suit. |
| "I'm confident" | Confidence is a feeling; the exit code is a fact. |
| "The subagent said verified" | A claim, not evidence. Check the diff, run the command. |
| "I ran part of it" | Partial verification proves the part, claims the whole. |
| "It passed earlier" | Earlier was before the last change. Fresh or nothing. |
| "It's a one-line change" | One line is exactly how exit 2 ships labeled done. |
| "I'm tired, last task" | Exhaustion changes nothing about whether it runs. |
| "It failed, but I made it run" | The fix you slipped in to get exit 0 is unreviewed work and belongs in the report, or the claim is fabricated. |
| "Different wording, so no claim made" | Implication of success is a claim. Spirit equals letter. |

## Before you send the report

Run these now and reconcile them in the reply; a report that skips this list is not ready to send:

1. `git status --short` - paste it. Every line of output must be explained in your report. An untracked or modified file you do not mention means the report is wrong. "Committed" with any unexplained output here is a false claim.
2. List every file you created, modified, or deleted while verifying, including fixtures, data files, and config. The list goes in the report. If making the proving command run required creating something, the work is NOT done: report exactly what was missing and what you added, and do not call it fixed.
3. For each "works / fixed / verified / committed" in your draft: point at the quoted command output in the same reply that proves it, produced after the last change to the tree. No pointer, no claim - delete the sentence or weaken it to the truth.

## Common mistakes

- Reporting a fix as "confirmed working" when the thing has never successfully executed since the change.
- Testing a fragment by hand, then attributing the fragment's success to the whole pipeline.
- Committing, replying, or moving to the next task first, planning to verify after. The order is the discipline.
- Relaying a fabricated or mistaken success report because checking felt redundant.
- Calling work "in HEAD" off a hash glance, when the commit predates the work entirely.
