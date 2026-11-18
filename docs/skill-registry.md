# Install skills with the shadcn CLI

Brigade's skills are published as a [shadcn registry](https://ui.shadcn.com/docs/registry).
The registry distributes skill files, the same way a component registry
distributes components. [remocn](https://github.com/kapishdima/remocn) by
kapishdima ships Remotion video components through this mechanism. Brigade
ships agent skills through it.

## Setup

Add the registry to `components.json` in your project (create the file if you
do not have one):

```json
{
  "$schema": "https://ui.shadcn.com/schema.json",
  "style": "new-york",
  "tailwind": { "config": "", "css": "app.css", "baseColor": "neutral", "cssVariables": true },
  "aliases": { "components": "components", "utils": "lib/utils" },
  "registries": {
    "@brigade": "https://brigade.tools/r/{name}.json"
  }
}
```

Non-React and non-Node projects work. The CLI needs one more file,
`tsconfig.json`, even when the payload is markdown:

```json
{ "compilerOptions": { "baseUrl": "." } }
```

## Install a skill

```bash
npx shadcn@latest add @brigade/bug-hunt
```

This writes `.claude/skills/bug-hunt/SKILL.md`. Claude Code discovers it on the
next session. Available skills: `bug-hunt`, `latent-premises`, `retry-safety`, `reduce`.

The `check`, `refire`, and `taste` skills were retired and are no longer in the
registry. Use `superpowers:verification-before-completion`,
`superpowers:systematic-debugging`, and `superpowers:test-driven-development`
instead.

If you run Brigade, project the skill into other harnesses afterward:

```bash
brigade skills import .claude/skills/bug-hunt
brigade skills install bug-hunt --target all
```

Brigade's own bundled skills use a separate canonical source identity. A named
install such as `brigade skills install brigade-work --target cursor` resolves
the current template shipped by the installed Brigade package. Use
`registry:brigade-work` or an explicit path only when you intend to select a
same-named local registry copy. `brigade skills fleet status` reports stale
harness copies and the exact update command for each supported copy. If current
metadata no longer supports an installed harness, it reports an uninstall
command instead of recommending another install.

### Hand-edited installed copies

An installed copy whose bytes differ from its install receipt
(`drift.local_edit` is `changed`) carries a hand edit, and nothing overwrites it
unless you say so:

- `brigade skills fleet status` reports it as `edited`, counted in
  `edited_count` and left out of `stale`. It prints no `update:` line and sets
  `update_command` to null, so no command generated from fleet output can
  clobber the edit. Instead it prints a `diff:` line (`brigade skills diff ...
  --against registry`) and a `discard:` line, the override below. The Hub node
  report still sends `stale` for an edited copy, so the closed status set the
  Hub accepts is unchanged.
- `brigade skills sync` plans it as `edited` with result `kept` and a reason
  naming the diff command and the override. `--write` makes no mutation, writes
  no receipt or history row and takes no snapshot for it. The edit state is
  checked again against the exact bytes the write would replace, so an edit made
  after the dry run is still kept. A copy with no valid receipt cannot prove it
  is pristine, so sync keeps reporting it as `blocked`.
- `brigade skills install <skill> --target <harness> --force` on an edited copy
  exits 2 before any snapshot or removal, naming the installed path, the diff
  command and the override. With `--target all`, the edited harness is recorded
  in `skipped` and the other harnesses install as before. A legacy copy with no
  receipt stays replaceable with `--force`, because nothing shows it was edited.
- `--discard-local-edits` (together with `--force`) is the explicit override. It
  restores the earlier behavior: a rollback snapshot of the edited copy is taken
  first, then the copy is replaced.

- `brigade skills rollback <skill> --target <harness>` on an edited copy exits 2
  before it deletes anything, naming the installed path, the diff command and
  `--discard-local-edits`. With the flag, the edited bytes are snapshotted under
  the rollback tree (named in the rollback receipt as `edit_snapshot`) before the
  copy is rolled back, so the edit stays recoverable.
- `brigade outcome reconcile --apply` is routing only ([decision on #2589](https://github.com/escoffier-labs/brigade/issues/2589#issuecomment-6965099265)):
  it never installs, rolls back or uninstalls a skill file, so it cannot touch
  an edited copy at all. It lists the `skills install` or `skills rollback`
  command that would apply a status change, and you run it. See
  `docs/outcome-scoring.md`.
- The edit check compares every installed file except the root `skill.json`
  provenance file, which its own metadata fingerprint covers. A nested
  `skill.json` (for example `assets/skill.json`) is ordinary content, so an edit
  to it counts. Receipts record this digest as `installed.edit_fingerprint`.
  A receipt written before it existed is still compared with the earlier digest,
  except that a copy holding a nested `skill.json` reads as `unknown` until it is
  reinstalled, because the earlier digest cannot prove that file untouched.
- A receipted copy whose `SKILL.md` is gone while other files remain is
  `unknown` in sync (dry run and `--write`) and in `fleet status`, never
  `missing`. Sync leaves it alone; `install --force` repairs it.
- `sync --write` binds every installed file of an updated copy, unchanged ones
  included, to the bytes it planned against. An edit made between planning and
  writing aborts the write with exit 1 before any receipt or snapshot is
  written. The projection kernel checks each destination's digest once more
  immediately before it applies that mutation (and the before-image it captures
  must match the plan), so an edit made after the prepare-time check is never
  overwritten: the operation restores the mutations already applied and reports
  `restored` with exit 1. `fleet status` rows always carry `diff_command` and
  `discard_command` (null unless the copy is edited).
- `install --force` and `rollback` bind their removal to
  the bytes the edit check saw. Immediately before each removal or replacement
  they re-verify the copy (directory inode and device plus a digest of every
  byte); a hand edit or a swapped directory that lands after the check aborts the
  command with exit 2 and changes nothing. The snapshot written for the aborted
  command is dropped.
- The re-verify is bound to the mutation by quarantine-then-verify, because a
  check followed by a separate pathname removal still leaves a window.
  `install --force` and `rollback` rename the installed directory to a fresh
  unpredictable `.<name>.<random>.quarantine` name in the same parent through a
  held parent descriptor (the Windows handle equivalents there), then compare
  the moved directory's inode, device and bytes with what the check saw. A match
  publishes the new content (which refuses an entry that appeared at the name)
  and deletes the moved copy; a mismatch renames it back, never over a new
  entry, and aborts with exit 2. A write that lands after the rename can only
  reach the moved copy, so the comparison sees it. The projection kernel does
  the same for a replaced or removed file in `sync --write`: the destination is
  moved aside, its digest is checked against the plan, and a mismatch restores
  it and reports the drift. Custom writer and remover callbacks, which merge
  into the live file themselves, keep the digest check alone. A crash between
  the rename and the cleanup can leave a hidden `.quarantine` entry beside the
  destination, which is safe to delete after inspection.
- `skills rollback` restores whatever the install receipt's snapshot pointer and
  fingerprint name, and both live in the workspace and are not authenticated, so
  the agent's own user could have chosen them. Text output prints a warning on
  stderr and the snapshot path and its recorded fingerprint before anything is
  restored (JSON output carries `warning` and `snapshot_fingerprint`). The
  `brigade outcome reconcile` text output prints the same warning and an
  `inspect first:` line (`brigade skills history <id> --target <workspace>
  --harness <harness> --json`, which shows the receipt's snapshot pointer and
  fingerprint) before every suggested rollback command. Which snapshot a
  rollback uses is unchanged.
- A copy with a missing, corrupt or digest-less receipt reads as `unknown`. A
  manual `install --force` on it stays operator-driven and repairs it. No
  unattended path writes over it: the receipt sits in the workspace the agent's
  own user can write, so forgeable state may veto a change but never authorize
  one, and `outcome reconcile --apply` changes no skill file at all.

Every copy that is replaced, edited or not, still gets its rollback snapshot.

Use `brigade skills sync --workspace . --target all --trust workspace` to plan
the complete registry-to-harness matrix without changing files. Add `--write`
to install only missing or changed pairs at or above the trust floor. Sync uses
explicit registry identities, so a same-named bundled skill cannot replace the
reviewed registry source. Completed targets keep their receipts and rollback
snapshots if another target fails.

## What gets installed

One markdown file per skill, vendored from
[escoffier-labs/skillet](https://github.com/escoffier-labs/skillet), with links adjusted for this distribution. No
dependencies, no code execution, no config changes. Read the file before you
adopt it, like anything else that instructs an agent.

## Process obligations (advisory)

Registry `skill.json` may declare an additive `obligations` list. Each entry
has `id`, `kind` (`check` | `review` | `handoff`), optional `required`
(default `true`), and optional `description`.

```bash
brigade skills audit latest --target . --json
```

`brigade skills audit` reads `selected_skill_ids` from a run's `plan.json`,
loads each skill's obligations, and compares them to captured verify, review,
and handoff ingest receipts. Missing required evidence is reported as advisory
findings (exit 0). Verify receipt commands may stamp `obligation_id` for
precise check matching; when no stamp exists, a completed verify receipt
satisfies a `check` obligation at the kind level. This audit never blocks
verify or promotion (see #499 / skill scorecards).
