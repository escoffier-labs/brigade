# Registry payloads

Skill files served by the @brigade shadcn registry (see `registry.json` at the
repo root and `docs/skill-registry.md`).

These are vendored, byte-identical copies. The source of truth is
[escoffier-labs/skillet](https://github.com/escoffier-labs/skillet) at
`skillet/skills/<name>/SKILL.md` (MIT). Do not edit files here. Update skillet
and re-copy, then re-run `npx shadcn@latest build`.

One exception: `skills/verify-brigade/` is written and maintained here, not
vendored from skillet, and it is deliberately absent from `registry.json`. It
drives this checkout's own `brigade` CLI, so it is a development skill for
Brigade itself rather than a payload to serve to other projects. Edit it in
place; `tests/test_verify_brigade_skill.py` is its gate.

The registry mechanism follows [shadcn registries](https://ui.shadcn.com/docs/registry).
[remocn](https://github.com/kapishdima/remocn) by kapishdima proved the same
mechanism carries non-component payloads (Remotion video components). This
registry carries agent skill files.
