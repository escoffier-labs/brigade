# Handoff inbox lint

Memory Handoffs are how durable knowledge leaves a session. `brigade handoff
lint` is the gate that keeps a handoff parseable by the ingester instead of
being a free-form note nobody can route.

## Sub-features

- `handoff lint` with no paths validates every pending file in the inbox
  (`.claude/memory-handoffs/` for a claude-owned target), skipping
  `TEMPLATE.md` and `processed/`.
- Explicit paths lint just those files, wherever they live.
- `--content-guard` adds a leak scan (secrets, PII) plus injection heuristics
  for instruction-shaped payloads; `--guard-policy` picks the policy.
- `--strict` promotes standalone-readability warnings to failures.
- `--json` returns `valid`, `count`, `content_guard`,
  `injection_flagged_count`, `readability_flagged_count`, and a `results` array
  with per-file `valid`, `errors`, `warnings`, `hints`, `readability`,
  `injection_heuristics`, `action`, and `salvageable`.
- Neighbors worth knowing: `handoff doctor` (inbox vs source config),
  `handoff draft` (writes a linted handoff), `handoff migrate` (converts
  homegrown notes), `handoff issues` / `import-issues`.

## How to get to it (user POV)

An agent finishing a session writes a handoff from
`.claude/memory-handoffs/TEMPLATE.md`, then checks it before leaving:

```bash
brigade handoff lint --target . --json
brigade handoff lint --target . --content-guard --strict
```

`"valid": true` means the ingester can route it. Anything else is a handoff
that will be dropped or need a human.

## Driving it with control-brigade

```bash
C=registry/skills/verify-brigade/control-brigade.py

# empty inbox on a fresh target
$C handoff-lint --target "$TARGET"

# after dropping a handoff into $TARGET/.claude/memory-handoffs/
$C handoff-lint --target "$TARGET" --content-guard

# one explicit file
$C handoff-lint --target "$TARGET" "$TARGET/.claude/memory-handoffs/2026-09-01-0000-sample.md"
```

Observed end states:

- Fresh target, empty inbox -> exit 0, `{"valid": true, "count": 0,
  "results": []}`. `valid: true` on zero files means "nothing is broken", not
  "something was checked" - assert on `count` when you meant to lint a file.
- One well-formed handoff -> exit 0, `count: 1`, and a result with
  `"valid": true`, `"errors": []`, `"action": "no-card"`.
- One handoff missing sections -> helper exit 3, `"valid": false`, and errors
  naming each miss verbatim:
  `missing required section: Type`, `missing required section: Summary`,
  `missing required section: Recommended memory action`.

The error strings are the stable handle. Assert on them rather than on the
count of errors, which grows as checks are added.

## Gotchas

- `valid: true` with `count: 0` is the empty-inbox answer and is easy to
  mistake for a pass. Always read `count`.
- `##` is the section delimiter. A handoff body that uses `##` for its own
  subheadings creates phantom sections; the template says use `###` or deeper.
- The template forces exactly one branch: a card action (`create-card` /
  `update-card`) must omit the document sections, and `no-card` must omit the
  card sections. Leaving both in is a lint failure, not a warning.
- `--content-guard` is a different failure class from schema lint. A handoff
  can be structurally valid and still be flagged for a leak, so run both before
  claiming a handoff is clean.
- `--strict` changes the exit code, not the file. Readability warnings that
  pass by default will fail under `--strict`.
- Never lint the operator's real inbox to test a change. Drop the fixture into
  the temp target, where cleanup takes it away.
