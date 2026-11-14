---
name: verify-brigade
description: "Prove a Brigade change works by driving the real `brigade` CLI against a throwaway temp target, then capture receipts as evidence. Use when you changed anything under src/brigade/ and need proof beyond pytest - init/doctor wiring, work-verify receipts, Grok Bot connector packs, feed manifests, handoff lint, guard scan/redact parity, the Fleet Hub HTTP wire contract. Also use before claiming a CLI behavior in a PR body, handoff, or job report."
---

# Verify Brigade

This skill verifies the `brigade` CLI against a temp target. The optional Center
dashboard is a separate surface. Run the CLI and read what it wrote. This skill
drives it through one helper, `control-brigade.py`, so a proof is a rerunnable
command instead of a story.

Every drive happens in a temp target this skill creates. The helper enforces
that: `--target` is accepted only when it resolves *under* `<state-root>/targets`,
so the operator home, the Brigade checkout, and every other path are refused
before a command runs. The state root gets the same treatment whether it comes
from `--root` or from the default - resolved, refused if it resolves to or
contains the home directory or the checkout, and refused if it is a symlink at
the root or at `targets/`/`captures/`, so a link planted in a world-writable
temp dir cannot redirect a drive. `cleanup` removes only paths `new-target`
recorded. `AGENTS.md` forbids driving the real workspace anyway; here it is not
reachable.

What that paragraph does *not* claim:

- A path under the temp base is allowed even when `$TMPDIR` sits inside the
  home directory. That is the only carve-out, and it is exactly that narrow:
  the home and checkout refusals run *first*, so the allowance applies only
  when the temp base itself resolves inside the home directory and outside the
  checkout. A `$TMPDIR` inside the checkout is still refused, and a relative
  `$TMPDIR` is refused outright rather than resolved against the working
  directory. The temp base itself is refused - a state root there would chmod
  the shared scratch tree to `0700`.
- The default evidence root is `<checkout>/.brigade/verification-evidence`,
  which is inside the checkout. That is the one deliberate exception: it is
  gitignored, and it sits outside the state root on purpose so `cleanup` cannot
  reach the proof. The default and `--evidence-root` are both guarded like
  `--root`, symlink check included: a link planted at either is refused, not
  followed. `--label` names the directory under that root, so it must be a
  simple `[A-Za-z0-9._-]` name of 1-64 characters - no separators, no `..`.
  a caller-supplied path, read for validation only and never written, so an
  operator can validate a real private feed manifest from wherever it lives.
  Everything the drive *writes* still lands in the target or the state root.

## Launch

There is no long-running process. "Launch" means: have a Brigade CLI, and have
a fresh target to drive it against.

```bash
# fresh checkout (this repo). Quote the extras so the shell does not glob them.
python3 -m venv .venv
.venv/bin/pip install -e ".[dev,connector]"
```

The `connector` extra installs the optional MCP and JWT dependencies. The
`grokbot` extra resolves to the same dependencies for the one-release removal
window. The `[dev]` extra supplies pytest, ruff and mypy.

Create the target:

```bash
registry/skills/verify-brigade/control-brigade.py new-target
```

That prints one JSON object. It ran `git init -q -b main` and
`brigade init --depth workspace --harnesses claude,codex` in a new directory
under the state root (`$TMPDIR/verify-brigade/targets/<stamp>-<rand>`), and
recorded the path so `cleanup` can find it later. Ready is
`"ok": true` plus the `"target"` path; export it:

```bash
TARGET=$(registry/skills/verify-brigade/control-brigade.py new-target \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["target"])')
```

`new-target` records the path before it runs `brigade init`, so `cleanup` can
reclaim it even when that init fails; a failed init also removes the directory
it made. `--dry-run` prints the two commands and creates nothing.

To drive an existing directory you already made under `<state-root>/targets`
(instead of letting `new-target` make one), `init` runs the same
`brigade init` against it:

```bash
registry/skills/verify-brigade/control-brigade.py init --target "$TARGET" \
  --depth workspace --harnesses claude,codex
```

Teardown is the `cleanup` section below. Run it after every attempt, including
failed ones, so broken runs do not leave targets behind.

## Doctor

One read-only check that answers "is this rig worth driving?" - before blaming
Brigade for a drive that fails:

```bash
registry/skills/verify-brigade/control-brigade.py doctor
```

It resolves the Brigade command (`--brigade`, then `$BRIGADE_BIN`, then
`.venv/bin/brigade`, then `PATH`, then `python -m brigade`), reports its
version, and checks git, the checkout, and a writable state root. `"ok": true`
means drive. Anything `"fail"` means fix that first.

Against a target that already exists, the equivalent question is:

```bash
registry/skills/verify-brigade/control-brigade.py doctor-target --target "$TARGET"
```

`"summary": {"failed": 0, ...}` is the success signal. `warn` counts above zero
are normal on a fresh target - memory-care, security, and backups are not
initialized yet.

Against this checkout itself (the development tree, not a temp target):

```bash
.venv/bin/brigade doctor --target .
.venv/bin/brigade status --target .
```

Observed here, both exit 0. Doctor printed
`summary: 43 checks, 0 failed, 0 manual` and a list of `warn` rows
(missing operator bootstrap files, unwired memory-care / security). Status
printed group rows (`core`, `skills`, `memory`, `guard`, ...) with `0 fail`
on every group; several groups were `[degraded]` or `[not-installed]` because
this tree is not an initialized operator workspace. Healthy for a Brigade
source checkout is **exit 0 and 0 failed**, not zero warnings.

## Drive

Real commands from this repo, one per mapped feature. The full map is in
[`features/README.md`](features/README.md); drive the feature your change
actually touched, not just the convenient one.

A paused feature can still be driven by hand, but its observations are
about to change: read its header before citing the result.

```bash
C=registry/skills/verify-brigade/control-brigade.py

# init + doctor wiring
$C doctor-target --target "$TARGET"

# work verification receipts (returns the receipt id and status)
$C work-verify --target "$TARGET" --capture verify-brigade

# handoff inbox lint
# PAUSED: the handoff format may change with hub memory v2
$C handoff-lint --target "$TARGET" --content-guard

# guard scan and redact parity (lookbehind and backreference goldens)
$C guard-parity --target "$TARGET"

# Fleet Hub wire contract over HTTP (starts its own throwaway hubs, one of them on a
# stepped clock for the expiry cases; no target needed). --write-golden re-records
# the refusal-text and tombstone golden from the in-repo hub.
$C hub-contract

# Read-only production profile: audit it on a throwaway hub (the only profile allowed against production)
$C hub-contract --profile prod-readonly

# Fleet Hub storage parity: every stored row against the golden (own hub, stepped clock)
$C storage-parity
```

Read the exit code, not the prose:

| exit | meaning |
|---|---|
| 0 | the drive ran and the observation is what a healthy rig produces |
| 1 | the helper itself failed (no Brigade, unsafe path, bad target) |
| 2 | usage error |
| 3 | the drive ran fine, but the observation is a failure |

Stable handles to assert on, in order of preference: the JSON keys the helper
normalizes (`run_id`, `status`, `summary.failed`, `checks[].status`, `valid`,
`reason`), then the receipt directory under
`$TARGET/.brigade/work/verify-runs/<run-id>/`. Do not grep the human prose;
it changes.

Checkout-level focused gates for a change in this repo (no full suite).
`./scripts/verify-focused` itself runs ruff, ruff format `--check`, mypy,
version-sync, the managed and template-profile snapshots, then the named
pytest selectors. The three lint/type pieces can also be run alone:

```bash
./scripts/verify-focused tests/test_skills_cmd.py tests/test_ci_workflow.py
.venv/bin/ruff check src/brigade
.venv/bin/ruff format --check src/brigade
.venv/bin/mypy
```

Receipted form — same focused command, recorded as a work-verify receipt
(`brigade work verify run --help` lists `--argv-json`, `--capture`, and
`--json`; `--help` itself exits 0):

```bash
.venv/bin/brigade work verify run --target . \
  --argv-json '["./scripts/verify-focused","tests/test_skills_cmd.py","tests/test_ci_workflow.py"]' \
  --capture brigade-work
```

`--command` and `--argv-json` receipts are audit-only. Do not pass
`--no-reuse` to the full `./scripts/verify` gate. Do not run
`./scripts/verify-fast` or the full suite from this skill.

## Evidence

```bash
registry/skills/verify-brigade/control-brigade.py evidence --target "$TARGET" --label <what-you-proved>
```

Writes `.brigade/verification-evidence/<stamp>-<label>/` in the checkout and
prints the path as `evidence_dir`. It contains `captures/` (every command the
helper ran, with argv, exit code, stdout, stderr, duration),
`target-brigade/work/verify-runs/` (the real receipts Brigade wrote), and
`manifest.json`. That directory is outside the state root, so `cleanup` cannot
reach it.

Proof standards for this repo:

- Drive the CLI path a user runs. Do not import `brigade.*` in Python and call
  the function; a passing unit test is not a proof that the command works.
- Capture the action and the resulting state. A `work-verify` proof is the
  command record **and** the receipt directory it names.
- Verify side effects on disk as well as command output. Compare the target
  files before and after preview commands.
- Every controller command above drives the CLI against the temporary target.

Checkout `work verify` receipts live at
`.brigade/work/verify-runs/<run-id>/receipt.json`. Cite the `run_id` in the
PR body (`work-verify receipt: <run-id>`). A passing receipt has
`"status": "completed"` and `commands[].exit_code` of `0`. See
`docs/receipt-schemas.md` and the citation paragraph at the end of this
skill.

## Cleanup

```bash
registry/skills/verify-brigade/control-brigade.py cleanup --dry-run   # see it first
registry/skills/verify-brigade/control-brigade.py cleanup
```

Cleanup removes only paths recorded by `new-target` and only when they sit
under `<state-root>/targets/`; anything else is skipped with a reason. It never
kills processes by name, and it never removes an evidence directory. Use
`--target` to drop one target, `--keep-captures` to leave the raw command
records in the state root.

After cleanup, confirm the proof survived:

```bash
ls .brigade/verification-evidence/<stamp>-<label>/manifest.json
```

`.venv/` is gitignored (`.gitignore` line `.venv/`). Never commit `.brigade/`
— the root `.gitignore` lists `.brigade/` and `.brigade/work/` among the
ignored paths. Evidence under `.brigade/verification-evidence/` is also
gitignored; copy a receipt id into the PR body instead of adding the
directory. After a checkout-level verify, leave `.venv/` in place for the
next drive; remove it only when you are done with the tree
(`rm -rf .venv`). Do not `git add` either tree.

## Feature map

CLI surfaces in this repo and the tests that cover them. Drive the helper
feature your change actually touched (`features/README.md`); use the
pytest selectors here when the proof is a focused gate. These selectors are
**stand-in proofs**: a passing test is not a drive of the command, so cite it
as a stand-in and never as "driven". Names confirmed present under `tests/`
on this checkout.

| Surface | Command | Tests |
|---|---|---|
| run | `brigade run` | `tests/test_run_cli.py`, `tests/test_run_lifecycle.py`, `tests/test_run_events.py`, `tests/test_run_control.py`, `tests/test_run_resume.py`, `tests/test_run_receipts.py` (34 `tests/test_run_*.py` / `tests/test_runs_*.py` files) |
| work verify | `brigade work verify` | `tests/test_work_cmd_verification.py`, `tests/test_work_cmd_verify_ranking.py`, `tests/test_verify_trial.py`, `tests/test_verification_contract.py`, `tests/test_verify_brigade_skill.py` |
| fleet | `brigade fleet` | `tests/test_fleet_claims.py`, `tests/test_fleet_claim_release.py`, `tests/test_fleet_nodes.py`, `tests/test_fleet_sync.py`, `tests/test_fleet_cloud.py`, `tests/test_fleet_command_deck.py` (58 `tests/test_fleet_*.py` files), `tests/test_verify_brigade_hub_contract.py`, `tests/test_verify_brigade_storage_parity.py` |
| guard | `brigade guard` / `brigade scrub` | `tests/guard/test_cli.py`, `tests/guard/test_engine.py`, `tests/guard/test_rules.py`, `tests/test_scrub.py`, `tests/test_runguard.py`, `tests/test_verify_brigade_guard_parity.py` |

Doctor and status for the checkout itself: `tests/test_doctor.py`,
`tests/test_status.py`.

## Helpers

`control-brigade.py` is the only helper. It is executable, stdlib-only, and
prints one JSON object per call.

```bash
registry/skills/verify-brigade/control-brigade.py --help
```

Subcommands: `doctor`, `new-target`, `init`, `doctor-target`, `work-verify`, `handoff-lint`,
`guard-parity`, `hub-contract`, `storage-parity`, `audit`, `evidence`, `cleanup`.

Global flags: `--brigade "<command>"` to drive a different Brigade build (for
example `--brigade "$(command -v python3) -m brigade"`), `--root <dir>` to
isolate the state root when two runs go at once (a directory under `$TMPDIR`,
not `$TMPDIR` itself), `--timeout <s>` per call.

Every subcommand that writes accepts `--dry-run`, which reports the command it
skipped and touches nothing outside the helper's own private state root (which
every subcommand resolves and creates first): `new-target`, `init`, `work-verify`,
`guard-parity`, `hub-contract`, `storage-parity`, `evidence`, and `cleanup`.
The dry-run envelope carries `"dry_run": true` and a `would_run`,
`would_remove`, or `would_copy` list; `hub-contract` with the default `contract`
profile instead reports `would_launch` (the hub argv, or `null` for `--hub-url`),
`would_create` (the hub's state directory, or `null`), and `lifecycle.would_launch`
(the clocked hub's argv, or `null` with the reason in `lifecycle.skipped`). With
`--profile prod-readonly` it reports the allowed `operations` and the `excluded`
ones.

`tests/test_verify_brigade_skill.py` runs `new-target`, `doctor-target`,
`work-verify`, and `cleanup` in a pytest `tmp_path`, and asserts the path
refusals (including a symlink planted at the default state root), the
unwritable-root JSON envelope, and the `0600`/`0700` permissions, so CI fails
if this helper stops working or stops guarding.
`tests/test_verify_brigade_guard_parity.py` runs `guard-parity` against the
real CLI and asserts the goldens hold, that a drifted engine is reported, and
that `--dry-run` writes nothing.
`tests/test_verify_brigade_hub_contract.py` runs `hub-contract` against the
in-repo hub and asserts zero mismatches, checks the covered routes against
`docs/phase-hub-contract.md`, and runs a deliberately drifting stand-in hub to
prove each drift is reported. `tests/test_verify_brigade_hub_lifecycle.py` runs
a drifting hub on the drive's clock to prove a tombstone read back as sealed and
a reworded refusal are both reported, and pins the golden comparator.
`tests/test_verify_brigade_hub_readonly.py` runs the `prod-readonly` audit on the
in-repo hub, proves each mutation kind fails it, and proves an unlisted case
stops the run before any request.
`tests/test_verify_brigade_storage_parity.py` runs `storage-parity` in every
profile and in a mixed-engine run, requires each to equal its golden, and
changes one stored value at a time (a timestamp format, a JSON separator, a
generation, an expiry, a digest, an id link, a tombstone) to prove each is
caught.

`tests/test_verify_brigade_audit.py` runs `audit` against the shipped map and
against broken copies of it, so CI fails when the map drifts from the CLI.

## Keeping the map honest

The feature map drifts as the CLI changes. When a change adds, renames, or
removes a command or flag, or changes output a feature file asserts on, run
the audit and follow the loop in
[`features/MAINTENANCE.md`](features/MAINTENANCE.md):

```bash
registry/skills/verify-brigade/control-brigade.py audit
```

The audit is read-only. It reads the live command tree from
`brigade completions bash` (plus `--help` for families such as `guard` that
parse their own verbs), checks every active feature's commands still answer
`--help`, and prints one JSON object: `uncovered` commands, `uncovered_flags`,
`stale` features with no recent drive, `paused` and `unreachable` features
with their reasons, and `failures`. Exit 0 means the map matches the CLI
apart from documented gaps. Exit 3 means it does not.

Three rules from that page apply to every proof:

- **Coverage.** A drive covers the commands it runs and nothing else. One
  convenient entry point driven once says nothing about its siblings.
- **Stand-ins are labeled.** A pytest selector, a mock, or a fake `brigade`
  wrapper is a stand-in proof. Say so; never call it driven.
- **Pauses and unreachable marks carry a reason.** A paused feature names
  what unpauses it. An unreachable one names what blocked it. "Not driven
  yet" is a gap, not unreachability.

## Reporting the proof

Do not claim a Brigade behavior without the receipt. When a change is done,
route the repo's own gate through Brigade once:

```bash
brigade work verify run --target . \
  --argv-json '["./scripts/verify-focused","<pytest-selector>"]' \
  --capture brigade-work
```

Then paste the `run_id`, the status, and the `evidence_dir` from this skill.

That receipt is proof for a human reader, not a scored trial. `--command` and
`--argv-json` receipts are audit-only; only `--manifest <id>` produces a receipt
the outcome ratchet can score. Each `--capture` run prints which one it was.
See `docs/outcome-scoring.md`.

Checkout receipts from `brigade work verify run --target .` land at:

```
.brigade/work/verify-runs/<run-id>/receipt.json
```

Cite the `run_id` in the PR body as a single line, for example
`work-verify receipt: <run-id>`. Read it back with
`brigade work verify show <run-id> --json`. A passing receipt
(`docs/receipt-schemas.md`, `brigade.work_verify_receipt` schema_version 2)
has `"status": "completed"`, a `run_id` shaped
`<YYYYMMDD>-<HHMMSS>-work-verify-<hex>`, `commands[].exit_code` of `0`,
and the identity tuple `baseline_commit` / `tree_fingerprint` /
`changes_patch_sha256`. Failures still write a receipt (`"status": "failed"`);
that is the ledger signal, not a missing file.

Connector discovery and setup preview: see [hub-connector.md](features/hub-connector.md).
Run `$C hub-connector --target "$TARGET"` on Linux for an offline preview.

Connector lifecycle: `brigade hub connector`. Use the independent `cloud-agent` profile and optional `connector` install extra. Verify with `tests/test_cloud_agent_profile.py` and `tests/test_dot_transport.py`.
