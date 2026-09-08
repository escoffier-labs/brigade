# Agent-change evidence index

The agent-change evidence index collects the references that justify an
orchestrated agent change in one signed, in-toto Statement. It is produced by
`brigade receipts export agent-change` and audited by
`brigade receipts verify-agent-change`.

## Commands

- `brigade receipts agent-change-policy init --target <dir> [--force]`
  Write a default policy file at
  `<dir>/.brigade/attestation/agent-change-policy.json`. Refuses to overwrite
  unless `--force` is given.

- `brigade receipts export agent-change --target <dir> --run-id <run-id>
  [--key <path>] [--profile <sshsig|cosign>] [--policy <file>] [--out <path>] [--force] [--json]`
  Build and sign the index statement for `<run-id>`. The default output is
  `<run-dir>/agent-change.json`. Exit is `0` when the index is complete under
  the policy and nonzero when it is incomplete.

- `brigade receipts verify-agent-change <envelope-or-run-dir>
  --target <dir> [--policy <file>] [--json]`
  Audit the index. When `<envelope-or-run-dir>` is a directory, the verifier
  reads `agent-change.json` inside it; when it is a file, it locates the run
  directory from the statement's run id under `<target>/.brigade/runs/`.
  Exit is `0` only for `COMPLETE-OK`.

## Policy file

Policy path: `<target>/.brigade/attestation/agent-change-policy.json`.
Schema: `brigade.agent_change_policy.v1`, `schema_version: 1`.

Fields:

- `project_scope`: opaque string chosen by the operator (a UUID or salted hash).
  Repository URLs and paths are never used as identity.
- `required_references`: list of reference kinds that must be present. Allowed
  kinds: `agent-request`, `test-result`, `human-approval`. `test-result` may
  carry `min_count` (default `1`).
- `allowed_profiles`: list of signer profiles accepted for references.
  Default `["brigade.sshsig-dsse.v1"]`.
- `policy_version`: integer.

The emitter refuses to run without a policy file.

## Statement fields

Envelope: DSSE with the `brigade.sshsig-dsse.v1` profile, namespace
`brigade-attestation`, written to `<run-dir>/agent-change.json`.

Predicate type: `https://brigade.dev/attestation/agent-change/v1`,
`schemaVersion: 1`.

Subject: exactly one `git:tree` entry carrying the run's terminal
`tree_fingerprint`.

Predicate fields:

- `run`: `{id, journalChainHead, orchestratorSeat, workerSeats}`.
- `participants`: seats named by `run.json` `orchestrator`, `worker`, and any
  `active_seats`, with harness and model declared from the run directory
  `roster.json` snapshot (`source: "roster_snapshot"`). `providerObserved` is
  always `{"status": "unknown"}` in this slice.
- `baseline`: `{"gitCommit": <sha>}` or `{"status": "unknown"}`.
- `patch`: `{"sha256": <sha256>}` or `{"status": "unknown"}`.
- `request`: `{"nonce": <32 hex>, "taskSha256": <64 hex>}` or `{"status": "absent"}`.
  The nonce is resolved from the run journal's `request.signed` event.
- `emittedAt`: UTC Z timestamp, second precision.
- `nonce`: 32 hex characters from `secrets.token_hex(16)`.
- `policy`: `{"name": "brigade.agent_change_policy.v1", "digest": {"sha256": ...}}`.
- `project`: `{"scope": <policy project_scope>}`.
- `signerIndependence`: always `"shared-workspace-key"`.
- `references`: sorted list of reference entries. Each reference is
  `{kind, predicateType, predicateVersion, profile, subjectTree,
  payloadSha256, envelopeSha256, signerKeyids, verified, locator}` with optional
  `subjectBaseline` for `agent-request`, `rederived` and `result` for
  `test-result`, `journalBound` for `human-approval`, and `reason` or `locators`
  as needed.
- `missing`: list of `{"kind", "reason"}` entries for required references that
  are absent at emit time. Only kinds listed in the policy's `required_references`
  are included; a policy that does not require `agent-request` will not list it.
- `otherTreeReceipts`: test-result receipts whose `producer_run_id` matches
  but whose `tree_fingerprint` differs from the final tree.
- `complete`: boolean. The emitter sets this from the policy, but verifiers
  recompute it from their own policy and ignore the statement value.

## Cosign profile

`--profile cosign` writes an unwrapped Sigstore bundle to
`<run-dir>/agent-change.sigstore.json`. `verify-agent-change` accepts SSHSIG
envelopes only. External consumers use `cosign verify-blob-attestation` with
the agent-change predicate type and the statement's `gitTree` subject claim.

## Reference kinds

- `agent-request`: the signed request recorded in the run's request event.
  Binds the baseline commit as `subjectBaseline` and records the request nonce
  and `taskSha256`.
- `test-result`: a verify-run attestation whose `producer_run_id` matches the
  run and whose final tree matches the index subject tree. Records `result`
  (`PASSED` or `FAILED`) and `rederived`.
- `human-approval`: every approval file in `<run-dir>/approvals/<nonce>.json`
  whose name matches `^[0-9a-f]{32}\.json$`. The latest journal-bound approval
  is marked `journalBound: true`; the others are `journalBound: false`.

## Verifier axes and statuses

The verifier reports observations on separate axes, not a single boolean.

Index envelope:

- `syntax`: `wellformed` or `malformed`.
- `signature`: `valid`, `invalid`, or `unverifiable`. A signature from an
  untrusted key is still reported as `valid` because the signature verified
  cryptographically over the PAE bytes; the `trust` axis reports `untrusted`
  and the overall status is `INVALID`.
- `trust`: `trusted`, `untrusted`, or `unknown` (when `allowed_signers` is
  absent or unreadable).
- `freshness`: `revocation-checked` or `revocation-absent`, plus
  `timestamp-absent` (no trusted timestamp exists in this profile).
- `binding`: `bound`, `conflicted`, or `unavailable`. Compares the statement
  subject tree with the local `run.json` terminal tree.
- `policy`: `match`, `mismatch`, or `unavailable`. Compares the policy digest
  recorded in the statement with the digest of the policy the verifier is
  evaluating.

Each reference:

- `availability`: `present`, `missing`, or `partial`.
- `syntax`: `wellformed` or `malformed`.
- `cryptographic`: `valid`, `invalid`, `unverifiable`, or `unchecked`.
- `trust` and `freshness`: same values as the index envelope.
- `binding`: `bound` or `conflicted`. Requires both that the local artifact's
  payload and envelope digests equal the reference and that the referenced
  statement's own subject tree equals the index subject tree. A valid Test
  Result for a different tree is `conflicted`, never `bound`.
- `rederivation`: `reproduced`, `failed`, or `not-applicable`. Test Results
  only.
- `policy_outcome`: `pass`, `fail`, `unevaluated`, or `not-applicable`. A
  `PASSED` Test Result under an allowed profile with a trusted signer passes;
  anything else is `fail` or `unevaluated`.

Project scope:

- `project.status`: `match` or `mismatch`. Compares the statement
  `project.scope` with the evaluating policy's scope, separate from the policy
  digest comparison.

Required-set evaluation:

- For each required kind the verifier reports `status: satisfied` or
  `status: missing` with `count`. Completeness is computed from the policy,
  not read from the statement's `complete` field.

Overall status:

- `COMPLETE-OK`: every required reference is present, cryptographically valid,
  trusted, bound, re-derived where applicable, and passes policy; and the index
  envelope is valid, trusted, bound, and policy-matched.
- `INCOMPLETE`: a required reference is missing or does not satisfy the policy.
- `INVALID`: signature or policy check failed.
- `UNVERIFIABLE`: the envelope is malformed or the policy is unavailable.

## "Not an approval" and shared-key limitations

The verifier output is an audit observation, not an approval or release
decision. The output text and JSON both carry a disclaimer to that effect.

The index is signed with the same workspace attestation key that signs Test
Result attestations. `signerIndependence` is therefore
`"shared-workspace-key"`, meaning the index is a producer self-attestation, not
an independent assessment. The verifier does not treat shared-key signing as a
trust defect; it reports `trust: trusted` when the signature verifies against
`allowed_signers` and reports the shared-key limitation in the statement.
