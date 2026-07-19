# AGENTS.md

Brigade is a zero-runtime-dependency Python CLI for agent memory, handoffs, and
local guardrails, published to PyPI as `brigade-cli`. Source lives in
`src/brigade/`, tests in `tests/`, harness/depth/include manifests in
`src/brigade/templates/`.

Installing, evaluating, or adapting Brigade for a user's workspace? Follow
[docs/agents-guide.md](docs/agents-guide.md) instead. This file is for
developing Brigade itself; the contributor policy lives in `CONTRIBUTING.md`.

## Definition of Done

```bash
./scripts/verify
```

It runs the same gates CI blocks on: ruff lint, ruff format check, the
version-sync check, mypy, and the full pytest suite with the coverage floor.
Report the actual result, paste any failure verbatim, and never claim success
you did not observe.

CI-only jobs still cover work that is slower, platform-oriented, or depends on
extra checkout/install context: `content-guard`, `repo-metadata`,
`install-from-source`, `quickstart-smoke`, and `windows-native-acceptance`.
The local `./scripts/verify` gate is the fast completion gate; do not treat it
as a replacement for those CI-only release and public-repo checks.

Setup, if `.venv/` is missing:

```bash
python -m venv .venv && source .venv/bin/activate && pip install -e ".[dev]"
```

## Rules

Format: trigger, then the rule, then what to do instead.

- About to report a code change as complete: run `./scripts/verify` first. If
  you cannot run it, do not guess; report the exact command and error that
  blocked you.
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
- Committing: conventional commits, no co-authorship trailers.

## Orientation

- `src/brigade/cli.py` is the entry point; commands live in sibling modules
  (`doctor.py`, `selection.py`, `operator_cmd/`, `work_cmd/`, ...).
- New harness adapter? Follow the step list in `CONTRIBUTING.md` (manifest,
  templates, `KNOWN_HARNESSES`, CI matrix, README table).
- Doctor checks return `OK`, `WARN`, `FAIL`, or `MANUAL`. Prefer `WARN` or
  `MANUAL` for optional wiring; `FAIL` means the profile is broken.
- `docs/plans/` and `docs/specs/` are gitignored scratch space; reviewed plans
  live at tracked `docs/phase-*.md` paths.

## Memory Handoff

At the end of any substantial task that produced durable knowledge (root
causes, decisions, gotchas), write a handoff to `.claude/memory-handoffs/`
using the format in `.claude/memory-handoffs/TEMPLATE.md`.
