# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Documentation
- README leads with a what / why / how-it-differs opening, a centered title and badge row, a prominent Website link, a keyword-rich "What it does" section, verified `doctor` and `status` output blocks, and "Why not <alternatives>?" plus "What agent-notify is not" sections.
- Added maintainer-health files: `SECURITY.md` (trust model and report path), `CONTRIBUTING.md` (contribution scope and channel/hook adapter guides), `CODE_OF_CONDUCT.md`, GitHub issue templates (`bug.yml`, `feature.yml`, `config.yml` with blank issues disabled and contact links), a pull request template with a no-PII checklist, and this changelog.

## [0.1.1] - 2026-05

### Fixed
- Codex hook now parses the event JSON from the trailing positional argument that Codex CLI appends, instead of stdin, so an unrelated leading argument cannot corrupt the payload.
- A channel send failure now returns exit code `3` (distinct from the config-error code `2`), with the per-channel failure count logged to stderr.
- Telegram Markdown V2 backslash escaping corrected so message bodies render as intended.

## [0.1.0] - 2026-04

### Added
- Initial release: a single-binary notification dispatcher that fans out to Discord, Telegram, and Signal, best-effort and concurrently.
- Routing precedence (`--to`, `--profile`, default profile, all channels) with `--skip` filtering.
- TOML config with env-var-referenced secrets, plus `init`, `status`, `doctor`, `version`, and `hooks print` subcommands.
- Built-in hook adapters for Claude Code (Stop / Notification), Codex CLI notify, and a custom JSON source.
- Privacy posture: no telemetry, no update checks, no persistent state, outbound HTTP only to configured channel URLs, asserted by `cmd/agent-notify/privacy_test.go`.

[Unreleased]: https://github.com/escoffier-labs/agent-notify/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/escoffier-labs/agent-notify/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/escoffier-labs/agent-notify/releases/tag/v0.1.0
