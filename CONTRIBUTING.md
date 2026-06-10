# Contributing to Brigade

Brigade is the local-first operator CLI for agent memory, handoffs, and reviewable work receipts. It grew out of [Solomon's Cookbook](https://github.com/escoffier-labs/solos-cookbook), and patches are welcome. Before you start, please skim this file so we both spend our time on the right things.

## What kinds of changes land easily

- **Bug fixes** for `brigade init`, `doctor`, `scrub`, quickstart, security scanning, or the ingester.
- **Harness / depth / include improvements**: new bootstrap content, sharper post-install notes, better defaults.
- **New harness adapters** (with doctor checks) under `src/brigade/templates/harnesses/<id>.json`.
- **Doctor checks** that catch real, observed failure modes.
- **Test coverage** for any of the above.

## What needs a conversation first

- **A new top-level harness, depth, or include.** Open an issue first describing the user story. These are the public surface and renaming or splitting them later is painful.
- **Breaking changes** to template paths, the handoff TEMPLATE.md fields, or the ingester routing rules.
- **Anything that adds a runtime dependency.** Brigade has zero runtime deps on purpose, and we want to keep it that way.

## What does not land

- Personal details, hostnames, IPs, account IDs, or live auth profiles in templates or tests. The whole point of this kit is to keep that stuff out of public repos. The `content-guard` job in CI will fail if it finds any.
- Cron jobs or hooks that post or call out to the network without explicit opt-in.
- AI-co-authorship trailers on commits (`Co-Authored-By: <model>`). Conventional commits only.

## Local dev

```bash
git clone https://github.com/escoffier-labs/brigade.git
cd brigade
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest -q
```

To smoke-test an install end-to-end the same way CI does:

```bash
target="$(mktemp -d)"
git init -q -b main "$target"
python -m brigade init --target "$target" --depth workspace --harnesses claude,codex,openclaw
python -m brigade doctor --target "$target"
```

## Adding a harness

A harness is a manifest under `src/brigade/templates/harnesses/<id>.json` plus any template files it references. The manifest declares `role: "writer"` (gets an inbox) or `role: "reader"` (gets adapter fragments).

To add a harness:

1. Create the manifest at `src/brigade/templates/harnesses/<id>.json`.
2. Add template files under a harness-named directory, for example `src/brigade/templates/<id>/`.
3. Add the harness id to `KNOWN_HARNESSES` in `src/brigade/selection.py`.
4. Update `HARNESS_PRIORITY` if the new harness should be an owner candidate (readers usually want to land near OpenClaw/Hermes in the priority list).
5. If it is a writer, add it to `WRITER_INBOXES` in `src/brigade/selection.py` and update any writer-specific installer, doctor, or ingest tests.
6. Add the harness to the CI matrix in `.github/workflows/ci.yml`.
7. Add a row to the harness table in `README.md`.

## Adding a depth

Depths live at `src/brigade/templates/depth/<id>.json` and may use `extends` to inherit from another depth. Add the id to `KNOWN_DEPTHS` in `selection.py` and to the `--depth` choices in `cli.py`.

## Adding an include

Includes live at `src/brigade/templates/includes/<id>.json`. Add the id to `KNOWN_INCLUDES` in `selection.py`.

## Adding a doctor check

Check functions live in `src/brigade/doctor.py` and nearby command modules. Each returns structured status data where status is `OK`, `WARN`, `FAIL`, or `MANUAL`. Prefer `WARN` or `MANUAL` over `FAIL` for things the user can choose not to wire up - `FAIL` should mean "this profile is broken."

## Promoting an experimental adapter

The Hermes adapter is currently marked experimental. To graduate it (or any future experimental adapter) to "tested":

- A doctor check exists that meaningfully exercises the adapter against a real install.
- Someone has run the full init + doctor cycle on a real Hermes workspace and reported it on an issue.
- The post-install notes no longer say "experimental".

Open a PR with all three and we'll land it.

## Filing issues

Please use the templates under `.github/ISSUE_TEMPLATE/` - they exist to save you from re-typing the version and install shape every time.

For first-run setup failures, use the "Quickstart setup problem" or "Init or doctor fails" form. The most useful report is the redacted `issue_report` from:

```bash
brigade operator quickstart --target <repo> --harnesses codex --json
```

Before posting output, remove tokens, private hostnames, private repo names, private account names, and unredacted absolute paths. Good labels for setup reports are `quickstart`, `setup`, `harness`, `docs`, and `security-scan`.

The `ingester-misclassified` template is the most useful one to file early. If a handoff that should have promoted to a card got bounced (or vice versa), that is a real bug in the routing rules, not a corner case. We want to see it.

## License

By contributing you agree that your contribution is licensed under the MIT License, same as the rest of the repo.
