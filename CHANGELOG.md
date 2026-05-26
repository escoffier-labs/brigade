# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- `brigade run "<task>"`, a bounded aboyeur flow that asks one rostered orchestrator to plan assignments, dispatches worker CLIs in parallel, then asks the orchestrator to synthesize the final answer.
- `.brigade/roster.toml` loading for cross-model agent rosters using the user's installed CLIs (`codex`, `claude`, or `ollama:<model>`). Claude is optional, not required.
- `brigade roster init` and `brigade roster doctor` to scaffold a Codex/Ollama starter roster and validate roster syntax plus installed CLI availability.
- `brigade run --show-plan` and `--verbose` visibility modes, plus defensive runtime enforcement of roster `allow_models`.
- `brigade run --cwd`, `--output-dir`, and default `.brigade/runs/<id>` artifacts for dogfooding auditable runs.
- `roster.json` run artifacts that capture the effective orchestrator, agents, limits, allow-list, and timeouts for later review.
- `plan-attempts.json` run artifacts that capture raw planner outputs and parse errors for debugging failed planning runs.
- `brigade run --handoff` to write a Memory Handoff for successful runs, with `--handoff-inbox` override.
- Roster-level and per-agent `timeout_seconds` controls for bounded CLI calls.
- `brigade run --read-only` prompt policy for planning and review runs that should inspect and recommend only, with native `codex exec --sandbox read-only` enforcement for Codex agents.

### Changed
- Live smoke docs now keep Codex agent execution in a trusted repo cwd while writing temporary roster, artifacts, and handoff output under `/tmp`.

## [0.6.0] - 2026-05-24

### Added
- Managed tools: external CLIs that Brigade can install and wire per station via `brigade add <station>`. Brigade shells out to each tool, never importing it in process.
- `memory-doctor` and `bootstrap-doctor` attached to the `memory` station.
- `content-guard` attached to the `guard` station.
- New `tokens` station with `tokenjuice` for output compaction.
- `brigade doctor` folds installed managed tools into its report and surfaces each tool's own health. Tools that are not installed are reported as non-failing `[todo]` hints, so doctor stays green on a bare host.
- `memory-doctor` and `bootstrap-doctor` inspect the operator's canonical memory and bootstrap files (host-global), so their findings are labeled operator-scoped and treated as advisory `[warn]`, never failing a workspace `brigade doctor` run.

## [0.5.0] - 2026-05-24

### Changed
- Renamed the project to **Brigade**. The PyPI distribution is now `brigade-cli` and the command is `brigade`. The workspace config directory is now `.brigade`, with a `.solo-mise` read fallback so older installs keep working.

### Added
- Built-in station registry that drives the doctor checks.
- `brigade status` command, alongside `brigade init` and `brigade doctor`, reporting over the station registry.

### Deprecated
- The `solo-mise` command is kept as a deprecated alias for `brigade`.

## [0.4.0] - 2026-05-17

### Breaking
- Removed the `--profile <name>` flag from `solo-mise init`. The flag has been deprecated since v0.3.0 with a stderr migration warning. Use `--depth <minimal|standard|deep>` plus `--harnesses <list>` instead. Migration table in the v0.3.0 notes below.

### Internal cleanup
Removed `src/solo_mise/init.py`, the `templates/profiles/` directory and its six legacy profile manifests, plus `templates.load_profile` and `selection.profile_to_selection`. No user-facing impact beyond the flag removal above.

### Migration

Same as v0.3.0. If you somehow have v0.2.0-era scripts still using `--profile`, see the table in the v0.3.0 section below.

## [0.3.0] - 2026-05-16

### Added
- Two-axis selection model: `--depth {repo,workspace}` + `--harnesses {claude,codex,openclaw,hermes}` + `--include publisher`. Pick any combination of harnesses.
- Interactive prompt on bare `solo-mise init` (no flags). Defaults to claude + repo + no includes.
- `.solo-mise/config.json` is now the per-target source of truth for selection state. Read by `doctor`, `ingest`, and `reconfigure`.
- `solo-mise reconfigure --target . [--prune]` adjusts an existing install to a new selection. `--prune` removes orphaned files for deselected harnesses.
- Per-writer handoff inboxes: `.codex/memory-handoffs/` for Codex (in addition to existing `.claude/memory-handoffs/`).
- Ingester now scans all configured writer inboxes.
- Doctor reports apparent harness shape, checks per-writer inbox, warns on orphaned inbox dirs from unselected harnesses.

### Changed
- README reframed around the two-axis model. New "Picking your harnesses" section walks through four common combos.
- CONTRIBUTING.md "Adding a profile" replaced by "Adding a harness" + "Adding a depth" + "Adding an include".

### Deprecated
- `solo-mise init --profile <x>` still works but prints a stderr deprecation note pointing at the new flags. Will be removed in v0.4.0.

### Migration

If you have v0.2.0 scripts using `--profile`:

| v0.2.0 | v0.3.0+ |
|---|---|
| `--profile repo` | `--depth repo --harnesses claude` |
| `--profile workspace` | `--depth workspace --harnesses claude` |
| `--profile openclaw` | `--depth workspace --harnesses claude,openclaw` |
| `--profile hermes` | `--depth workspace --harnesses claude,hermes` |
| `--profile generic` | `--depth workspace --harnesses none` |
| `--profile publisher` | `--depth repo --harnesses claude --include publisher` |

## [0.2.0] - 2026-05-16

### Added
- Memory-care staleness scaffolding: `memory/cards/decay/` layout and a doctor
  warning when the decay folder is missing, so durable cards do not quietly rot.
- Multi-workspace handoff patterns for users administering more than one agent
  home; secondary workspaces write into their own `.claude/memory-handoffs/`,
  the owner pulls those into a staging inbox.
- TokenJuice output-compaction guidance card covering Claude Code's PreToolUse
  wrapper path, Codex hook setup, and realistic savings expectations.
- Obsidian `/note` skill template under `skills/note/` for the `workspace`
  profile.
- `scripts/backup-restic.sh` template, exposed via the `workspace` profile.
- Managed `.gitignore` block: `solo-mise init` now creates or updates a
  `# >>> solo-mise gitignore block >>>` section in the target's `.gitignore`.
  Re-runs replace only the content between the markers, so user-authored rules
  are preserved. Skip with `--no-gitignore`.
- Release pipeline: `.github/workflows/publish.yml` builds an sdist + wheel on
  every `v*` tag and pushes to PyPI.
- CI matrix: `install-from-source` smoke now runs against all six profiles
  (`repo`, `workspace`, `openclaw`, `hermes`, `generic`, `publisher`).
- Project meta: `CONTRIBUTING.md`, `SECURITY.md`, `CODE_OF_CONDUCT.md`, and
  `.github/ISSUE_TEMPLATE/` (bug, profile-init-fails, ingester-misclassified).

### Changed
- Deepened the `workspace` profile's bootstrap files (`AGENTS.md`, `CLAUDE.md`,
  `IDENTITY.md`, `SOUL.md`, `HEARTBEAT.md`, `MEMORY.md`, `SAFETY_RULES.md`,
  `TOOLS.md`, `USER.md`, `INSTALL_FOR_AGENTS.md`).
- README: centered banner, refreshed badges, added a sample `doctor` run,
  noted that solo-mise makes no network calls, called out `init` idempotency.
- CI now pins the `content-guard` checkout to `v0.1.1` instead of tracking the
  default branch.
- `solo-mise init --profile hermes` prints a louder experimental-status notice
  on stderr in addition to the post-install note.

### Removed
- Stale `DREAMS.md` from the repo root and lingering references in templates.

## [0.1.0] - 2026-05-13

Initial release.

### Added
- `solo-mise` CLI with `init`, `doctor`, `scrub`, and `handoff-template`
  subcommands.
- Six profiles: `repo` (default), `workspace`, `openclaw`, `hermes`,
  `generic`, `publisher`.
- Conservative handoff ingester at `.claude/memory-handoffs/`: safe card
  handoffs become cards, targeted updates append, ambiguous material is
  kicked out for review.
- Content-guard pre-push hook for public-leak prevention.
- Sanitized bootstrap file set, starter memory cards, routing rules.
- OpenClaw adapter fragments and harness-aware doctor checks.
- Experimental Hermes adapter fragments.

[Unreleased]: https://github.com/solomonneas/solo-mise/compare/v0.6.0...HEAD
[0.6.0]: https://github.com/solomonneas/solo-mise/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/solomonneas/solo-mise/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/solomonneas/solo-mise/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/solomonneas/solo-mise/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/solomonneas/solo-mise/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/solomonneas/solo-mise/releases/tag/v0.1.0
