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
`latest`. It reads no more than 4,096 entries from the verify-runs directory,
no more than 32 MiB of receipt data in a scan, and no more than 8 MiB per
receipt. The selected receipt directory is output-placement metadata, not a
synthetic `path` field in the receipt JSON. A malformed direct receipt is
refused without falling back to a different prefix match.

The loader uses a target-relative pathname and a no-follow final receipt-file
read. It does not retain no-follow descriptors for every ancestor directory,
so it does not close concurrent ancestor replacement races. Approval collector
integration, including its receipt-selection and producer-binding rules,
remains deferred.
