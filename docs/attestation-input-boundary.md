# Attestation input boundary

Attestation and approval readers, plus cosign bundle structural validation,
parse untrusted JSON through `brigade.attestation_input`. The shared boundary accepts documents of at most
8 MiB, signed payloads of at most 4 MiB, and decoded signatures of at most
64 KiB. It rejects duplicate object names, non-finite numbers, invalid UTF-8
or Unicode scalars, nesting beyond 64 containers, and documents with more
than 100,000 value nodes.

Mapping and list inputs receive the same limits as serialized inputs. The
boundary builds a plain JSON snapshot before consumers inspect a mapping, so
later mapping methods cannot change the validated values. Root containers count
as depth 1. String and integer sizes are checked before serialization, and
decoder or serializer value errors become bounded input errors.

Path inputs are opened with the existing no-follow descriptor helper, checked
as regular files, and read only through a fixed byte budget. When that
primitive is unavailable, verification returns its existing bounded error
result rather than falling back to an ordinary path open.

DSSE decoding accepts the standard and URL-safe base64 alphabets with complete
RFC 4648 padding, but rejects whitespace, mixed syntax, missing padding, and
other ignored characters.
The existing raw armored SSH signature form remains accepted under the same
signature-size budget. Parsed statements retain their original signed payload
bytes, and signature-only verification does not require canonical whitespace.

Attestation run IDs accept only the existing lexical form. Dot-only IDs are
discarded for both the predicate URL form and the fallback `predicate.run.id`
form. An absent usable run ID prevents local receipt re-derivation.
Signature-only verification keeps its existing status behavior.

This boundary does not add ancestor containment checks, aggregate directory
budgets, subprocess output or time budgets, new receipt-digest fields, or
stored receipt-digest recomputation.
