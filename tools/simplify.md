# Simplify

Use this cross-harness tool source when a repo needs a concise explanation, cleanup plan, or smaller version of a complex artifact.

## Intent

Turn dense local context into a clear, actionable summary without changing files, running commands, or hiding important uncertainty.

## Use When

- A plan, issue, design note, or handoff is too broad to act on.
- A user asks for a shorter version of existing local material.
- A harness needs the same simplification behavior projected into its native command or skill format.

## Procedure

1. Identify the source material and the target audience.
2. Preserve concrete facts, commands, file paths, blockers, and decisions.
3. Remove repetition, speculation, and incidental implementation detail.
4. Keep unresolved questions explicit.
5. End with the next concrete action when one is known.

## Boundaries

- Do not invent missing facts.
- Do not remove safety, privacy, approval, or verification requirements.
- Do not edit source files unless the user explicitly asks.
- Do not run commands only to simplify text.

## Output Shape

Prefer:

- one short answer-first paragraph
- a short list of material facts or decisions
- a final next action when useful
