<!--
Thanks for sending a patch. Keep this short; delete sections that do not apply.
See CONTRIBUTING.md for what lands easily and what needs an issue first.
-->

## What and why

<!-- One or two sentences on the user-visible change and the problem it solves. -->

Closes #

## Type of change

- [ ] Bug fix
- [ ] New channel adapter
- [ ] New hook adapter
- [ ] Docs
- [ ] Refactor with no command-surface change
- [ ] Surface change (message model, config schema, or other public surface) — opened an issue first per CONTRIBUTING.md

## Checklist

- [ ] `go build ./...`, `go vet ./...`, and `go test -race ./...` pass locally
- [ ] Added or updated tests covering the change (including the privacy invariants where relevant)
- [ ] Updated the `Unreleased` section of `CHANGELOG.md` for any user-visible effect
- [ ] No new outbound HTTP except to channel APIs the user configured; no telemetry, update checks, or disk persistence added
- [ ] No personal details, hostnames, real IPs, account names, tokens, or unredacted absolute paths in code, tests, docs, or this PR (use `192.0.2.x` for example IPs and `EXAMPLE` for placeholder tokens)
- [ ] Conventional commit messages, no AI co-authorship trailers
