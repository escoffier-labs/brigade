# Skills, Friction, and Receipt Projections

## Completed

- Added strict Agent Skills authoring validation for exact `SKILL.md` casing,
  name, description, license, compatibility, string metadata, and declared
  `allowed-tools` requirements.
- Added lenient import diagnostics while retaining legacy skill packages and
  unknown fields.
- Kept `allowed-tools` informational. It never grants execution permission.
- Made friction v2 consume verification, run, evaluation, and explicit
  MiseLedger receipts before regex evidence.
- Added per-source-family candidate quotas and recurrence identities that omit
  source paths and line positions.
- Added privacy-safe OTel GenAI and OpenInference JSONL projections. Prompts,
  model output, tool arguments, command catalogs, and retrieved content are
  omitted.
- Reused the existing MiseLedger adapter, install receipts, history,
  fingerprint scoring, promotion signals, and rollback implementation.

## Verification

- Focused skills, friction, receipt, and projection tests passed.
- Full `./scripts/verify` passed through Brigade in run
  `20260712-220804-work-verify-12fab0`.
