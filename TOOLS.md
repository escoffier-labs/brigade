# TOOLS.md - solo-mise dev runbook

Commands for working on `solo-mise` itself. Not for users of the installed CLI - those live in QUICKSTART.md.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

## Tests

```bash
.venv/bin/python -m pytest -q              # full suite (40 tests)
.venv/bin/python -m pytest tests/test_ingest.py -q   # one file
.venv/bin/python -m pytest -k "promote" -q  # one keyword
```

## Content-guard scan

```bash
PYTHONPATH=$HOME/repos/content-guard/src python3 -m content_guard scan . \
  --policy $HOME/repos/content-guard/policies/public-repo.json
```

Expected: `Clean.` or warn-only. Block-level findings must be fixed or inline-allow-tagged before commit.

## CLI smoke (local install)

```bash
.venv/bin/solo-mise --version
.venv/bin/solo-mise init --target /tmp/sm-smoke --profile workspace
.venv/bin/solo-mise doctor --target /tmp/sm-smoke
rm -rf /tmp/sm-smoke
```

## pipx install from local source

```bash
pipx install --force .
solo-mise --version
```

## Dogfood (install solo-mise into solo-mise)

```bash
.venv/bin/solo-mise init --target . --profile repo --force
```

Lays down `AGENTS.md`, `CLAUDE.md`, `.claude/memory-handoffs/TEMPLATE.md`, and `hooks/pre-push`. After install:

```bash
git config core.hooksPath hooks
```

## Pre-push hook

Once activated (above), every `git push` scans the working tree with content-guard. Bypass with `git push --no-verify` only if the user explicitly accepts the risk.

## Release

See `RELEASE.md`.

## Profiles

| Profile | Files installed |
|---------|-----------------|
| `repo` | AGENTS.md, CLAUDE.md, .claude/memory-handoffs/TEMPLATE.md, hooks/pre-push |
| `workspace` | All bootstrap files + starter memory cards + handoff dirs |
| `openclaw` | workspace + OpenClaw config fragments |
| `hermes` | workspace + Hermes adapter fragments (experimental) |
| `generic` | workspace + contract docs |
| `publisher` | publish gate only (hook + policies + content-safety card) |

## Multi-workspace memory

Use `memory/cards/multi-workspace-handoff-admin.md` as the public-safe pattern for pulling handoffs from secondary agent homes into one canonical owner. Use `memory/cards/memory-care-staleness.md` for the card decay scanner and safe refresh loop.

## Where things live

- Source: `src/solo_mise/`
  - `cli.py` - argparse entrypoint, dispatches to subcommands
  - `init.py` - profile materialization + path safety
  - `doctor.py` - workspace verification, harness-specific checks
  - `scrub.py` - content-guard wrapper
  - `ingest.py` - conservative handoff routing
  - `fragments.py` - OpenClaw/Hermes JSON fragment dump
  - `handoff.py` - print TEMPLATE.md
  - `templates.py` - importlib.resources access + placeholder rendering
- Templates (shipped to users): `src/solo_mise/templates/`
- Tests: `tests/` (pytest, conftest.py provides `tmp_target` fixture)
- CI: `.github/workflows/ci.yml`
