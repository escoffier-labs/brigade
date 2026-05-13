# Hermes Adapter (Experimental)

`solo-mise` supports Hermes through the same harness contract as OpenClaw. This adapter is **experimental** until it has been validated against a real Hermes install.

## What this gives you

- `workspace.harness.json` - which bootstrap files Hermes should load
- `memory-handoff.harness.json` - the handoff inbox and routing targets
- `model-lanes.harness.json` - suggested model alias names

## What it does not do yet

- Validate against the live Hermes config schema
- Generate Hermes-specific plugin entries
- Replace `solo-mise hermes doctor` with anything beyond file existence checks

## Contributing

If you run Hermes and have working config, open an issue at <https://github.com/solomonneas/solo-mise/issues> with:

- the file Hermes loads as its primary bootstrap file
- the path where Hermes expects memory handoffs (if any)
- the command that ingests handoffs into canonical memory

That lets the adapter be promoted from experimental to tested.
