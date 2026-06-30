---
name: ultra-work-scout
description: Use when the user asks for ultra work, scout work, broad pre-implementation scouting, or parallel investigation before a large Brigade task. Designed for Codex and other SKILL.md harnesses that can delegate or split work.
---

# ultra-work-scout

Use this skill when a task is important enough to scout before changing code, especially when the user asks for "ultra work", "scout this", "use scouts", "use subagents", or "really scope this out".

## Mission

Turn a large request into a small number of parallel, bounded scouting jobs, then bring the results back into one implementation path. Scouts gather context and risks; they do not replace the main agent's ownership of the final change.

## Default Scout Set

Start with the smallest set that can answer independent questions:

- **Surface scout**: find the public commands, docs, templates, and user-facing entry points that must change.
- **Install scout**: trace init, upgrade, profile, and default-install paths so new users get the intended behavior.
- **Verification scout**: identify focused tests, full checks, and existing flaky or environment-sensitive gates.

Only add more scouts when the work naturally splits into independent repos or subsystems.

## Main Agent Loop

1. State the immediate local task you will keep yourself.
2. Assign scouts concrete questions with disjoint read or write scopes.
3. Keep moving on non-overlapping work while scouts run.
4. Integrate only findings you can verify in the current tree.
5. Run verification through Brigade:

```bash
brigade work verify run --target . --command "<test command>" --capture ultra-work-scout
```

If `--capture` is not available in the installed Brigade version, run:

```bash
brigade work verify run --target . --command "<test command>"
brigade outcome capture ultra-work-scout --run-id latest --kind skill
```

## Boundaries

- Do not spawn scouts for tiny, single-file fixes.
- Do not let scouts mutate the same files unless you explicitly split ownership.
- Do not trust a scout's success claim without reading the diff or output that matters.
- Do not skip the final Brigade verify and outcome capture.

## Output Contract

Finish with:

- Scout questions asked and what changed because of them.
- Files changed.
- Verification command and result.
- Any release blocker or follow-up that remains.
