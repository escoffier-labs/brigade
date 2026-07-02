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
next session. Available skills: `check`, `refire`, `bug-hunt`, `reduce`,
`taste`.

If you run Brigade, project the skill into other harnesses afterward:

```bash
brigade skills import .claude/skills/check
brigade skills install check --target all
```

## What gets installed

One markdown file per skill, byte-identical to its source in
[escoffier-labs/skillet](https://github.com/escoffier-labs/skillet). No
dependencies, no code execution, no config changes. Read the file before you
adopt it, like anything else that instructs an agent.
