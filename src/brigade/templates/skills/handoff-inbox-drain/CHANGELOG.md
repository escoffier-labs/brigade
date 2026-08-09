# Changelog

## 0.2.0

- Document ingest fingerprint reinforcement (#724): honor near-match / contradiction inbox routing and reinforce-in-place instead of pending manual-only dedup.

## 0.1.0

- Initial bundled handoff-inbox-drain skill: allowlist-bound inbox triage with grounded edits, never-delete retention, and manual dedup pending #724.
- Scope Bash to named `brigade handoff …` commands; document missing-inbox / empty / malformed failure paths.
