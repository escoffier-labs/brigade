<!--
Thanks for sending a patch. Keep this short; delete sections that do not apply.
See CONTRIBUTING.md for what lands easily and what needs an issue first.
-->

## What and why

<!-- One or two sentences on the user-visible change and the problem it solves. -->

Closes #

## Type of change

- [ ] Bug fix
- [ ] New harness adapter / doctor check
- [ ] Docs
- [ ] Refactor with no command-surface change
- [ ] Surface change (new harness, depth, include, or breaking template/ingester change) — opened an issue first per CONTRIBUTING.md

## Checklist

- [ ] `pytest -q` passes locally
- [ ] Added or updated tests covering the change
- [ ] Updated the `Unreleased` section of `CHANGELOG.md` for any user-visible effect (entries describe effects, not commit subjects)
- [ ] No personal details, hostnames, IPs, account names, tokens, or unredacted absolute paths in code, templates, tests, or this PR (the `content-guard` CI job will fail otherwise)
- [ ] No new runtime dependencies (Brigade is zero-runtime-dep on purpose)
- [ ] Conventional commit messages, no AI co-authorship trailers
