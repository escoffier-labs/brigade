# Memory Handoff

## Type

security

## Title

Attestation JSON parsing now has one bounded strict boundary

## Summary

Attestation, cosign, and both approval formats now share strict JSON, base64,
and no-follow regular-file input handling. Mapping consumers receive plain JSON
snapshots after bounded validation, while signed payload bytes remain unchanged
for DSSE verification. This parser-first slice leaves broader filesystem and
receipt boundaries for later work.

## Durable facts

- `src/brigade/attestation_input.py` owns the 8 MiB document, 4 MiB payload,
  64 KiB signature, depth-64, and 100,000-node limits.
- DSSE accepts standard and URL-safe base64 with complete RFC 4648 padding and
  retains bounded raw SSH armor.
- Mapping and list inputs are copied into plain JSON snapshots after bounded
  depth, node, Unicode, scalar-size, and aggregate-byte validation.
- Scalar subclasses and mapping-key subclasses are normalized to exact built-in
  JSON types after string bounds are checked. Duplicate normalized keys and
  ordinary mapping iteration failures return bounded input errors.
- Cosign bundle media-type and payload-type diagnostics name the fields and
  expected profile constants without echoing supplied values.
- `predicate.url` and fallback `predicate.run.id` both discard dot-only run IDs.
- Ancestor containment, aggregate directory budgets, subprocess budgets, raw
  artifact digest versioning, and stored receipt digest recomputation remain
  intentionally deferred.

## Evidence

- files changed: `src/brigade/attestation_input.py`, `src/brigade/attestation.py`,
  `src/brigade/cosign_attestation.py`, attestation consumers, focused tests,
  documentation, and changelog
- previous checkpoint verification: 192 passed, 1 skipped because cosign is absent.
  Receipt `20260905-162105-work-verify-e1b4e5`.
  Evidence `4a0e6d91b88f1d1723f80dbc`.
- commands run: `brigade work verify run --target . --argv-json '["./scripts/verify-focused","tests/test_attestation_input.py","tests/test_attestation_input_integration.py","tests/test_attestation.py","tests/test_cosign_attestation.py","tests/test_agent_request.py","tests/test_approval.py"]' --capture brigade-work`
- correction worker: `bb7d9dbb.20260905-160945-4b803986`, completed after the
  earlier implementation run timed out. Primary source reassessment and review
  reconciliation remain in ignored `docs/plans/2026-09-05-governance-reassessment.md`
  in the campaign orchestration worktree.

## Recommended memory action

no-card

## Target document

.learnings/LEARNINGS.md

## Suggested document content

### Attestation input boundary

Use `brigade.attestation_input` for every new attestation evidence consumer so
JSON, DSSE base64, final-file reads, and mapping snapshots share the bounded
strict contract.
