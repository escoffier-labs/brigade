---
topic: content-safety
category: foundation
tags: [publishing, content-guard, pre-push, scrubber]
---

# Content Safety

`brigade` installs a publish gate so private infrastructure does not leak into public docs, commits, or social drafts.

## Default blocked classes

- Private IP addresses and loopback endpoints
- Internal hostnames, usernames, and private domains
- Local service URLs and sensitive ports
- Secrets, tokens, API keys, OAuth material
- Personal contact details and account IDs
- Private project names or unreleased identifiers
- AI attribution trailers (`Co-Authored-By: Claude`, etc.)

## Two layers

1. **Pre-push hook.** `hooks/pre-push` runs Brigade's embedded content guard against the working tree before every `git push`. It blocks on violations. Inline allow-tags exist for intentional examples.
2. **Deterministic scrub.** `brigade scrub --target .` runs the same scanner standalone. Use it before generating public artifacts (blog posts, social drafts, docs PRs).

## Bypass

Do not bypass the hook. Both `brigade scrub` and the hook report every violation so you can review the result locally.

## Inline allow

If an example genuinely needs a localhost reference: <!-- content-guard: allow localhost-bare -->

```markdown
A local service might run on localhost:8080. <!-- content-guard: allow localhost-port -->
```

## Setup

```bash
git config core.hooksPath hooks
```

The guard ships with `brigade-cli`. `CONTENT_GUARD_POLICY` defaults to `public-repo`. `CONTENT_GUARD_DIR` is an explicit compatibility override for older standalone checkouts.

## Why this is part of the product

Most leaks are accidental. A blog post mentions a port. A commit message includes an internal IP. A social draft pastes an OAuth profile path. Without a gate, all of those reach the public eventually. The gate runs deterministically on every push, so the question stops being "did I remember to scrub" and starts being "did the scanner say clean".
