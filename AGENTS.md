# AGENTS.md

Brigade is a zero-runtime-dependency Python CLI for agent memory, handoffs, and
local guardrails, published to PyPI as `brigadestack` (with `brigade-cli` as the same
code under the old name). Source lives in
`src/brigade/`, tests in `tests/`, harness/depth/include manifests in
`src/brigade/templates/`.

Installing, evaluating, or adapting Brigade for a user's workspace? Follow
[docs/agents-guide.md](docs/agents-guide.md) instead. This file is for
developing Brigade itself; the contributor policy lives in `CONTRIBUTING.md`.

## Definition of Done

Run `brigade work verify required --worktree --json` to preview the plan. For a
selective plan of up to 20 files, run the listed files together in one verify run.
For a broader plan, run one relevant focused check and rely on required CI for
the full check.

Before opening a PR, use the entire `run_argv` array from a selective plan.
For example, a plan listing two files can look like this:

```bash
brigade work verify required --worktree --json
brigade work verify run --target . --argv-json '["./scripts/verify-focused","--","tests/test_verify_gate.py","tests/test_verify_required.py"]' --capture brigade-work
```

`./scripts/verify-focused` runs ruff lint, ruff format check, the version-sync
check, managed snapshot, template-profile snapshot, mypy, and only the named
pytest selectors. Report the actual result, paste any failure verbatim, and
never claim success you did not observe.

Required `ci-gate` on the exact head commit supplies the suite and coverage
evidence. After pushing, wait for `ci-gate` on the head SHA. A local receipt is
not evidence that the suite passed. Do not run `./scripts/verify` as part of
normal work, before a PR or merge, inside a review lane, or on `main` to compare
failures. It runs only when the operator asks to reproduce CI locally (see
[Reproducing CI locally](CONTRIBUTING.md#reproducing-ci-locally)) or during a
release cut (`RELEASE.md`). Status 75 means another full gate holds the slot. Do
not retry it.

CI-only jobs still cover work that is slower, platform-oriented, or depends on
extra checkout/install context: `content-guard`, `repo-metadata`,
`changelog-fragments`, `install-from-source`, `quickstart-smoke`, and `windows-native-acceptance`.

Setup, if `.venv/` is missing:

```bash
python -m venv .venv && source .venv/bin/activate && pip install -e ".[dev]"
```

## Rules

Format: trigger, then the rule, then what to do instead.

For protected CI changes, the author does not approve the change and a builder that wrote it never applies the label. The pull request conductor applies `workflow-change-approved` only after an independent security review of the protected-path changes at the current head says to ship, with a comment naming the commit and protected files. A new head needs the label again; it needs a new review only when the protected files changed. No pull request gets more than two reviews without operator agreement. Approval is recorded and audited after the fact, not supplied by a separate account; see [the workflow guard](docs/workflow-guard.md).

- Before opening a PR or reporting a code change complete: run the planned files
  together through `brigade work verify run` as shown above. To skip the preview,
  use the command from Stop or closeout if the receipt misses files. For a broad
  plan, run one relevant focused check. Required `ci-gate` on the exact head commit
  supplies suite and coverage evidence. After pushing, wait for it on the head
  SHA. A local receipt is not suite evidence. Do not run `./scripts/verify` in
  normal work, before a PR or merge, inside a review lane, or on `main` to compare
  failures. Run it only when the operator asks to reproduce CI, as documented in
  CONTRIBUTING.md, or during a release cut.
  If the focused gate cannot run, report the exact command and error.
- A test fails after your change: never weaken, skip, or delete the test to
  make it pass. Fix the code. If you believe the test itself is wrong, say so
  explicitly and get agreement before touching it.
- Pushing: assigned in-house work may push its feature branch and open a PR
  without asking again. Never push to `main`. This repo has a content-guard
  pre-push hook. Never bypass it with `git push --no-verify`. Fix the flagged leak, or
  add a reviewed inline allow-tag (`<!-- content-guard: allow <rule-id> -->`)
  on the offending line.
- Unsure how to build, test, or release: never invent commands. Check
  `pyproject.toml`, `.github/workflows/ci.yml`, and `CONTRIBUTING.md`; if the
  answer is not there, report exactly what is missing.
- Exercising `ingest`, `init`, `operator`, or `quickstart` during development:
  never run them against the real operator workspace or your own home
  directory. Use a temp dir, the way the tests do: the `tmp_target` fixture in
  `tests/conftest.py` builds on `tmp_path`, and the manual smoke test in
  `CONTRIBUTING.md` targets `$(mktemp -d)`.
- Writing templates, fixtures, or tests: no personal details, hostnames, IPs,
  account IDs, or live auth profiles. The content-guard CI job fails on them.
  Use obviously fake placeholders.
- Adding a runtime dependency, or a new top-level harness, depth, or include:
  needs a conversation first. Open an issue per `CONTRIBUTING.md` instead of
  landing it directly.
- A change has a user-visible effect: never edit `CHANGELOG.md`; the
  `changelog-fragments` CI job rejects it outside a release commit. Add
  `changelog.d/<pr-number-or-slug>.md` holding `- ` bullets (see
  `changelog.d/README.md`) and run `python3 scripts/changelog_fragments.py check`.
- Bumping the version: update `pyproject.toml`, `src/brigade/__init__.py`, and
  every template `_brigade_version` field together. Run
  `python scripts/version_sync.py --check` to verify the sync.
- Committing: use conventional commits. In escoffier-labs organizations and original solomonneas repositories, commits for substantial coding work should carry the coding agent's co-author trailer. External repositories and upstream third-party PRs remain trailer-free.
- Posting or editing an issue, pull request, or GitHub comment on an agent's
  behalf: use the attribution block in `CONTRIBUTING.md` ("Agent attribution").
  Start with `Sent on behalf of Solomon by <orchestrator model> via <harness> in <client>`.
  Name the harness for every model in `Models used`. Name only models that
  performed the work, include the provider if the model name is ambiguous, and
  list each review pass. Put the observed checks in the body above the block or
  say `not run`. Do not credit a model for checks it only wrote. Every agent-written
  review comment and summary includes
  `Review by <model> via <harness> in <client>`. Keep the human author identity.

## Orientation

- The process entry point is `brigade.cli:process_main` (`[project.scripts]` in `pyproject.toml`). `brigade.cli:main` remains the integer-returning library API.
  Command families live in `src/brigade/cli/`, one module per group (e.g. `doctor.py`,
  `operator.py`, `init.py`, `work/`); `__init__.py` builds the parser and dispatches
  to each module's `register`/`dispatch`.
- New harness adapter? Follow the step list in `CONTRIBUTING.md` (manifest,
  templates, `KNOWN_HARNESSES`, CI matrix, the writer harness table in
  `docs/overview.md`, and the harness list in `docs/technical-guide.md`).
- Doctor checks return `OK`, `INFO`, `WARN`, `FAIL`, or `MANUAL`. Prefer `WARN` or
  `MANUAL` for optional wiring; `FAIL` means the profile is broken.
- `docs/plans/` and `docs/specs/` are gitignored scratch space; reviewed plans
  live at tracked `docs/phase-*.md` paths.

## Memory Handoff

Write a handoff only when the session produced knowledge a later session
cannot get from the repo, git history or an existing card: a root cause, a
decision with its reason, a new operational fact, or a stated preference. Run
`brigade memory search` first and update that card where one exists. Write it
with `brigade handoff draft`. See the [packaged handoff template](src/brigade/templates/claude/memory-handoffs/TEMPLATE.md) for the format.
It is the supported writer: it publishes atomically (private temp, fsync,
no-replace rename), and a writer that bypasses it must do the same
(`docs/handoff-promotion.md`). If nothing qualifies, end with one line, `Nothing durable: <reason>`, and write
no file. A code or config edit alone is not a reason.

## Cursor Cloud specific instructions

Brigade is a zero-runtime-dependency Python CLI. There is no long-running
server or web UI to start. "Run the application" means exercising the CLI
against a temp directory (never the operator home or this checkout).

### Services / surfaces

| Surface | Required? | Notes |
|---|---|---|
| Python CLI (`brigade` via `.venv`) | Required | `source .venv/bin/activate` then use `brigade` / `python -m brigade` |
| Native engines (`brigade setup`) | Optional | Code graph / evidence binaries; not needed for unit tests or init+doctor |
| Model seats / `brigade run` | Out of scope here | No seat credentials in this VM. Do not run `brigade run`. |
| Rust/Go engines under `engines/` | Optional | Built in CI only when those paths change |

### Standard commands

See AGENTS.md Definition of Done and CONTRIBUTING.md. Day-to-day:

- Lint/type/sync gate pieces: `ruff check .`, `ruff format --check .`, `mypy`,
  `python scripts/version_sync.py --check`, `python scripts/managed_snapshot.py --check`
  (or `./scripts/verify-focused` with the planned files for a development slice)
- Development and pre-PR checks: run focused selectors through Brigade as
  described in the Definition of Done. Required CI on the exact head commit
  supplies the full-suite and coverage evidence.
- Reproduce the full gate locally only when the operator asks, as described in
  [Reproducing CI locally](CONTRIBUTING.md#reproducing-ci-locally).
- End-to-end smoke (same shape as CI `install-from-source`):

```bash
target="$(mktemp -d)"
git init -q -b main "$target"
python -m brigade init --target "$target" --depth workspace --harnesses claude,codex --no-care
python -m brigade doctor --target "$target"
```

Doctor may report `WARN`s on a fresh temp target (memory-care / security not
initialized yet); `0 failed` is the success signal.

### Beads (`bd`)

`bd` is installed at `/usr/local/bin/bd` from the linux-amd64 release tarball.
Pinned during environment setup to **bd 1.1.2**
(`bd version 1.1.2 (20e493e56: HEAD@20e493e569c9)`), fetched from the
`steveyegge/beads` → `gastownhall/beads` release assets. Beads is young and
schema-mobile. If `bd` commands fail after an upstream release, re-check the
installed pin before assuming a Brigade bug.

Session-scratch rule: treat bd as planning scratch. Nothing tracked only in bd
counts as done until it appears as a PR, a Brigade receipt, or a hub record.
`brigade work import bd` mirrors bd issues and edges into the task ledger and
`brigade work export bd-outcomes` writes results back as a file. Brigade runs
only `bd version --json` and `bd export` and never writes bd storage. It
supports bd 1.3.x, so it refuses the 1.1.2 pin above. See
[docs/beads-with-brigade.md](docs/beads-with-brigade.md).
