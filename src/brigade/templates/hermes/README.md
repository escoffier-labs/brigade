# Hermes Adapter

`brigade` supports Hermes as a local Memory Handoff writer, validated against a real Hermes install (Hermes v0.17). Reviewed skills also install into your Hermes store (`$HERMES_HOME/skills`), where Hermes discovers them.

## What this gives you

- `workspace.harness.json` - which bootstrap files Hermes should load
- `memory-handoff.harness.json` - the handoff inbox and routing targets
- `model-lanes.harness.json` - suggested model alias names
- `.hermes/memory-handoffs/TEMPLATE.md` - the local handoff writer template

## Smoke test

```bash
brigade handoff sources init --target .
brigade handoff draft --target . --inbox hermes \
  --title "Hermes smoke handoff" \
  --summary "Hermes can write a local Brigade Memory Handoff." \
  --content "### Hermes smoke handoff

Hermes uses the shared Brigade handoff format."
brigade operator verify-harness --harness hermes --target .
brigade handoff list --target .
```

When a Hermes draft has been reviewed and ingested by the memory owner, record that local outcome with:

```bash
brigade handoff receipt record --target . --owner hermes --run-id hermes-manual-001 <draft-id>
brigade handoff archive --target . <draft-id> --reason "reviewed"
```

## What it does not do yet

- Validate against every live Hermes config schema
- Generate Hermes-specific plugin entries
- Ingest handoffs into canonical memory automatically

## Contributing

If your Hermes config layout differs, open an issue at <https://github.com/escoffier-labs/brigade/issues> with:

- the file Hermes loads as its primary bootstrap file
- the path where Hermes expects memory handoffs (if any)
- the command that ingests handoffs into canonical memory

That helps keep the adapter working across Hermes versions.
