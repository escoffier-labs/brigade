# Governance

Brigade is a maintainer-led open-source project. Decisions are made in public where practical, with the maintainer responsible for the final call on scope, safety, releases, and project direction.

## Decision principles

Brigade favors:

- local files over hosted state
- explicit commands over background automation
- review queues over silent promotion
- receipts and evidence over trust-me status
- small, reproducible fixes over speculative platform work

Changes that preserve those principles can usually move through issues and pull requests. Changes that alter those boundaries need discussion first.

## What needs discussion first

Open an issue before starting work on:

- new top-level harness support
- new storage backends or hosted services
- network calls that happen outside an explicit user command
- memory promotion behavior
- changes to default privacy, scrub, or security policies
- new runtime dependencies
- breaking changes to generated templates or file paths

## Release authority

The maintainer owns release timing, versioning, PyPI publishing, and security fixes. Release candidates should pass the documented CI and cold-start checks before publishing.

## Community expectations

Project discussion follows the [Code of Conduct](CODE_OF_CONDUCT.md). Technical disagreement is welcome. Personal attacks, private-data leaks, and surprise automation that reaches outside the user's machine are not.

## Sponsored tools and credits

Brigade may apply for open-source programs, credits, and sponsored services that help maintain the public project. Those resources are for Brigade's open-source work only, including docs, CI, security scanning, release automation, and public maintainer workflows. If Brigade ever grows separate commercial services, those services must use separate accounts or paid plans where required by provider terms.
