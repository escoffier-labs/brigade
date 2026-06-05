# Anti-Slop

Use this cross-harness tool source to remove vague, low-quality, performative, or unfinished work before handoff.

## Intent

Force the work to become specific, testable, and useful. Replace generic filler with concrete behavior, evidence, and clear next actions.

## Use When

- An answer, plan, UI, document, or code change feels generic or padded.
- A task claims success without enough verification.
- A workflow has placeholders, fake completeness, weak assumptions, or unexplained tradeoffs.
- A user asks for sharper, less sloppy, or more production-ready work.

## Procedure

1. Name the actual goal in one sentence.
2. Remove decorative text, vague praise, generic best practices, and unsupported certainty.
3. Check whether every claim is backed by a file, command, source, screenshot, test, or explicit assumption.
4. Replace placeholders with working behavior, or label the blocker and the exact missing input.
5. Prefer root-cause fixes over cosmetic patches.
6. Run the smallest meaningful verification step before saying the work is done.
7. Report only the changes, evidence, caveats, and next action that matter.

## Boundaries

- Do not use anti-slop as an excuse to broaden scope.
- Do not rewrite user intent into a different task.
- Do not hide uncertainty, skipped verification, or known gaps.
- Do not make prose terse at the cost of losing required technical detail.

## Output Shape

Prefer:

- concise answer first
- concrete evidence or verification
- explicit caveats only when they affect the user
