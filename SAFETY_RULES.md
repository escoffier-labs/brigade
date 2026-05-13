# SAFETY_RULES.md

## Publishing

- Scrub public content before release.
- Do not publish private hostnames, account IDs, secrets, internal paths, or personal contact details.
- The pre-push hook runs content-guard against `public-repo.json` policy. Do not `--no-verify` unless the user has explicitly accepted the risk.

## Production

- Ask before destructive or production-impacting actions.
- Prefer read-only inspection before mutation.
- Back up config before risky changes.

## Credentials

- Never store secrets in markdown.
- Use env files, secret stores, or platform credential managers.

## Memory Hygiene

- Do not write durable memory entries directly; use the handoff flow.
- Do not promote unverified reflections into canonical memory.
- Stale memory is worse than missing memory. Remove or update entries when their basis changes.

## AI Attribution

- Do not add `Co-Authored-By` or other AI-attribution trailers to commits, PR bodies, or public docs.
- Do not disclose AI authorship of user-attributed writing unless the user has explicitly asked.
