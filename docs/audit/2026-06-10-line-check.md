# line-check report: brigade (2026-06-10)

## Verdict

Brigade is healthy. CI is green on main, all 1038 tests pass locally in 101 seconds, the release machinery is airtight (version in `pyproject.toml`, `__init__.py`, and the latest tag all agree at 0.10.1, and the publish workflow enforces that match), zero open issues, zero TODO markers in source, zero runtime dependencies by design, and the gitignore is one of the most carefully reasoned I have audited. The single most important thing to do is add a lint/format gate to CI: a 66k-line Python codebase written largely by coding agents has no ruff, black, mypy, or any style/type check anywhere, which is the one place quiet drift can still accumulate. After that, refresh the stale QUICKSTART.md. Everything else is polish on an already well-run line.

## Scorecard

| Station | Score (0-5) | Summary |
|---|---|---|
| 1. Docs and onboarding | 4 | README is excellent and its quickstart commands are smoke-tested verbatim in CI; QUICKSTART.md has drifted (4-harness prompt example vs 16 actual, dead file references) |
| 2. Agent-readiness | 4 | AGENTS.md accurate for install-agents, CONTRIBUTING covers dev loop; `brigade handoff doctor` healthy with 2 warnings, `memory care scan` clean (0 cards, repo does not own memory) |
| 3. Tests and CI | 4 | 1038 tests pass (verified locally), CI green with 3.10-3.12 matrix, content-guard scan, 8-combo install smoke, metadata checks; no lint/format/type gate at all |
| 4. Hygiene | 5 | MIT LICENSE, exemplary gitignore (agent dirs, dogfood state), no secrets or private hostnames in tracked files (only a test fixture `"super-secret-value"`); minor branch litter |
| 5. Structure | 3 | work_cmd split landed in 0.10.0, but five ~5k-line command monoliths remain plus an 11.4k-line test file; zero runtime deps is a deliberate strength |
| 6. Release hygiene | 5 | Keep-a-Changelog maintained same-day, tag v0.10.1 = pyproject = `__version__`, HEAD only 1 docs commit past tag, publish workflow verifies tag/version match |
| 7. TODO and issue mining | 5 | Zero TODO/FIXME/HACK in `src/`, zero open issues, zero open PRs; forward work lives in ROADMAP.md and phase plans |

## Findings

### [MEDIUM] Wire a lint and format gate into CI
- **Station:** Tests and CI
- **Where:** `.github/workflows/ci.yml`, `pyproject.toml`
- **What:** No linter, formatter, or type checker is configured anywhere: no ruff/black/flake8/mypy/pyright in pyproject, CI, or pre-commit. CI runs pytest, content-guard, metadata checks, and install smokes only.
- **Why it matters:** 66k lines of source written by multiple coding agents with no style or static-analysis gate means unused imports, shadowed names, and type drift accumulate silently until they become bugs the test suite happens not to cover.
- **Fix:** Add `ruff` to the dev extra, run `ruff check` + `ruff format --check` as a CI job, fix the initial fallout in one commit. Optionally add mypy later as a separate, advisory job.
- **Effort:** S (ruff gate) to M (if the first `ruff check` run surfaces a large backlog)

### [MEDIUM] Refresh or retire the stale QUICKSTART.md
- **Station:** Docs and onboarding
- **Where:** `QUICKSTART.md` (lines 106-107, interactive example block)
- **What:** The `brigade init` prompt example shows only 4 harnesses while `src/brigade/prompt.py` now offers 16; lines 106-107 point readers at `memory/cards/memory-care-staleness.md` and `memory/cards/token-glace-output-compaction.md`, which are gitignored (`/memory/`) and do not exist in a public clone. QUICKSTART.md is also not linked from README (only `docs/technical-guide.md:951` references it) and overlaps `docs/first-10-minutes.md`.
- **Why it matters:** It is a top-level file new users will open first by filename convention, and it teaches an outdated UI and dead links, undercutting an otherwise polished first-contact story.
- **Fix:** Either update the prompt example and replace the two `memory/cards/` references with links to docs that exist, or fold QUICKSTART.md into `docs/first-10-minutes.md` and update the technical-guide link.
- **Effort:** S

### [MEDIUM] Continue the monolith split: five command modules near 5k lines
- **Station:** Structure
- **Where:** `src/brigade/work_cmd/services.py` (5983), `src/brigade/tools_cmd.py` (5729), `src/brigade/repos_cmd.py` (5092), `src/brigade/cli/` (was a single ~5k-line `cli.py` module), `src/brigade/phases_cmd.py` (4719); plus `tests/test_work_cmd.py` (11417)
- **What:** The 0.10.0 work_cmd package split was the right move, but five modules remain near or above 5k lines, and the single largest file in the repo is one test module. The project already tracks this in `docs/simplification-audit-plan.md` and `docs/work-cmd-split-plan.md`, so this is acknowledged, unfinished work, not news.
- **Why it matters:** A newcomer (or agent) cannot predict where the next change belongs inside a 5k-line module, and an 11k-line test file makes targeted test runs and review diffs painful.
- **Fix:** Repeat the work_cmd recipe (facade + frozen-surface test) on `tools_cmd.py` and `repos_cmd.py` next, and split `test_work_cmd.py` along the same package boundaries already created (`constants`/`helpers`/`ledger`/`config`/`services`/`session`).
- **Effort:** L

### [LOW] Fix the two dogfood handoff-doctor warnings
- **Station:** Agent-readiness
- **Where:** `.brigade/handoff-sources.json` (local state, not tracked)
- **What:** `brigade handoff doctor` warns twice: the configured `.opencode/memory-handoffs` inbox does not exist, and the configured ingestor log at `.brigade/handoff-ingest/latest.log` is missing.
- **Why it matters:** The flagship repo's own wiring should doctor clean; these are exactly the silent-coverage gaps the README says Brigade exists to catch.
- **Fix:** Either create the opencode inbox (`brigade reconfigure --target . --harnesses ...`) or drop it from watched sources via `brigade handoff sources init`; run the ingester once (or remove the log expectation) to clear the second warning.
- **Effort:** S

### [LOW] Prune merged and stale branches
- **Station:** Hygiene
- **Where:** repo-wide (`git branch -a`)
- **What:** Merged but undeleted: local `codex/scanner-ready-inbox-workflow` and `docs/readme-roadmap-plain-english` plus their remote twins and `origin/codex/memory-care-multi-workspace`. Unmerged stragglers from May 27: `codex/brigade-rename-doc-cleanup-20260527`, `codex/issue-tdd-loop-phase-2-20260527`. Old remotes: `origin/chore/v0.2.0-release`, `origin/feat/v0.3.0-harness-selection`, `origin/feat/v0.4.0-remove-legacy-profile`. Four `worktree-agent-*` branches are live Claude worktrees and should be left alone.
- **Why it matters:** Stale agent branches make `git branch -a` noise and invite an agent to resume a dead line of work.
- **Fix:** `git branch -d codex/scanner-ready-inbox-workflow docs/readme-roadmap-plain-english`, `git push origin --delete` the merged remotes, and triage the two May 27 codex branches (rebase or delete).
- **Effort:** S

### [LOW] Resolve the tracked-vs-ignored plan-doc policy contradiction
- **Station:** Hygiene
- **Where:** `docs/phase-*.md`, `docs/roadmap-completion-plan.md`, `docs/superpowers/plans/`, `docs/superpowers/specs/` (~5k lines tracked) vs `.gitignore` lines ignoring `/docs/plans/` and `/docs/specs/` as "Internal implementation plans"
- **What:** The gitignore declares internal plans local-only, yet seven phase/completion plan docs and eight superpowers plan/spec docs are tracked and public. Verified to contain no hostnames, IPs, or secrets, so this is a consistency problem, not a leak.
- **Why it matters:** The split rule means the next agent cannot predict whether a new plan doc should be committed, and public users wade through internal sequencing docs (`phase-115-164-plan.md` calls itself "the source of truth for the next production-hardening tranche").
- **Fix:** Decide once: either move tracked plan docs under the ignored `/docs/plans/` path, or update the gitignore comments and add a line to CONTRIBUTING about which planning artifacts are public.
- **Effort:** S

### [INFO] Repo owns no memory cards by design
- **Station:** Agent-readiness
- **Where:** `memory/cards/` (only `decay/` scan state exists; `/memory/` is gitignored)
- **What:** `brigade memory care scan` reports 0 cards, 0 issues. The repo is a writer, not a memory owner, per its own architecture, so this is expected, recorded here so the empty scan is not mistaken for a gap.
- **Fix:** None needed.
- **Effort:** S

## Backlog

1. [MEDIUM/S] Wire a lint and format gate into CI (Tests and CI)
2. [MEDIUM/S] Refresh or retire the stale QUICKSTART.md (Docs and onboarding)
3. [LOW/S] Fix the two dogfood handoff-doctor warnings (Agent-readiness)
4. [LOW/S] Prune merged and stale branches (Hygiene)
5. [LOW/S] Resolve the tracked-vs-ignored plan-doc policy contradiction (Hygiene)
6. [MEDIUM/L] Continue the monolith split: five command modules near 5k lines (Structure)

## Not checked

- Runtime behavior of the full command surface (~100+ public commands per `docs/command-inventory.md`); only `--version`, `handoff doctor`, `memory care scan`, and the test suite were executed. CI's install/quickstart smoke matrix covers the init paths.
- Published PyPI artifact contents vs the repo (`MANIFEST.in` / package-data correctness); the publish workflow was reviewed but no build was run.
- Full prose accuracy of the large docs (`docs/technical-guide.md`, `docs/overview.md`); only link targets and the commands quoted in README/QUICKSTART/AGENTS were verified against the code.
- Git history for secrets (working tree only; the repo runs content-guard in CI which mitigates this).
- Dev-dependency vulnerabilities (runtime deps are zero by design; pytest/playwright dev extras not audited).
- Local-only private dirs (`.claude/`, `.brigade/` internals beyond doctor output, `.venv/`, `build/`, `dist/`) and Windows/macOS portability, per the skill's rule against auditing ignored/generated trees.
