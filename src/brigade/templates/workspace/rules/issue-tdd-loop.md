# Issue And TDD Work Loop

Use this repo-shareable rule when work starts from a local task, scanner import, or issue mirror.

## Rule

1. Pick one bounded item before editing.
2. Confirm the item has acceptance criteria. Add criteria locally if they are missing.
3. Write or identify the smallest meaningful verification step before implementation when practical.
4. Make the narrowest change that satisfies the acceptance criteria.
5. Run the focused verification step and record the result in the work closeout.
6. Leave follow-up work as a new reviewed local task or import instead of silently expanding scope.

## Boundaries

- Do not mutate remote issues, pull requests, releases, tags, or repository settings from this rule.
- Do not copy private issue bodies, logs, chat transcripts, secrets, hostnames, or local paths into public docs.
- Do not treat this file as a personal preference file. Repo-specific policy belongs in this repo. Machine-local preference belongs in gitignored local config.

## Useful Commands

- `brigade work next`
- `brigade work task plan <task-id>`
- `brigade work run`
- `brigade work verify run`
- `brigade work closeout latest`
- `brigade work acceptance`
