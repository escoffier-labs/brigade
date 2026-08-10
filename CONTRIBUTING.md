# Contributing to Brigade

Brigade is the local-first operator CLI for agent memory, handoffs, and reviewable work receipts. It grew out of [Solomon's Cookbook](https://github.com/escoffier-labs/solos-cookbook), and patches are welcome. Before you start, please skim this file so we both spend our time on the right things.

## Your first PR

External contributions are welcome. This section is the short path; [Pull requests](#pull-requests) below lists the full merge gates.

1. **Pick work.** Look for issues labeled `good first issue` or `help wanted`. If nothing fits, comment on an existing issue or open one before you spend time on a surprise scope.
2. **Claim it.** Comment on the issue that you are working on it. The maintainer holds off internal lanes for **48 hours** so your branch is not steamrolled.
3. **Install and verify locally.** Clone with Python 3.10+, then follow [Local dev](#local-dev): use a virtual environment (required on PEP 668-managed Python and on Windows), install dev dependencies, and run the verification path that matches your change (content-guard scan for docs-only; `./scripts/verify` for code).
4. **Open a pull request.** Required CI checks run automatically on the branch. You do not need to dispatch all required checks yourself.
5. **Review turnaround.** Target an initial review within a few business days once required checks are green. Merge still waits on the formal review and gate rules in [Pull requests](#pull-requests).

## Pull requests

`main` is branch-protected. A dispatched session cannot supply the review artifact required by its own pull request. GitHub enforces required checks and conversation resolution; formal review is maintainer merge policy (branch protection does not currently require approving reviews).

**GitHub-enforced gates** on `main`:

- All required GitHub Actions checks pass. The checks are pinned to GitHub Actions app id 15368.
- All review conversations are resolved.

**Maintainer merge policy** (required before merge; not enforced by GitHub branch protection today):

- A current formal `APPROVED` review exists from a non-author reviewer.
- The approval was recorded after the last push. New commits dismiss stale approvals.
- Keep the branch current with `main` before merge when practical (branch protection does not require strict status checks, so GitHub does not block merge solely for being behind `main`).

The pull request author cannot approve their own pull request. `gh pr review --approve` fails with "Can not approve your own pull request" when the author and reviewer share one GitHub identity.

CodeRabbit is the current external review identity. Its green commit status is not the grading artifact because the status can be green while the formal GitHub review is still `CHANGES_REQUESTED`. Under maintainer policy, merge waits on a current formal non-author `APPROVED` review and all required checks passing.

Inspect the gate before attempting a merge:

```bash
gh pr checks <number> --required
gh pr view <number> --json reviewDecision,mergeStateStatus
```

`reviewDecision` reports `REVIEW_REQUIRED`, `CHANGES_REQUESTED`, or `APPROVED`. `mergeStateStatus` reports states such as `CLEAN`, `BLOCKED`, and `BEHIND`. Neither field exposes unresolved review conversations; GitHub still blocks merge when any remain.

Dispatched sessions should open the pull request, run local verification, and push commits. After the final push, comment `@coderabbitai full review`. The `coderabbitai[bot]` identity records the formal GitHub review. Wait for its current `APPROVED` review before merging. A green CodeRabbit commit status alone does not satisfy this gate.

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
- Commits must use conventional commits. In-house commits in escoffier-labs organizations and original solomonneas repositories should include a co-author trailer for a coding agent that did substantial work. External repositories and upstream third-party PRs remain trailer-free.

## Planning artifacts

Reviewed planning docs are public and tracked: the phase plans (`docs/phase-*.md`), `docs/roadmap-completion-plan.md`, and the superpowers plans and specs under `docs/superpowers/`. They stay in the repo as a record of how the work was sequenced.

`/docs/plans/` and `/docs/specs/` are gitignored on purpose. They are scratch space for in-flight drafts that have not been reviewed for publication. When a plan there is finished and scrubbed, move it to one of the tracked locations above (or delete it).

## Local dev

From a repo clone with Python 3.10+:

```bash
git clone https://github.com/escoffier-labs/brigade.git
cd brigade
python -m venv .venv
```

Activate the environment before installing:

- **POSIX (macOS, Linux):** `source .venv/bin/activate`
- **Windows PowerShell:** `.\.venv\Scripts\Activate.ps1`
- **Windows cmd:** `.venv\Scripts\activate.bat`

Then install dev dependencies:

```bash
pip install -e ".[dev]"
```

On PEP 668-managed Python (many Linux distributions), installing into the system interpreter fails; a virtual environment avoids that.

**Docs-only changes:** CI runs the `content-guard` job on the repo. Locally, scan each edited markdown file before you push (one path per invocation):

```bash
brigade guard scan <edited-file>.md --policy src/brigade/guard/policies/public-repo.json
```

Example when only `CONTRIBUTING.md` and `README.md` changed:

```bash
brigade guard scan CONTRIBUTING.md --policy src/brigade/guard/policies/public-repo.json
brigade guard scan README.md --policy src/brigade/guard/policies/public-repo.json
```

**Code or test changes:** run the full local gate before you push:

```bash
./scripts/verify
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

Depths live at `src/brigade/templates/depth/<id>.json` and may use `extends` to inherit from another depth. Add the id to `KNOWN_DEPTHS` in `selection.py` and to the `--depth` choices in `src/brigade/cli/init.py`.

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

## Agent-facing error and refusal copy

Most Brigade invocations are made by agents. Agents pattern-match remediation
commands out of error strings and automate them. Treat refusal text as a public
API surface.

When writing or changing a user-facing error, refusal, blocker, hint, or
`suggested_next_command`:

1. **State what happened**, then **state what is true now** (left unchanged,
   gated, corrupt, missing, …).
2. **Name a next step only if automating that step unattended is acceptable.**
   Classify the remediation before you inline it:
   - **Safe-to-automate** - copy-pasteable command that is correct on every
     supported platform and does not overwrite, bypass a gate, or mutate shared
     operator state. Name it.
   - **Judgment-required** - describe the situation and point at `doctor`,
     `--help`, or docs. Do **not** inline the mutating command.
   - **Destructive or gate-bypassing** - never name `--force`,
     `--allow-*` overrides, `--operator-confirm`, `--no-verify`, bulk release
     overrides, or similar flags as the remediation. Operators already know
     those flags from `--help`; agents must not learn them from refusals.
3. **Structured before textual.** When a caller might branch on the failure,
   put machine-readable fields on the JSON payload (`reason`, ids, holders,
   expected/observed). Keep the text form from becoming a second parser
   surface (no stable ``by <name>`` suffixes when a typed field exists).
4. **Stay platform-portable.** Do not suggest `chmod`, shell-only paths, or
     other host-specific recipes unless the message is already gated to that
     platform (see verify's high-risk command copy). Prefer describing the
     required end state ("mark the hook executable for your Git hook host").

The audit table for the #739 sweep lives at
[`docs/audit/2026-08-09-error-refusal-copy.md`](docs/audit/2026-08-09-error-refusal-copy.md).
High-traffic contracts are pinned in `tests/test_error_refusal_copy.py`.

## Filing issues

Please use the templates under `.github/ISSUE_TEMPLATE/` - they exist to save you from re-typing the version and install shape every time.

For first-run setup failures, use the "Quickstart setup problem" or "Init or doctor fails" form. The most useful report is the redacted `issue_report` from:

```bash
brigade operator quickstart --target <repo> --harnesses codex --json
```

Before posting output, remove tokens, private hostnames, private repo names, private account names, and unredacted absolute paths. Good labels for setup reports are `quickstart`, `setup`, `harness`, `docs`, and `security-scan`.

The `ingester-misclassified` template is the most useful one to file early. If a handoff that should have promoted to a card got bounced (or vice versa), that is a real bug in the routing rules, not a corner case. We want to see it.

## Credits

Brigade is written and maintained by Solomon Neas. Development runs through the same multi-model seat roster the tool orchestrates, so commits sometimes carry co-author trailers for the coding agents that did substantial work on them.

## License

By contributing you agree that your contribution is licensed under the MIT License, same as the rest of the repo.
