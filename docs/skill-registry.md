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
npx shadcn@latest add @brigade/check
```

This writes `.claude/skills/check/SKILL.md`. Claude Code discovers it on the
next session. Available skills: `check`, `refire`, `bug-hunt`, `latent-premises`, `retry-safety`, `reduce`,
`taste`.

If you run Brigade, project the skill into other harnesses afterward:

```bash
brigade skills import .claude/skills/check
brigade skills install check --target all
```

Brigade's own bundled skills use a separate canonical source identity. A named
install such as `brigade skills install brigade-work --target cursor` resolves
the current template shipped by the installed Brigade package. Use
`registry:brigade-work` or an explicit path only when you intend to select a
same-named local registry copy. `brigade skills fleet status` reports stale
harness copies and the exact update command for each supported copy. If current
metadata no longer supports an installed harness, it reports an uninstall
command instead of recommending another install.

Use `brigade skills sync --workspace . --target all --trust workspace` to plan
the complete registry-to-harness matrix without changing files. Add `--write`
to install only missing or changed pairs at or above the trust floor. Sync uses
explicit registry identities, so a same-named bundled skill cannot replace the
reviewed registry source. Completed targets keep their receipts and rollback
snapshots if another target fails.

## What gets installed

One markdown file per skill, byte-identical to its source in
[escoffier-labs/skillet](https://github.com/escoffier-labs/skillet). No
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
