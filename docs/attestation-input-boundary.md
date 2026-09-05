# Attestation input boundary

Attestation, cosign, and approval verification parse untrusted JSON through
`brigade.attestation_input`. The shared boundary accepts documents of at most
8 MiB, signed payloads of at most 4 MiB, and decoded signatures of at most
64 KiB. It rejects duplicate object names, non-finite numbers, invalid UTF-8
or Unicode scalars, nesting beyond 64 containers, and documents with more
than 100,000 value nodes.

Path inputs are opened with the existing no-follow descriptor helper, checked
as regular files, and read only through a fixed byte budget. When that
primitive is unavailable, verification returns its existing bounded error
result rather than falling back to an ordinary path open.

DSSE decoding accepts the standard and URL-safe base64 alphabets with valid
padding, but rejects whitespace and other ignored characters.
The existing raw armored SSH signature form remains accepted under the same
signature-size budget. Parsed statements retain their original signed payload
bytes; signature-only verification does not require canonical whitespace.

This boundary does not add ancestor containment checks, aggregate directory
budgets, subprocess output or time budgets, new receipt-digest fields, or
stored receipt-digest recomputation.
