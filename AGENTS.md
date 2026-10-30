# AGENTS.md

Brigade is a zero-runtime-dependency Python CLI for agent memory, handoffs, and
local guardrails, published to PyPI as `brigade-cli`. Source lives in
`src/brigade/`, tests in `tests/`, harness/depth/include manifests in
`src/brigade/templates/`.

Installing, evaluating, or adapting Brigade for a user's workspace? Follow
[docs/agents-guide.md](docs/agents-guide.md) instead. This file is for
developing Brigade itself; the contributor policy lives in `CONTRIBUTING.md`.

## Definition of Done

During development and before opening a PR, run relevant focused checks through Brigade:

```bash
brigade work verify run --target . --argv-json '["./scripts/verify-focused","<pytest-selector>"]' --capture brigade-work
```

`./scripts/verify-focused` runs ruff lint, ruff format check, the version-sync
check, managed snapshot, template-profile snapshot, mypy, and only the named
pytest selectors. Report the actual result, paste any failure verbatim, and
never claim success you did not observe.

Required CI supplies the final full-suite and coverage gate for code changes.
Keep all required CI, coverage, review, and merge-queue gates intact. A successful
equivalent required CI run does not require a duplicate local full run before
PR or merge completion, including verification-infrastructure changes.

Local full verification is explicit opt-in for CI reproduction, broad offline
validation, or an operator request. When needed, run it through Brigade:

```bash
brigade work verify run --target . --argv-json '["./scripts/verify"]' --timeout 3600 --capture brigade-work
```

`--timeout 3600` bounds the known-long integration gate and is not permission
to retry status 75.

Keep receipt reuse enabled for unchanged code and environment. If dependencies
or environment state changed and a fresh local full run was explicitly selected,
add `--no-reuse` to the Brigade command above. This reruns the existing gate
under the same admission limits. Never use it to retry contention or make a
duplicate full run mandatory for PRs or merges. Status 75 means all full
verification slots for this user on this host are occupied. It must not be retried
with a larger timeout. Use `./scripts/verify-focused` or wait for an active
Brigade receipt. Set `BRIGADE_VERIFY_WAIT=<seconds>` before starting a gate
to wait for a slot within a bounded deadline.

The full gate reuses CI's deterministic pytest hash shards, combines their
coverage, and enforces the 78% floor once. `BRIGADE_VERIFY_SHARDS` overrides
N (default: CPU count / 2, rounded down, bounded to 1 through 4).
`BRIGADE_VERIFY_SLOTS` overrides K (default: 1, independent of N).
The default pool is shared across linked worktrees and separate clones under
`/tmp/brigade-verify-<uid>/`, with owner-only directory and file permissions.
Overrides must be positive integers. Coordinate overrides across all callers
on the host: each must use the same K and lock path to share the limit.
`BRIGADE_VERIFY_LOCK_PATH` names an existing slot directory or a file prefix.
Changing it creates a separate admission pool. Do not remove active lock files.
The focused gate runs only named selectors, without coverage or full-gate slots.

Reuse consults only the latest receipt overall in the target workspace. That
receipt itself must be completed and passing, with the same tree fingerprint
and planned command. A newer focused receipt prevents reuse of an older full
receipt. Do not create another cache. Reuse does not discover another
clone's receipts, wait for an active run, or certify changed environments,
dependencies, or external state. An audit receipt is not required CI evidence.

CI-only jobs still cover work that is slower, platform-oriented, or depends on
extra checkout/install context: `content-guard`, `repo-metadata`,
`install-from-source`, `quickstart-smoke`, and `windows-native-acceptance`.
The local `./scripts/verify` gate is the full coverage gate. Do not treat it
as a replacement for those CI-only release and public-repo checks.

Setup, if `.venv/` is missing:

```bash
python -m venv .venv && source .venv/bin/activate && pip install -e ".[dev]"
```

## Rules

Format: trigger, then the rule, then what to do instead.

- About to report a code change as complete: run focused verification through
  `brigade work verify run` with `./scripts/verify-focused` and the relevant
  pytest selector. Required CI supplies final suite and coverage evidence.
  `./scripts/verify` is optional local full validation. If you cannot run the
  focused gate, do not guess. Report the exact command and error that blocked
  you. Status 75 from the full gate must not be retried with a larger timeout.
- A test fails after your change: never weaken, skip, or delete the test to
  make it pass. Fix the code. If you believe the test itself is wrong, say so
  explicitly and get agreement before touching it.
- Pushing (only when explicitly asked): this repo has a content-guard pre-push
  hook. Never bypass it with `git push --no-verify`. Fix the flagged leak, or
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
- Bumping the version: update `pyproject.toml`, `src/brigade/__init__.py`, and
  every template `_brigade_version` field together. `./scripts/verify` checks
  the sync.
- Committing: use conventional commits. In escoffier-labs organizations and original solomonneas repositories, commits for substantial coding work should carry the coding agent's co-author trailer. External repositories and upstream third-party PRs remain trailer-free.

## Orientation

- The process entry point is `brigade.cli:process_main` (`[project.scripts]` in `pyproject.toml`). `brigade.cli:main` remains the integer-returning library API.
  Command families live in `src/brigade/cli/` — one module per group (e.g. `doctor.py`,
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

At the end of any substantial task that produced durable knowledge (root
causes, decisions, gotchas), write a handoff to `.claude/memory-handoffs/`
using the format in `.claude/memory-handoffs/TEMPLATE.md`.

## Cursor Cloud specific instructions

Brigade is a zero-runtime-dependency Python CLI — there is no long-running
server or web UI to start. "Run the application" means exercising the CLI
against a temp directory (never the operator home or this checkout).

### Services / surfaces

| Surface | Required? | Notes |
|---|---|---|
| Python CLI (`brigade` via `.venv`) | Required | `source .venv/bin/activate` then use `brigade` / `python -m brigade` |
| Native engines (`brigade setup`) | Optional | Code graph / evidence binaries; not needed for unit tests or init+doctor |
| Model seats / `brigade run` | Out of scope here | No seat credentials in this VM — do not run `brigade run` |
| Rust/Go engines under `engines/` | Optional | Built in CI only when those paths change |

### Standard commands

See AGENTS.md Definition of Done and CONTRIBUTING.md. Day-to-day:

- Lint/type/sync gate pieces: `ruff check .`, `ruff format --check .`, `mypy`,
  `python scripts/version_sync.py --check`, `python scripts/managed_snapshot.py --check`
  (or `./scripts/verify-focused <pytest-selector>` for a development slice)
- Tests: `pytest -q` (full suite; expect a long run, ~30+ minutes)
- Development and pre-PR gate: `./scripts/verify-focused <pytest-selector>`.
- Optional local full gate: `./scripts/verify`. `./scripts/verify-fast` delegates
  to the same admission and shard limits. Positive integer `JOBS` values remain
  a deprecated alias for `BRIGADE_VERIFY_SHARDS`. Unset `JOBS=auto` for defaults.
- End-to-end smoke (same shape as CI `install-from-source`):

```bash
target="$(mktemp -d)"
git init -q -b main "$target"
python -m brigade init --target "$target" --depth workspace --harnesses claude,codex
python -m brigade doctor --target "$target"
```

Doctor may report `WARN`s on a fresh temp target (memory-care / security not
initialized yet); `0 failed` is the success signal.

### Beads (`bd`)

`bd` is installed at `/usr/local/bin/bd` from the linux-amd64 release tarball.
Pinned during environment setup to **bd 1.1.2**
(`bd version 1.1.2 (20e493e56: HEAD@20e493e569c9)`), fetched from the
`steveyegge/beads` → `gastownhall/beads` release assets. Beads is young and
schema-mobile — if `bd` commands fail after an upstream release, re-check the
installed pin before assuming a Brigade bug.
