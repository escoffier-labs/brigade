<!--
Thanks for sending a patch. Keep this short; delete sections that do not apply.
See CONTRIBUTING.md for what lands easily and what needs an issue first.
-->

## What and why

<!-- One or two sentences on the user-visible change and the problem it solves. -->

Closes #

## Type of change

- [ ] Bug fix
- [ ] Extractor / language coverage improvement
- [ ] Docs
- [ ] Refactor with no command or output-shape change
- [ ] Surface change (schema, JSON output, or MCP tool contract), opened an issue first per CONTRIBUTING.md

## Checklist

- [ ] `cargo fmt --check` passes
- [ ] `cargo clippy --all-targets --all-features -- -D warnings` passes
- [ ] `cargo test --all-features` passes
- [ ] Added or updated tests covering the change
- [ ] Updated the `Unreleased` section of `CHANGELOG.md` for any user-visible effect
- [ ] No personal details, hostnames, IPs, account names, tokens, or unredacted absolute paths in code, tests, fixtures, or this PR (run `content-guard` before pushing)
- [ ] No new default runtime dependency, the default build stays network-free, and the MCP server stays read-only
- [ ] Conventional commit messages, no AI co-authorship trailers
