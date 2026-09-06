# Attestation receipt digests

Test Result attestation export derives the receipt subject from a plain JSON
snapshot. The receipt digest is the SHA-256 of the compact, sorted-key JSON
serialization with only the top-level `digests` member omitted. A stored
`path` remains receipt content. Moving a receipt without rewriting it keeps
the same digest. Rewriting `path` changes the digest.

When `digests.receipt_sha256` is present, export accepts it only when
`digests.algorithm` is `sha256`, the value is lowercase 64-character hex, and
it equals the digest rederived from that snapshot. Invalid stored evidence is
refused. A receipt with no stored digest remains exportable and gets the
rederived writer digest in the Test Result statement. This keeps legacy
digestless exports compatible. Approval evidence still requires a stored
digest and is not changed by this slice.

The export command strictly parses receipts for exact IDs, prefixes, and
`latest`. Exact IDs and a single unambiguous prefix match resolve; a prefix
that matches more than one receipt run ID raises an ambiguity error. It reads
no more than 4,096 entries from the verify-runs directory, no more than 32 MiB
of receipt data in a scan, and no more than 8 MiB per receipt. The scan budget
counts bytes actually read from each receipt file, not the stat size. The
selected receipt directory is output-placement metadata, not a synthetic `path`
field in the receipt JSON. A malformed direct receipt is refused without falling
back to a different prefix match.

Statement construction always re-derives the plain receipt snapshot and its
digest from the mapping it is given at call time, even when the caller passes
a `ReceiptSnapshot`. It does not rely on a cached digest for signed evidence, so
mutating the receipt mapping between snapshot creation and statement
construction is detected instead of silently emitted as a stale subject digest.

The loader uses a target-relative pathname and a no-follow final receipt-file
read. It does not retain no-follow descriptors for every ancestor directory,
so it does not close concurrent ancestor replacement races.

## Approval collectors

Both approval v1 and v2 require a stored receipt digest on every matching verify
receipt. They validate the digest before applying any tree fingerprint filter,
so a mismatched or missing digest is reported as a receipt error rather than
silently skipped as a non-matching tree. After validation, both collectors use
the same `ReceiptSnapshot` for identity checks and pass the snapshot receipt to
Test Result re-derivation, so `receipt.json` is read exactly once and
re-derivation cannot disagree with the collector's digest.
