# Offline acknowledgement acceptance

`brigade.evidence_ack_acceptance.accept_acknowledgement` composes the existing
strict package snapshot, canonical request and envelope, and one-key SSHSIG
observation. This implements the destination acknowledgement residual assigned
to [#1620](https://github.com/escoffier-labs/brigade/issues/1620) by
[#1407](https://github.com/escoffier-labs/brigade/issues/1407#issuecomment-6027257498).
The parent issue retains its separate retention and prune work.

The function performs no I/O. It receives immutable observations and explicit
organizational inputs. It neither signs nor uploads evidence, discovers keys,
reads a clock, writes replay history, or changes retention.

## Inputs and provenance

Callers obtain `PackageSnapshot` through `snapshot_package`, `AckRequest` through
`build_request` or `parse_request`, `AckEnvelope` through `parse_envelope`, and
`AckCryptoObservation` through `observe_ack_sshsig`. Pass a
`PackageSnapshotError` to preserve a failed snapshot observation. An unavailable
snapshot capability remains `unavailable`. Unsafe, malformed, mismatched, or
changed packages are rejected.

The acceptance boundary re-parses the captured canonical wire bytes using the
existing parsers and compares every derived field. It re-hashes the exact
manifest bytes, applies the existing strict manifest validator, and compares the
complete entry metadata and source identity against the snapshot. It then binds
the request's manifest digest, verify run and receipt digest to that snapshot,
and binds the response request ID and crypto observation to the exact envelope.
Each optional entry present in the captured manifest participates in that
binding. Omissions, injections and metadata replacements fail association.

These consistency checks do not reproduce file capture or signature verification.
Dataclasses can be constructed manually. The caller must retain observation
provenance and must not replace adapter outputs with unvalidated claims. The
snapshot describes captured files, not the later state of a mutable directory.

The caller independently supplies all of the following:

- Expected `destination_id` and local `audience`. The existing request wire
  format binds a destination, but has no audience field. The supplied audience
  selects local policy and replay scope. It is never described as a signed
  audience claim.
- `DestinationPolicy`: destination, audience, revision, policy validity window
  and up to 128 explicit `AuthorizedKey` observations. Every key specifies its
  verified fingerprint, validity window, revocation state, revocation evidence
  digest and evidence validity window. The exact policy input has a derived
  canonical JSON SHA256 digest recorded in the result. That digest identifies
  the evaluated policy, without proving organizational approval.
- `evaluated_at`: an explicitly supplied UTC timestamp. No destination clock
  claim can supply this value. The evaluator's clock source and its reliability
  are the caller's responsibility.
- `ReplayObservation`: the exact `AckAssociation`, state, evidence digest,
  check time and end of the clearance window. A clear observation must cover
  the current evaluation and have been checked after request creation. The
  upstream replay observer must check consumed requests, envelope IDs and
  destination nonces, including conflicting responses. This boundary does not
  implement that observer or reserve the nonce. Callers must atomically consume
  or reserve a request in their own replay authority before acting on acceptance.

Policy, validity, revocation, replay and optional independent authority evidence
must be admitted by the adopting organization through its own trusted process.
Evidence digests alone do not prove the evidence exists, is authentic, or is
complete. No policy, key, evidence source or destination is inferred from an
envelope, its key hint, local key inventory, or a successful signature check.

## Results and authority

`AckAcceptance` keeps signature validity, key hint comparison, verified
fingerprint, destination authorization, asserted receipt result, exact
association, policy identity, replay evidence and custody/time authority in
separate fields. It retains the destination reference exactly as signed. The
wire reference is opaque, so the function does not invent an object/version
split from its spelling.

The principal state is one of `accepted`, `rejected`, `expired`, `replayed`,
`ambiguous`, `unavailable`, or `indeterminate`. `reasons` records the evaluated
boundaries that prevent acceptance. Conclusive policy refusals remain the
principal state even when the request has expired. Subsequent independent capabilities
are not evaluated after that refusal. The supplied replay and authority
observations remain available in the result, even when another boundary refuses
acceptance. A valid signature can coexist with rejected authorization. A
verified, authorized `rejected` response never establishes custody.

Receipt assertion, destination reference and acknowledged-time fields are
projected from parsed canonical envelope bytes. If those bytes cannot be parsed,
the fields are `None` and the result is rejected.

Acceptance requires an exact association, valid signature, explicitly authorized
verified fingerprint, current policy/key/revocation evidence, a request still
within its validity window, a plausible acknowledged time and fresh replay
clearance. Validity windows are half open: their end instant is excluded.
Acknowledgement time must be within the request window and no later than the
evaluation time. These comparisons express consistency under the supplied
clock. They do not prove the destination's clock operated correctly.

With no independent authority evidence, accepted `received` responses carry
`authenticated-assertion` for custody and time. Optional `AuthorityObservation`
inputs can report `independent-operating-evidence` or `independent-time-evidence`
only when they match the entire association, signed destination reference and
acknowledged timestamp and carry an admitted supporting evidence digest. A
custody observation also supplies an explicit object version. Duplicate kinds,
unbound evidence and unavailable or indeterminate observations prevent
acceptance and keep authority unestablished.

Those labels report independently supplied observations. This module does not
validate external storage controls or time sources. It cannot establish legal
hold, retention enforcement, restoration, organizational identity, or control
effectiveness. Those observations require their own evidence and scope.

## Verification

Composition fixtures cover exact bindings, complete membership, destination and
audience selection, fingerprint policy, revocation, validity windows, replay,
capability failures, rejected receipt assertions and independent authority.
The existing package, protocol and crypto test files are read-only dependencies.

```bash
brigade work verify run --target . --argv-json '["./scripts/verify-focused","tests/test_evidence_ack_acceptance.py","tests/test_evidence_package_snapshot.py","tests/test_evidence_ack_protocol.py","tests/test_evidence_ack_crypto.py"]' --capture brigade-work
```

The PR gate uses `./scripts/verify` through the same wrapper with its normal
shared lock and a 3600-second timeout. Status 75 is a lock refusal, never a pass.
