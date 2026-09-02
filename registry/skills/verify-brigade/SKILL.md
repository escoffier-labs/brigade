---
name: verify-brigade
description: "Prove a Brigade change works by driving the real `brigade` CLI against a throwaway temp target, then capture receipts as evidence. Use when you changed anything under src/brigade/ and need proof beyond pytest - init/doctor wiring, work-verify receipts, Grok Bot connector packs, feed manifests, handoff lint. Also use before claiming a CLI behavior in a PR body, handoff, or job report."
---

# Verify Brigade

Brigade has no server and no UI. The surface a user touches is the `brigade`
CLI, and the only honest way to prove a change is to run that CLI against a
target and read what it wrote. This skill drives it through one helper,
`control-brigade.py`, so a proof is a rerunnable command instead of a story.

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
- `grokbot-feed --manifest` is the one flag outside the target contract. It is
  a caller-supplied path, read for validation only and never written, so an
  operator can validate a real private feed manifest from wherever it lives.
  Everything the drive *writes* still lands in the target or the state root.

## Launch

There is no long-running process. "Launch" means: have a Brigade CLI, and have
a fresh target to drive it against.

```bash
# once per checkout, if .venv/ is missing
python -m venv .venv && source .venv/bin/activate && pip install -e ".[dev]"
```

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

## Drive

Real commands from this repo, one per mapped feature. The full map is in
[`features/README.md`](features/README.md); drive the feature your change
actually touched, not just the convenient one.

```bash
C=registry/skills/verify-brigade/control-brigade.py

# init + doctor wiring
$C doctor-target --target "$TARGET"

# work verification receipts (returns the receipt id and status)
$C work-verify --target "$TARGET" --capture verify-brigade

# Grok Bot connector pack lifecycle (implementation-worker is the worker pack)
$C grokbot-pack-setup  --target "$TARGET" --id implementation-worker
$C grokbot-pack-setup  --target "$TARGET" --id implementation-worker --apply
$C grokbot-pack-doctor --target "$TARGET" --id implementation-worker
$C grokbot-pack-canary --target "$TARGET" --id implementation-worker
$C grokbot-pack-remove --target "$TARGET" --id implementation-worker

# feed manifest validation and queue projection
$C grokbot-feed   --target "$TARGET" --sample
$C grokbot-status --target "$TARGET"

# handoff inbox lint
$C handoff-lint --target "$TARGET" --content-guard
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
- Verify the side effect on disk, not just stdout. `grokbot-pack-setup --apply`
  claims it wrote `.brigade/grokbot/packs/<id>.json`; look at the file.
- Preview modes here are genuinely non-mutating, but check rather than trust:
  `grokbot-pack-setup` without `--apply` and `grokbot-pack-remove` without
  `--apply` print a `writes`/`paths` list and touch nothing, and
  `grokbot-feed` validates without enqueueing. Confirm by diffing
  `$TARGET/.brigade/` before and after.
- No mocks. Every command above hits the real CLI. The one boundary that is
  genuinely absent is a running Grok Bot listener, and the skill reads its
  absence as data rather than faking it.

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

## Helpers

`control-brigade.py` is the only helper. It is executable, stdlib-only, and
prints one JSON object per call.

```bash
registry/skills/verify-brigade/control-brigade.py --help
```

Subcommands: `doctor`, `new-target`, `init`, `doctor-target`, `work-verify`,
`grokbot-pack-setup`, `grokbot-pack-doctor`, `grokbot-pack-canary`,
`grokbot-pack-remove`, `grokbot-feed`, `grokbot-status`, `handoff-lint`,
`evidence`, `cleanup`.

Global flags: `--brigade "<command>"` to drive a different Brigade build (for
example `--brigade "$(command -v python3) -m brigade"`), `--root <dir>` to
isolate the state root when two runs go at once (a directory under `$TMPDIR`,
not `$TMPDIR` itself), `--timeout <s>` per call.

Every subcommand that writes accepts `--dry-run`, which reports the command it
skipped and mutates nothing: `new-target`, `init`, `work-verify`,
`grokbot-feed --sample`, `evidence`, `cleanup`, and
`grokbot-pack-setup`/`grokbot-pack-remove` (there it forces preview even with
`--apply`). The dry-run envelope carries `"dry_run": true` and a `would_run`,
`would_remove`, or `would_copy` list.

`tests/test_verify_brigade_skill.py` runs `new-target`, `doctor-target`,
`work-verify`, and `cleanup` in a pytest `tmp_path`, and asserts the path
refusals (including a symlink planted at the default state root), the
unwritable-root JSON envelope, and the `0600`/`0700` permissions, so CI fails
if this helper stops working or stops guarding.

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
