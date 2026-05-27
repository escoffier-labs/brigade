# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Public `templates/` index that points fresh-start users at the packaged starter templates without exposing local dogfood workspace files.
- Built-in `brigade doctor` bootstrap budget checks that fail hard when installed bootstrap files exceed conservative byte limits.
- Built-in `brigade doctor` memory-card budget checks that fail when `memory/cards/*.md` cards become too large.
- Built-in `brigade doctor` memory-index checks that fail when `MEMORY.md` links to missing `memory/cards/*.md` files.
- `brigade doctor` memory-care freshness checks for stale decay scans, plus hard failures for corrupt scan or refresh-queue JSON.
- `brigade run "<task>"`, a bounded aboyeur flow that asks one rostered orchestrator to plan assignments, dispatches worker CLIs in parallel, then asks the orchestrator to synthesize the final answer.
- `.brigade/roster.toml` loading for cross-model agent rosters using the user's installed CLIs (`codex`, `claude`, or `ollama:<model>`). Claude is optional, not required.
- `brigade roster init` and `brigade roster doctor` to scaffold a Codex/Ollama starter roster and validate roster syntax plus installed CLI availability.
- `brigade dogfood` for a built-in Codex-only, prompt-level read-only, inspected run with artifacts and optional handoff.
- `brigade dogfood init` to persist machine-local dogfood defaults in gitignored `.brigade/dogfood.toml`, enabling a one-command daily `brigade dogfood` path.
- `brigade dogfood status` to report local dogfood readiness, effective paths, CLI availability, ignore coverage, sandbox mode, and latest run.
- `brigade dogfood latest`, `brigade dogfood next`, and per-run dogfood `summary.md` artifacts for turning the latest run into the next work item without copying artifact paths.
- `brigade run --show-plan` and `--verbose` visibility modes, plus defensive runtime enforcement of roster `allow_models`.
- `brigade run --inspect` to print a readable artifact summary immediately after a run completes.
- `brigade run --cwd`, `--output-dir`, and default `.brigade/runs/<id>` artifacts for dogfooding auditable runs.
- Start, finish, and duration metadata in `run.json` artifacts.
- `roster.json` run artifacts that capture the effective orchestrator, agents, limits, allow-list, and timeouts for later review.
- `plan-attempts.json` run artifacts that capture raw planner outputs and parse errors for debugging failed planning runs.
- `synthesis.json` run artifacts that capture orchestrator synthesis status, detail, and raw text for non-dry runs.
- Successful `--handoff` runs now record the written handoff path in `run.json`.
- `brigade run --handoff` to write a Memory Handoff for successful runs, with `--handoff-inbox` override.
- `brigade runs list` to print recent run artifact directories from `.brigade/runs`.
- `brigade runs latest` to show the newest run summary without copying a run path from `brigade runs list`.
- `brigade runs show <run-dir>` to print a readable summary of one run artifact directory.
- `brigade work status` to report the current repo branch, dirty files, dogfood readiness, latest run, and extracted next step for daily work sessions.
- `brigade work start` and `brigade work end` to create local `.brigade/work/` session artifacts for normal daily work loops.
- `brigade work end --handoff` to write a Memory Handoff from closed work session artifacts.
- `brigade work list`, `brigade work latest`, and `brigade work show` to inspect local work session artifacts.
- `brigade work recap` to summarize recent or date-filtered work sessions.
- `brigade work run` to start a work session, run dogfood, close the session, write a work handoff, and print a recap in one command.
- `brigade work resume` to show the active or latest work session, latest dogfood run, extracted next step, and suggested command.
- `brigade work next` to resolve the next daily task without inspecting artifacts, plus `brigade work run` now uses the latest extracted next step when no task is passed.
- `brigade work next --json` to expose the resolved daily task, active session, dogfood snapshot, and suggested command to wrappers.
- `brigade work bootstrap` to initialize and verify the dogfood-backed daily work loop in one command.
- `brigade work brief` and `brigade work brief --json` as a start-of-day entrypoint with git state, latest sessions, latest dogfood run, resolved next task, and suggested command.
- `brigade work tasks` plus `brigade work task add/show/done` to manage a gitignored local task ledger under `.brigade/work/tasks.json`.
- `brigade work run --queue-next` to queue the successful run's extracted next step, with duplicate pending task protection.
- `brigade work import add/list/show/promote` to manage a gitignored local import inbox for scanner-discovered candidate work.
- `brigade work import validate` and `brigade work import ingest` for scanner-authored JSONL import files.
- `brigade work import memory-care` to convert `memory/cards/decay/refresh-queue.json` into local work imports.
- `brigade work import triage` to group pending imports by source and kind.
- `brigade work import dismiss` to close noisy imports without promoting them.
- `brigade work import promote --all` with optional `--source` and `--kind` filters for batch promotion.
- `docs/import-schema.md` documenting the local import JSONL contract for scanners and wrappers.
- Cybersecurity plugin roadmap covering broad agent-workspace security checks plus Brigade-specific scanner, doctor, import, and multi-harness security checks.
- Built-in `security` station and `brigade security scan` for read-only agent workspace security checks.
- Deeper MCP security checks for unpinned `npx`, shell metacharacters, secret-looking env values, sensitive or broad file args, high-risk local commands, large server sets, and missing timeouts.
- Supply-chain security checks for package scripts, GitHub Actions permissions and action refs, Python URL dependencies, and legacy install hooks.
- `brigade security enrich` for explicit post-scan enrichment artifacts, with an offline local provider and opt-in MISP provider config.
- `brigade security scan --import-findings` to route security findings into the local work import inbox for review.
- `brigade security init` to write gitignored local defaults to `.brigade/security.toml`.
- `brigade security fix` to create the local security artifact directory and refresh the managed `.gitignore` block.
- `brigade security review`, `brigade security suppress`, and `brigade security unsuppress` for a local finding review lifecycle with required suppression reasons.
- Security policy presets (`personal`, `public-repo`, `strict`), template scanning controls, stable finding fingerprints, and fingerprint suppressions.
- `brigade security scan --output-dir <dir>` to write redacted `security-report.json` and `security-report.md` evidence bundles.
- `brigade doctor` and `brigade work doctor` now report security config health, latest security evidence bundle status, and local security artifact ignore coverage.
- `brigade doctor` and `brigade work doctor` now warn on stale security suppressions and suppressions missing reasons.
- Security scan secret evidence is redacted before reports or work imports are written.
- `ROADMAP.md` covering the daily-driver path, scanner-ready inbox, chat-surface scanners, memory-card decay refresh, and portable operator setup.
- `brigade work note` to append timestamped checkpoints to the active work session without ending it.
- `brigade work doctor` to check dogfood config, Codex availability, local artifact paths, handoff inbox, ignore coverage, and latest run context for the daily work loop.
- Workspace installs now include `.brigade/memory-care.example.json` as a scanner wiring contract for memory-care decay output.
- Roster-level and per-agent `timeout_seconds` controls for bounded CLI calls.
- `brigade run --read-only` prompt policy for planning and review runs that should inspect and recommend only, with native `codex exec --sandbox read-only` enforcement for Codex agents.

### Changed
- Public repo contents now keep live dogfood workspace files, internal planning notes, and root memory cards untracked; public templates remain under `src/brigade/templates/`.
- Dogfood handoff defaults now use `.codex/memory-handoffs/` for new Codex-driven local configs while preserving explicit configured inbox paths such as `.claude/memory-handoffs/`.
- Bootstrap truncation is now treated as a hard doctor failure to prevent by moving durable detail into memory cards before agents load context.
- Dogfood runs now default to a 600 second per-agent timeout for practical daily repo reviews.
- Dogfood next-step extraction now handles markdown `## Next` sections and can fall back to `summary.md` when `final.txt` does not contain a next-step label.
- `brigade work run` now consumes the oldest pending ledger task before falling back to the latest extracted dogfood next step, and marks consumed tasks done after successful runs.
- `brigade work task add --from-next` now reuses an equivalent pending task instead of adding duplicates.
- `brigade work brief` now includes pending local work imports and import counts in both text and JSON output.
- The managed gitignore block now treats `.brigade/dogfood.toml`, `.brigade/security.toml`, `.brigade/runs/`, and `.brigade/security/` as local state.
- Live smoke docs now keep Codex agent execution in a trusted repo cwd while writing temporary roster, artifacts, and handoff output under `/tmp`.
- Handoff write failures now preserve final run artifacts, print the final answer, return nonzero, and mark `run.json` as `handoff-failed`.
- Dogfood runs default to prompt-level read-only plus Codex's `danger-full-access` sandbox setting for trusted-workspace use so repo inspection works on hosts where native read-only sandboxing blocks shell inspection; `--native-read-only-sandbox` opts into stricter native enforcement.

### Fixed
- `brigade init` now collapses mixed current and legacy managed `.gitignore` blocks into one regenerated Brigade block.

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
