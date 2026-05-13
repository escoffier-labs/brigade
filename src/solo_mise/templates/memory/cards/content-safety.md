---
topic: content-safety
category: foundation
tags: [publishing, content-guard, pre-push, scrubber]
---

# Content Safety

`solo-mise` installs a publish gate so private infrastructure does not leak into public docs, commits, or social drafts.

## Default blocked classes

- Private IP addresses and loopback endpoints
- Internal hostnames, usernames, and private domains
- Local service URLs and sensitive ports
- Secrets, tokens, API keys, OAuth material
- Personal contact details and account IDs
- Private project names or unreleased identifiers
- AI attribution trailers (`Co-Authored-By: Claude`, etc.)

## Two layers

1. **Pre-push hook.** `hooks/pre-push` runs `content-guard` against the working tree before every `git push`. Blocks on violations. Inline allow-tags exist for intentional examples.
2. **Deterministic scrub.** `solo-mise scrub --target .` runs the same scanner standalone. Use it before generating public artifacts (blog posts, social drafts, docs PRs).

## Bypass

`git push --no-verify` skips the pre-push hook. Use it only when you understand exactly what you are allowing through. Both `solo-mise scrub` and the hook log every violation so you can audit later.

## Inline allow

If an example genuinely needs a localhost reference: <!-- content-guard: allow localhost-bare -->

```markdown
A local service might run on localhost:8080. <!-- content-guard: allow localhost-port -->
```

## Setup

```bash
git config core.hooksPath hooks
```

If content-guard is not installed:

```bash
git clone https://github.com/solomonneas/content-guard ~/repos/content-guard
```

The hook reads `CONTENT_GUARD_DIR` (defaults to `$HOME/repos/content-guard`) and `CONTENT_GUARD_POLICY` (defaults to `$SCANNER_DIR/policies/public-repo.json`).

## Why this is part of the product

Most leaks are accidental. A blog post mentions a port. A commit message includes an internal IP. A social draft pastes an OAuth profile path. Without a gate, all of those reach the public eventually. The gate runs deterministically on every push, so the question stops being "did I remember to scrub" and starts being "did the scanner say clean".
