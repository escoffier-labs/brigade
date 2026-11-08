- Verifier attachments are now bound to the proposal they support. A verifier's record must
  be a `memory-verify-manifest` for the attested card, or attach refuses it with
  `memory_manifest_card_mismatch`. The record starts in `pending_review`, never holds
  automatic apply and never counts as proof. `propose` binds it only to the attaching
  principal's proposal for the exact scope, card and base revision the manifest checked, and
  otherwise refuses the whole proposal with `422 memory_manifest_binding_invalid`. Accepting the
  proposal makes the record `active`. Rejection, staleness, compaction and the 24 hour unbound
  sweep make it `inert`, which frees its quota slot. Memory schema moves to 12.
- Verifier records that are pending or inert count against a retained-storage budget of
  16 MiB and 1,024 records per principal and 64 MiB and 4,096 records per scope, refused with
  `429 memory_evidence_retained_principal_quota` or `memory_evidence_retained_scope_quota`.
  The idle sweep purges a record 35 days after it went inert through the `evidence.purge`
  tombstone, keeping its hash and logging the purge.
- `proposal_show` returns a `verifier_manifests` list of bound manifest hashes to callers with
  `review` on every target and every bound record's scope, and omits the key for everyone
  else. Memory `status` advertises a `verifier_manifests` capability.
