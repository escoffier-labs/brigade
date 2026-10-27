# Agent mesh implementation plan

Design: [phase-agent-mesh.md](phase-agent-mesh.md). Issues: #2005 (sealed
mail), #2006 (teammate invites), #2007 (no-hub mode), #2008 (Hub Lite). Parent:
#1121.

Each slice below is one pull request that a worker lane can pick up on its own
once the slices it depends on have merged. Slices list the files they touch,
the tests they add and what must be true before they merge. Slice numbers
from revision 1 are kept so review comments still resolve. New slices carry a
letter suffix. Revisions 3 and 4 add no slices. Revision 3 changed the
contents of slices 1 to 3, 5 to 12c, 16 and 17, and the dependency headers.
Revision 4 changes slices 2, 2b, 5, 6, 7, 7b, 10, 11, 13 and 16, and adds
`7b -> 11` to the dependency order. Revision 5 (2026-10-27) proposes contract
closure in D3 through D8, adds `2 -> 2b`, and names acceptance tests below.
It does not authorize migration, policy activation or deployment. The separate
90-day retry-receipt policy and staged policy activation require parent, Astra
and Daybreak disposition before implementation. Every slice runs the focused
gate through
Brigade with its own test selectors:

```bash
brigade work verify run --target . --argv-json '["./scripts/verify-focused","<selectors>"]' --capture brigade-work
```

The full gate runs once per pull request, as AGENTS.md requires. Planned
commands are written without the `brigade` prefix because they do not exist
yet.

## Integration baseline

Slices build on `main` plus #1939 (opaque paging cursors and snapshots), #2009
(`count_only`), #2010 (`client_id` and tool annotations) and #1951 (operator
questions). A slice that touches a surface one of them changes waits for that
merge and says so in its header. Rebase onto the merged form, not the open
branch.

| Pull request | Slices that wait for it | Why |
|---|---|---|
| #1939 | 3, 11, 16 | `who` paging snapshots and opaque cursors |
| #2009 | 4, 5, 11, 12c, 16, 17 | the `count` action and `count_only` |
| #2010 | 4, 6, 11, 16, 17 | sender-bound `client_id`, the 409 replay contract, tool annotations |
| #1951 | 7b, 11, 16, 17 | `fleet_ask_user`, `fleet_questions` and its trigger |

## Gates

- **G1, dependency: approved** on #2005 (2026-10-22). The optional extra is
  `mesh = ["cryptography>=50.0.2,<52"]`. The original floor was 48. Current
  source and CI use 50.0.2 after
  the dependency update. Keep that floor pending source-backed platform review,
  and hold the stale Intel 48 install route. This revision changes no dependency.
- **G2, native engine: approved** on #2007 (2026-10-22), staged: slice 15 is a
  bounded spike, slice 15b writes the wire protocol, and slices 18a to 18e
  start only after slice 15 records a go and slice 15b is merged.
- **G3, open questions: decided.** Design "Decisions on the open questions"
  records all fourteen.
- **G4, `wrangler` for CI: approved** by the conductor on PR #2013
  (2026-10-22, decision 6, under the operator's delegation of best-practice
  calls). `wrangler` is a pinned development dependency of
  `workers/hub-lite/` only, with a committed lockfile, used in CI and locally
  for the workerd runs in slices 12a to 12c. It is not a runtime dependency of
  `brigade-cli`, and the Python package stays dependency-free.

## Constraints for every slice

- `src/brigade/fleet_hub.py` is at 1965 lines on `main` and the module size
  ratchet caps modules at 2000 lines. New hub logic goes in new modules. Only
  small hook calls may be added to `fleet_hub.py`. `fleet_hub_messages.py`
  (1135 lines) and `fleet_client_messages.py` (1183 lines) also take new logic
  in new modules.
- The hub never imports `cryptography`. A test in slice 3 enforces this.
- Every slice that changes the hub schema (3, 4, 7b, 8 and 9) bumps
  `SCHEMA_VERSION` past #1939's 41 or registers additive columns, so the
  startup gate accepts an upgraded database.
- Every module that needs the extra imports it lazily and raises one
  `MeshExtraMissing` error, the pattern `src/brigade/dot/access_auth.py` uses.
- No fixture, test or doc contains real hostnames, addresses, tokens or
  personal names. Use `example.invalid` hosts and obviously fake keys.
- Existing plain-text messaging tests pass unchanged with and without the
  extra installed.
- `src/brigade/message_envelope.py` already exists for run messages (#1010).
  New modules use the `fleet_mesh_` prefix.
- Docs refer to planned commands without the literal `brigade` prefix until
  the slice that adds the command merges, so the documented-command inventory
  check stays green.

## Dependency order

```text
1 -> 2
#1939 + 3 -> 4 (4 also needs #2009 and #2010)
1 + 2 + 3 -> 2b
1 + 2 + 2b + 3 + 4 + #2009 -> 5 -> 6 (6 also needs #2010) -> 7
6 + #1951 -> 7b
8;  3 + 8 -> 9
2 + 2b + 5 + 6 + 9 -> 10
4 + 7b + 9 + #1939 + #1951 + #2009 + #2010 -> 11 -> 12a -> 12b -> 12c -> 13 -> 14 (14 also needs 10)
15 -> 15b
#1939 + #1951 + #2009 + #2010 -> 16
1 + 2 + 5 + 6 + 15b + 16 -> 17
15 + 17 -> 18a -> 18b -> 18c -> 18d, 18e
18b -> 19
```

## How the review findings map to slices

| Finding | Design | Slices |
|---|---|---|
| ask_user bypasses the boundary | D7 | 7b, docs note in 7 |
| Hub identity can reset pins | D4 | 3, 2b |
| Reply check trusts hub columns | D5, D6 | 4, 5 |
| Pin acceptance boundary | D3 | 2 |
| Invite fragment readable by the hub's page | D9 | 9, 10 |
| Pending invitation trust | D3, D9 | 2, 10 |
| Retry recovery API | D5 | 1, 4, 6 |
| Rotation and retention semantics | D3, D8 | 2, 4, 5 |
| Idempotent redemption | D9 | 9, 10 |
| Dependency graph and baseline | this plan | all |
| Hub Lite storage math | D8, D12 | 4, 12c, 14 |
| Reason Network rationale | D10 | none (docs) |

Round 3:

| Finding | Design | Slices |
|---|---|---|
| A. Own-fleet `observed` lacks authenticated membership | D3, D13, threat model | 2, 3, 5, 7 |
| B. Revocation timing legitimizes backdated forgeries | D3, D6 | 2, 5, 7b |
| 1. Signed statement rollback | D3, D4 | 2, 2b, 3 |
| 2. Question storage is a dictionary oracle and keeps copies | D7, D8 | 7b |
| 3. Invite multi-use and binding persistence (Codex P1 and P2) | D9, D12 | 9, 10, 12b |
| 4. Node grant administration | D9 | 8, 9 |
| 5. Trust labels must reach users and agents | D6 | 5 |
| 6. `open_rows()` contexts | D6 | 5, 6 |
| 7. Hub identity lineage | D2, D4, D5 | 2b, 5 |
| 8. Backend contract and `fleet_ask_user` | D7, D11, D12 | 12c, 16, 17 |
| 9. Frozen PQ wire profile | D1 | 1 |

Round 4:

| Finding | Design | Slices |
|---|---|---|
| S1. Membership removal for existing pins | D3, D5, D6 | 2, 5, 6 |
| S2. Outbound confidentiality from the wrong key | D5, D6 | 5, 6 |
| S3. Receipt clock bound | D3 | 5 |
| S4. Slice 11 needs the question flow | this plan | 11 |
| S5. Question retry after response loss | D5, D7 | 7b |
| Reply identity across a hub migration | D7 | 7b |
| Signed backend identification | D4, D11 | 2b, 13, 16 |
| Multi-use invites in the threat table | threat model | none (docs) |
| Fresh-station membership limitation | D3, D9 | 2, 7, 10 |
| Earlier `compromised_from` suppresses history | threat model | 7 |

## Revision 5 closure tests and decisions

These are named acceptance tests to implement in the owning slices, not tests
added by this documentation change. Earlier review mappings remain applicable.
D3's statement fixtures and malformed corpus are shared by Python verifiers,
the Hub's format-only parser where applicable, and later Hub Lite. Keep frozen
bm1 known answers and all existing adversarial expectations intact.

| Review disposition / contract | Named tests and observable result | Owning slices |
|---|---|---|
| Astra 2, Opus C: exact signed bytes | `test_statement_known_answer_encodings`: each of five fixtures verifies exact labeled unsigned bytes and complete signed digest. `test_statement_duplicate_fields_rejected`, `test_statement_limits_types_order_and_ranges`: recursive duplicates, floats/bools/nulls, Unicode, unknown fields, wrong label, count/size/depth bounds, unsafe integers and wrong unsigned digest change no state. | 2, 2b, 11, 12b |
| Astra 2 / manifest anchor | `test_manifest_key_bootstrap_requires_matching_fingerprint`: explicit 32-byte public-key file plus independently supplied full fingerprint succeed, missing file/mismatch/hub-supplied substitute do not write an anchor. | 2 |
| Opus B / bootstrap dependency | `test_hub_fetch_requires_persisted_current_identity`: local custody works first, network accept/import refuses without identity and never trusts a new hub namespace from fetched content. A persisted observed identity retains its documented semantics. | 2, 2b |
| Daybreak rollback / Opus C | `test_identity_origin_normalization_and_endpoint_mismatch`: case/default ports normalize consistently, invalid paths/userinfo/query/fragment/escapes/Unicode/trailing-dot/IPv6 zones fail, signed other origin or cross-origin redirect writes nothing. `test_bootstrap_policy_requires_seq_digest_floors`: id-only acceptance cannot claim fresh policy, old valid identity/membership below trusted floors fails, a later withheld statement remains an explicit limitation. | 2, 2b, 10 |
| Astra 3 / Opus A / Fable lookup | `test_migration_crash_preserves_history_revocations_and_highwater`: kill before/after journal commit and reopen, observing exactly old/new generation with provenance, floors and `(kind, signer)` marks intact. Old membership replay and revoked-kid acceptance fail after restart. `test_migration_requires_fresh_kid_and_current_hub_membership`: same-kid rebind fails, predecessor-signed new pairs pass only in accepted lineage, no observed use before newly signed membership and carried-floor reconciliation. `test_record_for_original_hub_principal_kid_at_signed_time`: old mail finds immutable old record and intervals, with no rewriting of current-hub records. | 2, 2b, 5, 6 |
| Astra 5 / trust-state precedence | `test_require_verified_blocks_existing_observed_pin`: seal/open/reply/spool drain refuse after policy change with provenance intact. `test_accept_cannot_unrevoke_kid`, `test_membership_relisting_cannot_clear_changed_or_pending_binding`: relisting never clears changed or pending binding, and fingerprint acceptance never revives a revoked kid or completes binding. `test_exact_fingerprint_accepts_nonrevoked_replacement`: explicit independent acceptance resolves changed after a nine-link chain or replacement of a revoked old key, while preserving old refusal evidence. `test_mcp_automatic_continuity_has_no_operator_acceptance_tool`: normal messaging may persist validated automatic state, but cannot install an anchor or manually accept a changed identity. | 2, 5, 6 |
| Astra 4 / Opus D/E / Daybreak namespace | `test_response_loss_then_hub_migration_recovers_original_send`: validate old stored header, use recorded old hub/rc/deadline and recover original id before any B send. Include old request committing after first lookup, both sides of the identity fence, timeout/restart and uncertain stale-key retry. `test_plaintext_lookup_is_content_unverifiable`: never count a prior plaintext row as sealed delivery. `test_expired_attempt_never_reseals`: missing/expired attempt state cannot become a fresh send. | 4, 6, 7b |
| Astra 7 / publication loss | `test_publish_response_loss_reconciles_exact_record_after_restart`: persist candidate, reconcile exact active/retired state, reject substitution/revocation and never regenerate due to timeout. `test_statement_upload_response_loss_reconciles_full_signed_digest`: identical rejected upload retry is reconciled by read-back, higher/different bytes are not assumed success. | 2, 2b, 3, 6 |
| Astra 6 / receipt identity | `test_revoked_tombstone_matches_original_receipt_without_authority`: full and verified-header digests match prior qualifying receipt, tombstone has only historical evidence, no label/reply/authority power. `test_full_to_tombstone_replay_transition`: same id/header transitions once, changed id/header and tombstone-to-full resurrection fail, fresh tombstone cannot create qualifying history. | 5, 7b |
| Daybreak / Opus retention | `test_sealed_hub_scoped_uniqueness_and_legacy_lookup_ambiguity`: explicit hub isolates A/B, omitted hub with multiple own matches returns 409, plaintext predecessor never becomes verified sealed success, cross-sender lookup leaks nothing. `test_routing_expiry_keeps_bounded_retry_receipt`: routing set to 1 day, deletion on second day hides inbox/ack/reply rows but preserves original-id reconciliation, never redelivery. `test_sealed_freshness_independent_of_long_routing_retention`: 3650-day routing does not admit old new sealed content or extend 90-day local retries. `test_retry_receipt_quota_and_expiry_fail_closed`: reserved receipt bytes count, retries do not extend expiry, full quota cannot evict unexpired receipt, expired local state sends nothing. These receipt tests depend on the D8 proposal being accepted. | 4, 6, 11, 12c |
| Fable / source parser drift | `test_plaintext_reply_to_sealed_or_tombstoned_parent_refused_when_optional`: both reply routes fail even under optional plaintext, including legacy client shapes, while legacy plaintext-parent behavior remains. `test_hub_crypto_parser_corpus_agreement`: checked-in bm1 plus malformed corpus have consistent acceptance at admission, and a purged header verifies with the crypto verifier. Keep `test_sealed_reply_hint_survives_parent_expiry` and exercise signed linkage after parent deletion. `test_identity_replacement_clears_only_unrelated_migration`: same-id matching proof survives, C never serves A-to-B proof. `test_new_envelope_and_key_record_match_current_hub`: foreign/mesh new writes fail, original-hub retry lookup succeeds. | 3, 4, 5, 11 |
| Fable / routine credential rotation | `test_routine_token_rotation_preserves_mesh_keys_and_history`: old bearer loses access, replacement works and keys/grants/history stay. `test_compromise_revocation_still_defaults_to_key_creation`: reenrollment/fingerprint acceptance cannot revive a kid. | 3, 6 |
| Fable policy deadlock / parent disposition | `test_staged_policy_never_advertises_unenforced_sealing`: inactive strict candidate is not active identity. `test_policy_activation_crash_and_rollback`: crash at every boundary gives old or matching new runtime/identity, concurrent plaintext cannot pass after strict activation, stale candidate/config and old-flag restart refuse, rollback requires higher-seq operator transition. Retain both existing active-policy mismatch guards. The proposed staged D8 path requires parent/Astra/Daybreak review before implementation. | 3, 4, 7, 11 |
| Opus dependency / rollout | `test_mesh_floor_fixture_compatibility` and supported-platform CI retain 50.0.2 pending evidence, never reintroduce an unsupported Intel route. `test_mixed_version_sealed_mail_rollout`: old clients cannot reply plaintext to sealed/tombstoned parents, labels reach every renderer, downgrade is explicit, question creation under require-sealed refuses until 7b. `test_unsigned_hub_answer_never_authenticates_after_7b` keeps the adversarial authority gate. | 1, 5, 6, 7, 7b, 11 |

Pending product decisions have concrete alternatives: retain minimal retry
metadata for 90 days after short visible routing deletion, or shorten retry
support and fail closed at the routing horizon. The former costs metadata
storage and changes deletion semantics, the latter gives shorter retry
availability. Prefer staged restart for policy activation. In-process atomic
activation needs stronger concurrency evidence and must preserve the same
advertisement/enforcement invariant. Parent, Astra and Daybreak review settles
these proposals before source changes.

Revision 5 follow-up checks from Astra and Daybreak:

- `test_migration_rejects_older_valid_new_identity_policy`: the old-key
  migration binds the exact first new identity sequence and complete signed
  digest. Commit its new-signer floor atomically. Another valid new-key policy
  cannot substitute for that object, even with the same endpoint and key.
- `test_manifest_anchor_rotation_continues_publication_counter`: K2's
  publication advances above K1's per-kind Hub counter, while independent
  client signer marks and old revocations remain intact after restart.
- `test_inactive_relaxation_cannot_change_client_sealing`: serving a higher
  sequence lax candidate cannot turn accepted strict policy into plaintext.
  Staged candidates tighten only. Remote relaxation remains outside version 1.

The first sealed-mail milestone needs opening, sealing, labels, docs,
adversarial tests, Hub prerequisites and mixed-version handling together.
Merge is separate from deployment. Every station needs the combined opener
and sealer release before runtime require-sealed activation. Questions stay
disabled in that mode until 7b, and labeled Hub-trusted elsewhere. A
hub-independent operator-authority claim also needs 7b's migration
and negative tests. Invites, no-hub and Hub Lite keep their existing scope.

## #2005: sealed message bodies

### Slice 1: crypto core and the `mesh` extra

Pure functions, no I/O.

Files:
- `pyproject.toml`: add the `mesh` extra. Add `cryptography>=50.0.2,<52` to the
  `dev` extra.
- `src/brigade/fleet_mesh_crypto.py` (new): `MeshExtraMissing`, the ML-KEM
  backend probe (`mesh_backend_unsupported`), Ed25519 and hybrid KEM key
  generation, key record build and verify, `kid` and fingerprint derivation,
  canonical encoding, binary payload fields, padding, the routing header,
  signed header and envelope of design D2, the retry commitment, the label
  constants, and `seal()` and `open()` with `Suite(KEM.MLKEM768_X25519,
  KDF.HKDF_SHA256, AEAD.AES_256_GCM)`, the frozen `bm1` profile of design D1.
  Private KEM keys are the 32 byte X-Wing seed, expanded with
  `hashlib.shake_256` into the ML-KEM-768 seed and the X25519 key.
- `tests/fixtures/mesh/` (new): the X-Wing vectors from
  draft-connolly-cfrg-xwing-kem revision 11 Appendix C, the Appendix A.5
  vector from draft-ietf-hpke-pq revision 05 (KEM `0x647A`, KDF `0x0001`,
  AEAD `0x0003`), each with its source revision and retrieval date, and a
  checked-in `bm1` envelope.
- `tests/mesh_hpke_reference.py` (new, test-only): an RFC 9180 base-mode key
  schedule on `hmac` and `hashlib`, used only to check pyca against the
  vectors.
- `.github/workflows/ci.yml`: one leg runs the mesh tests with the extra, one
  runs the plain-text suite without it, one installs the floor (50.0.2). The existing
  macOS and Windows legs
  need source-backed supported-platform review before making install claims. A
  cross-version job seals with the floor and with the newest release and
  opens each output with the other.

Tests (`tests/test_fleet_mesh_crypto.py`):
- Seal and open round trip for the recipient and sender wraps.
- Changing any single routing header field, `ct_sha256`, `wraps_sha256`,
  `rc`, the ciphertext, a wrap or the signature makes `open()` fail with a
  fixed code.
- An envelope re-signed by a different sender with `from` changed fails to
  unwrap and fails the AEAD check.
- An envelope forwarded unchanged to a third party fails the `to` check, and
  one with `to` or `rcpt` rewritten fails the signature.
- The signed header verifies alone after `ct` and `recipients` are removed.
- Unknown `v` or `suite` gives `envelope_unsupported`.
- Padding lands on 1 KiB buckets and strips back exactly.
- Two seals of the same input draw different content keys and produce the
  same `rc`. Changing any semantic field changes `rc`, and changing
  `created_at` alone does not.
- Every label is distinct, and a signature made under one label fails under
  each of the others.
- The largest legal message seals under 96 KiB, and the client reports
  `message_too_large` against a 32 KiB `max_envelope_bytes`.
- Key records: changing any covered field breaks `self_sig`.
- Known answers, layer 1 (KEM): for every X-Wing vector, expanding the seed
  gives the vector's 1216 byte public key, and decapsulating the vector's 1120
  byte ciphertext with pyca's ML-KEM and X25519 primitives and the SHA3-256
  combiner gives its shared secret.
- Known answers, layer 2 (HPKE schedule): with the A.5 vector's private key
  and `enc`, the reference schedule reproduces its `shared_secret`, `key`,
  `base_nonce` and first ciphertext.
- Known answers, layer 3 (pyca agreement): a message sealed by the reference
  schedule under KEM `0x647A`, KDF `0x0001` and AEAD `0x0002` with empty
  associated data opens with pyca's `Suite.decrypt`, and a pyca `encrypt`
  output opens with the reference schedule. The same holds with AEAD `0x0003`
  against the A.5 key pair.
- Profile constants: the suite object reports KEM `0x647A`, KDF `0x0001` and
  AEAD `0x0002`, public keys are 1216 bytes, `enc` is 1120 bytes, and a wrap
  of a 32 byte key is 1168 bytes. A change to any of these fails the test,
  because `bm1` can never change meaning.
- The checked-in `bm1` envelope opens byte for byte on every supported
  `cryptography` version, and an envelope sealed by the floor opens on the
  newest release and the reverse (the cross-version job).
- A 20 KiB padded payload seals to at most 32,768 bytes (round 2 measured
  31,639).
- With `cryptography` blocked in `sys.modules`, every entry point raises
  `MeshExtraMissing`. With ML-KEM reported unavailable, sealing raises
  `mesh_backend_unsupported` and never falls back to `KEM.X25519`.

Acceptance: the focused gate passes on the floor and newest `cryptography`.

### Slice 2: local key store, trust levels and the keys subcommands (needs 1)

Files:
- `src/brigade/fleet_mesh_keys.py` (new): key files, the retry key and retired
  keys under `<brigade_home>/fleet/mesh-keys/`, a pin store per accepted hub
  identity with the statuses of design D3 including `unverified`, continuity
  chains of up to 8 links, key history with validity intervals,
  `compromised_from` handling with the earlier-wins rule, the manifest key
  pin, membership manifests, key manifests, revocation statements,
  fingerprint-list verification, the per-kind high-water marks
  (`signed_statement_rollback`, `signed_statement_conflict`), the optional
  trusted statement floors, exact D3 signed-statement schemas/limits and
  complete signed-object digests, trust provenance distinct from use refusals,
  irreversible kid revocation, membership checked at every use of an `observed`
  key with the computed `observed_not_member` status, and pruning.
  File handling follows `read_message_token_file` and
  `src/brigade/authority_key.py`.
- `src/brigade/cli/fleet_keys.py` (new), registered from `cli/fleet.py`:
  `fleet keys show`, `rotate`, `accept <principal> --fingerprint <fp>`,
  `accept --manifest <file>`, `accept --fingerprints <file>`,
  `trust-manifest-key --public-key-file <file> --fingerprint <fp>`,
  `manifest sign` and `membership sign` (admin machine), `membership import <file>`, `membership require
  --seq <n> --sha256 <digest>`, `revoke-statement sign`
  and `import`, and `prune`. No `--all-changed`. `rotate` writes locally until
  slice 6. File-only custody/verification can land first. Network key/manifest
  commands remain unavailable until 2b provides the endpoint's persisted current
  identity, then fail `hub_identity_unverified` without that entry. An observed
  accepted identity still has D4's existing semantics, without claiming current
  policy from an id alone. Never derive a new pin namespace from a fetch.

Current local custody foundation: private files require exactly 0600, and
`<brigade_home>`, `fleet` and `mesh-keys` require current-user ownership and
exactly 0700. Every custody-path component, including ancestors, must be
physical. If a home or temporary-directory ancestor is symlinked, the operator
must choose an already physical path. No automatic path resolution occurs.
Native Windows custody is unsupported. Retired keys are not automatically
pruned. Rotation that would exceed the 8 MiB serialized-state bound fails
closed with `mesh_keys_invalid` without changing the saved generation. A
dedicated capacity diagnostic remains deferred.

Tests (`tests/test_fleet_mesh_keys.py`):
- Key files are exactly 0600 under exactly 0700 Brigade home, fleet and private
  directories, and symlinks anywhere in the custody path are refused.
- A first key for a principal the accepted membership manifest lists is
  recorded `observed`, never `verified`. With
  `BRIGADE_FLEET_MESSAGES_REQUIRE_VERIFIED=1` it is `unverified`.
- Third station: station C, which did not create the invite, holds a
  membership manifest that lists only own-fleet principals. A hub that serves
  teammate T with `role: node`, or as any other own-fleet shape, leaves T
  `unverified` on C. A hub-minted principal is `unverified` the same way. A
  key manifest signed by the operator that lists T's fingerprint makes T
  `verified` on C.
- No membership manifest: every first key is `unverified`.
- Removal: manifest 7 lists P and P is pinned `observed`. Accepting manifest
  8 without P makes P `observed_not_member`, and the pin and key history are
  unchanged. Manifest 9 listing P again makes it `observed`, and a
  fingerprint acceptance makes it `verified`, which manifest 10 without P does
  not change.
- Removal and restart: after manifest 8 is accepted, a restart still reports
  P as `observed_not_member`, and replaying manifest 7 is refused with
  `signed_statement_rollback`.
- Continuity after removal: while P is removed, the hub serving an unrelated
  key for P makes the pin `changed`, and a later manifest listing P leaves it
  `changed`. A valid successor chain instead advances the pin, and relisting
  makes the new kid `observed`.
- Trusted floor: with a floor of `seq` 8 and its digest, manifest 7 is
  refused, a different manifest at `seq` 8 is refused, and until a manifest at
  or above 8 is accepted every unconfirmed key is `unverified`.
- A membership manifest signed by any key other than the pinned manifest key
  is refused whole, and changing the hub-reported role of a listed or an
  unlisted principal never changes its status.
- Rollback, for each of membership manifests, key manifests and revocation
  statements: after `seq` 5 is accepted, `seq` 4 is refused with
  `signed_statement_rollback`, a different statement at `seq` 5 is refused
  with `signed_statement_conflict`, the accepted statement replayed is a
  no-op, and the marks survive a restart.
- A revocation statement that drops an earlier revocation or moves a
  `compromised_from` later is refused. When the hub and a signed statement
  disagree, the earlier `compromised_from` applies.
- `accept` without `--fingerprint` is refused. With a fingerprint that does not
  match the fetched record it changes nothing, and with a matching one the
  entry becomes `verified`.
- A manifest signed by an unpinned key, or with any entry altered, is refused
  whole. A fingerprint list with one mismatch accepts nothing.
- A chain of 8 valid links advances the pin and keeps its trust level. A chain
  of 9 yields `changed`. A chain with a revoked link, a gap, or a
  non-increasing `created_at` yields `changed`.
- Archived mail verifies against a retired key inside its interval and fails
  outside it.
- No MCP tool exposes operator pin acceptance, trust-anchor replacement or
  manual identity acceptance. Existing messaging may trigger validated automatic
  observation/continuity, statement or authenticated-migration persistence.
  `fleet_mcp.tool_specs()` names stay unchanged. Automatic paths share the
  verifier and cannot replace independently supplied acceptance evidence.

Acceptance: the focused gate passes, and the keys help output lists the
subcommands.

### Slice 2b: hub identity on the client (needs 1, 2 and 3)

Files:
- `src/brigade/fleet_mesh_hub_identity.py` (new): identity statement signing
  and verification including the signed `backend` and `capabilities` fields,
  the hub id derivation, the endpoint-to-identity store (a
  separate file from the pin store) with its authenticated lineage, migration
  statements per D3, exact D4 origin normalization, independently supplied
  identity seq/digest floors, policy-freshness provenance, the identity statement
  high-water mark, the journaled trust-state migration transaction, historical
  `record_for(hub, principal, kid, signed_created_at)` lookup, and the
  `hub_identity_changed` and `hub_identity_superseded` refusals.
- `src/brigade/cli/fleet_hub_identity.py` (new): `fleet hub-identity init`,
  `show`, `publish` (admin token), `migrate` and `accept --hub-id <id>`.
  A bootstrap claiming verified current policy also requires `--seq <n>
  --sha256 <digest>` from an independent source. The bare id still records
  identity verification only, as before, never a current-policy guarantee.

Tests (`tests/test_fleet_mesh_hub_identity.py`):
- A known endpoint serving a different hub id or key stops every message
  operation with `hub_identity_changed` and creates no new pin namespace.
- A migration signed by the old key preserves immutable old-hub history and
  atomically advances current identity with pins, provenance, revocations,
  high-water marks and floors. One signed by any other key is refused. Fresh
  principal keypairs with predecessor signatures are required under the new
  hub, and new observed use requires a new current-hub signed membership.
- `accept --hub-id` with the right id carries the pins, and with a wrong id
  changes nothing.
- A statement whose `hub_id` is not the hash of its key is refused.
- A new endpoint served by the same key with a fresh signed statement is
  accepted.
- Rollback: after identity statement `seq` 3 is accepted, a statement with
  `seq` 2 (for example one with `require_sealed` off or a larger
  `max_envelope_bytes`) is refused with `signed_statement_rollback`, and a
  different statement at `seq` 3 with `signed_statement_conflict`. The same
  holds for a lower-`seq` statement that changes only `backend` or
  `capabilities`.
- Backend fields: a statement with `backend` outside `full` and `hub-lite`, or
  with unsorted or duplicate capabilities, is refused as malformed. An
  unknown capability name is ignored, and changing either field after signing
  breaks the signature.
- Lineage: after a signed migration from `h_old` to `h_new`, an envelope
  signed under `h_old` before the migration verifies against the key history
  recorded under `h_old`. One signed under `h_old` with a `created_at` after
  `superseded_at` plus the skew opens as `hub_identity_superseded`. New sends
  carry `h_new` only. A hub that serves an identity outside the lineage
  without a migration or operator acceptance cannot add it.
- Distinct namespaces: the same `(from, client_id)` under `h_old` and `h_new`
  are separate seen-id, receipt and pending entries, and a reply signed under
  `h_new` whose `in_reply_to` names an `h_old` request links to it.

Acceptance: the hub identity can change only through a signed migration or
operator verification, cannot roll back, and original-hub history survives a
crash during migration. Pass the named revision 5 tests above before any
hub-facing client code uses the contract.

### Slice 3: hub key directory and hub identity storage (no crypto, waits for #1939)

Files:
- `src/brigade/fleet_hub_message_keys.py` (new): `fleet_message_keys` with one
  active key per principal, `keys_publish` and `keys_get`, format checks, the
  `prev_kid` rule, `principal_key_exists`, refusal of revoked kids, and
  `compromised_from`. Validate a new record's hub against active identity by
  string comparison. Add the distinct routine credential-rotation operation
  from D3 before rollout, preserving compromise revocation's key-creation default.
- `src/brigade/fleet_hub_identity.py` (new): `hub_meta` storage of the signed
  statement and migration, the `hashlib` check that `hub_id` matches the key,
  the admin upload action, and `fleet_mesh_statements` with the admin
  `POST /mesh/statements` upload (size check and increasing `seq` only) and
  the `manifests_get` action. Reject identity sequence rollback/conflicts,
  atomically clear unrelated migration proof on identity replacement, and retain
  only matching proof on same-identity updates. Publication/statement retries
  need exact candidate read-back reconciliation before any fresh key generation.
- `src/brigade/fleet_hub_messages.py` and `fleet_hub_paging.py`: route the new
  actions. `who` reads key entries live on every page instead of from the
  #1939 snapshot.
- `src/brigade/fleet_hub.py`: revoke keys in the same transaction as
  `revoke_node`, the schema bump, and `GET /mesh/identity` in
  `fleet_hub_http.py`.
- `revoke_principal`: the same key revocation call.

Tests (`tests/test_fleet_hub_message_keys.py`):
- A caller can publish only its own record. A second record without
  `prev_kid` gets `principal_key_exists`.
- Revoking a node or principal revokes its keys with `compromised_from`, and a
  revoked kid cannot be published again.
- Paging: between two pages of one `who` snapshot, a key published for a
  principal on the second page shows on that page, and a key revoked for a
  principal on the second page is not served there.
- `who` shows keys only for principals the caller's grants allow.
- `/mesh/identity` serves the uploaded statement unchanged, and refuses an
  upload whose `hub_id` does not match its key or that lacks the admin token.
- Importing the hub modules with `cryptography` blocked succeeds.
- A statement upload with a `seq` not above the stored one is refused, and
  `manifests_get` returns the stored blobs unchanged to any principal.

Acceptance: after revocation `who` serves no key for that node, including on
continuation pages.

### Slice 4: sealed storage, retry lookup, retention and admission (needs 3, waits for #2009 and #2010)

Files:
- `src/brigade/fleet_hub_sealed.py` (new): envelope parse and header checks by
  string comparison, the `max_envelope_bytes` cap (96 KiB default),
  `payload_hash` over the envelope bytes, `recipient_key_stale` and
  `sender_key_stale`, the symmetric `reply_to` and `in_reply_to` presence
  rule, `sent_lookup` by `client_id`, the existing id in the 409 body, the
  7 day post-ack purge to a `content_expired` tombstone, the unacked TTL, and
  per-sender and fleet stored-bytes admission. Close the D5/D8 prerequisites:
  hub-scoped sealed uniqueness and lookup, ordered identity fence, 90-day sealed
  admission/hard TTL independent of routing, and dual-parser corpus.
  Implement separate retry receipts only after the proposed D8 metadata-retention
  choice is settled. Keep 1..3650-day routing config and retained `sealed_reply_to`.
  D8 staged activation is likewise a review prerequisite, not permission to
  loosen active-policy guards.
- `src/brigade/fleet_hub_messages.py`: `envelope`, `envelope_bytes` and
  `content_state` columns, the sealed path in `send()` and `reply()`, sealed
  rows in `_row()`, and `count` (#2009) treating sealed rows like any row.
- `brigade fleet serve --require-sealed` and its environment variable.

Current foundation status: the common Hub storage guard refuses plaintext
replies to retained sealed/tombstoned parents even with plaintext optional,
including legacy retry replays. Pre-upgrade plaintext replies already stored
remain readable under existing retention and are not retroactively encrypted
or quarantined. After parent deletion, only an existing
detached reply with an exact non-null stored payload hash can be retried.
New missing-parent replies and ambiguous null-hash retries are refused. The
other prerequisites above remain open. This storage boundary does not
establish deployed or end-to-end enforcement.

Tests (`tests/test_fleet_hub_sealed.py`), using syntactically valid fake
envelopes because the hub never verifies cryptography:
- A sealed send stores empty `subject` and `body` and NULL `reference`,
  `thread` and `sender_label`. A sealed send that also carries plain fields
  is refused.
- A wrong `from`, a stale `from_kid` or a stale recipient kid is refused, and
  a 409 stores nothing.
- The duplicate check runs before the key id checks.
- Identical bytes with the same `client_id` return the stored row. Different
  bytes get 409 `message_client_id_conflict` with the existing id.
- `sent_lookup` returns only the caller's own row: another sender's
  `client_id` gives `not_found`, and a purged row returns its tombstone with
  the signed header.
- A `reply_to` without `in_reply_to`, or the reverse, is refused.
- An acked envelope becomes a tombstone after 7 days and keeps its header and
  signature. Unacked mail past the TTL is removed.
- A send over the per-sender stored-bytes limit gets `sender_storage_quota`,
  and one over the fleet limit gets `hub_storage_full`.
- `--require-sealed` refuses plain text with `message_sealing_required`.
  Without it, `tests/test_fleet_messages.py` passes unchanged.

Acceptance: the hub stores sealed rows without plaintext columns, enforces its
policy, and supports retry recovery after purge.

### Slice 5: opening, pending requests, receipts, contexts and trust labels (needs 1, 2, 2b, 3 and 4, waits for #2009)

Opening lands before sealing because a sealed reply has to open the original.

Files:
- `src/brigade/fleet_mesh_open.py` (new): `open_rows(rows, context)` in the
  order of design D6, with the required `inbound` or `outbound` context and
  `open_context_mismatch`, the seen-id cache with its start time and
  `unchecked` label, freshness and `MAX_FUTURE_SKEW`, hub identity lineage,
  revoked-key history, `sender_key_not_member`, per-envelope confidentiality
  by context, tombstones, legacy labels, structural pattern checks and the
  failed-row shape.
- `src/brigade/fleet_mesh_receipts.py` (new): the append-only receipt log of
  design D3, insert-only triggers, local UTC receive times, the measured
  clock offset stored with each receipt, the strict
  `MAX_RELATIVE_SKEW` plus `TIMESTAMP_PRECISION` rule, the full-envelope and
  verified signed-header digests and full-to-tombstone replay transition, and pruning limited to
  rows past the freshness window whose kid is not revoked.
- `src/brigade/fleet_mesh_pending.py` (new): the local pending request store
  keyed by the full `(hub, from, client_id)` triple, the outbox records with
  the recipient kid and its trust status at seal time, the ask idempotency
  records, and reply linking (design D5).
- `src/brigade/fleet_client_messages.py`: every call site passes its context
  from the D6 table. `send()` and `reply()` return rows, `sent_lookup` and
  `sent()` use `outbound`. `list_messages()` (with `open_limit`), `ack()`
  and `reply()` opening the original use `inbound`.
- `src/brigade/fleet_mcp.py`: the new structural fields and error codes, and
  `key: {"fingerprint", "status"}` and `confidentiality` in `fleet_who`.
- `src/brigade/fleet_inbox_delivery.py`: open only the shown rows in the
  `inbound` context, with cached keys, the read-only pending store and no
  extra hub call. `render()` (line 326) adds the fixed trust label strings to
  each message header.
- `src/brigade/dot/state.py`: `DotState.hub_upsert()` (line 195) keeps
  `sealed`, `key_status`, `confidentiality`, `authenticity`, `replay`,
  `reply`, `history` and `open_error` in new columns, with a schema
  migration. Rows stored before it render as `authenticity: unknown`.
- `src/brigade/dot/hub_inbox.py` and the Dot message tools: render the stored
  labels, and replace the `row.get("reply_to") == msg_id` match at line 424
  with a match of the verified `in_reply_to` against the pack's own record of
  the `client_id` it sent for `msg_id`.
- `src/brigade/hub_messages.py`: `legacy_message()` (line 107) copies the same
  label fields, and its consumers (`dot/inbox_tools.py`,
  `grokbot_t3/inbox.py`) render them.
- `src/brigade/doctor_*`: a clock offset warning.

Tests:
- `tests/test_fleet_mesh_open.py`:
  - A tampered ciphertext or wrong signer is rejected before `_frame()`, and
    the result contains no plaintext.
  - Another hub id outside the lineage, an envelope older than the window and
    a future-dated one each get their own code.
  - A replay under a new hub message ID opens as `replayed`, and the seen
    entry is persisted before the row is returned.
  - On a fresh station, an envelope created before the cache start opens as
    `unchecked`.
  - A hub `reply_to` pointing at a different request does not link the reply.
    A reply whose signed `in_reply_to` matches a local record links even when
    the hub's `reply_to` is altered. Presence mismatches open as
    `reply_mismatch`.
  - A reply signed after the request's deadline does not link.
  - Plain-text rows are labeled `unauthenticated-legacy`. A plain-text row
    from a pinned sender with no local pre-pin record is labeled
    `unauthenticated-after-pin`, whatever its hub timestamp.
  - A tombstone opens as `content_expired` with a verified header.
  - A failed row contains no sender-chosen string outside the fence.
  - `sender_label` is dropped for `agent:` senders and for `observed` keys.
  - Third station: mail from a teammate, or from a principal the membership
    manifest does not list, opens as `sender_key_unverified` on a station that
    did not create the invite, whatever role the hub reports.
  - Removal: after the station accepts a manifest that drops P, mail from
    P's `observed` key opens as `sender_key_not_member`, including mail
    received while P was listed, and a reply from P links nothing. After a
    restart it still opens that way. When a later manifest lists P again,
    the same mail opens normally.
  - Confidentiality per envelope: a station whose own key is `verified`
    sealing to an `observed` recipient key gets `confidentiality:
    "opportunistic"` on the `outbound` row, and `end_to_end` for a `verified`
    recipient key. The label comes from the outbox record, so verifying or
    changing the recipient pin later does not change it. An outbound row
    with no outbox record takes the kid's status from the pin store, and
    `unknown` when the kid is not there. An inbound row's label follows the
    sender key.
  - Contexts: each call site in the D6 table passes the context the table
    names (asserted per function). A station's own sent row opens in
    `outbound` with `direction: "outbound"`, and the same row passed to
    `inbound` fails with `open_context_mismatch` and no recipient error. An
    inbox row passed to `outbound` fails the same way.
- `tests/test_fleet_mesh_receipts.py`:
  - The backdated forgery: key compromised at 10:00, a message signed with
    `created_at` 09:59 and received at 10:10, revocation learned at 10:20. It
    opens as `sender_key_revoked`.
  - A message received at 09:30 with its digest in the receipt log, from a key
    later revoked from 10:00, opens as history with `key.status: "revoked"`
    and `history: "received_before_compromise"`. One received at 09:57 opens
    as `sender_key_revoked`.
  - Boundary, with `compromised_from` 10:00:00, a 10 minute
    `MAX_RELATIVE_SKEW` and 1 second precision: a receipt at 09:49:58
    (immediately before the bound) is history, and receipts at 09:49:59
    (exactly at it) and 09:50:00 (immediately after) open as
    `sender_key_revoked`.
  - A receipt recorded while the measured clock offset exceeded 5 minutes,
    or with no offset measured in the previous 24 hours, never qualifies,
    whatever its receive time.
  - The offset comes only from the operating system's time synchronization
    status. A hub `Date` header that matches a slow local clock does not
    qualify a receipt: with the local clock 30 minutes slow and a hub `Date`
    agreeing with it, a message received at true 10:10 against a 10:00
    `compromised_from` opens as `sender_key_revoked`. With no or
    unsynchronized OS status, no receipt qualifies.
  - A message with a receipt for a different digest under the same triple,
    or with no receipt (a fresh station), opens as `sender_key_revoked`.
  - Revoked-key history never links or completes a pending request, carries
    no `sender_label`, and is not operator-authenticated.
  - `UPDATE` and `DELETE` on the receipt table fail, receive times come from
    the local clock and never from a hub column, and pruning keeps rows for
    revoked kids.
- `tests/test_fleet_inbox_delivery.py`: the rendered output (the actual
  string from `render()`) shows `sealed`, the key status,
  `opportunistic confidentiality` for an `observed` sender, `replay
  unchecked`, `reply linked` or `reply unlinked`, `unauthenticated-legacy`
  for a plain-text row, and the `open_error` for a failed row. Label text
  comes only from the fixed strings, so a row whose sender chose label-like
  text shows it inside the fence only. Delivery completes no request, and an
  unopenable row renders the fixed body without raising.
- `tests/test_dot_inbox_tools.py`: a forged `reply_to` does not match, a
  failed row does not raise, and the Dot tool output for a stored row shows
  its key status, confidentiality, authenticity, replay and reply labels. A
  row stored before the migration shows `authenticity: unknown`. A stored
  outbound row sealed to an `observed` recipient renders `opportunistic`.
- `tests/test_fleet_messages.py`: `legacy_message()` output keeps the label
  fields, including the per-envelope confidentiality of an outbound row, and
  the views its consumers render (the Dot inbox tools and the Grok Bot T3
  inbox) show them.

Acceptance: the "tampered ciphertext or wrong signer is rejected before the
agent sees the text" bullet of #2005 holds, and every trust label reaches the
three rendered surfaces. Run the focused gate with the extra and with it
blocked.

### Slice 6: sealing on send, reply, retry and the spool (needs 5, waits for #2010)

Files:
- `src/brigade/fleet_mesh_send.py` (new): the seal decision table, key lookup
  through pins and `who`, the downgrade rule, a fresh content key on every
  seal, `rc`, local reply construction, pending request records, the outbox
  record of the recipient kid and its trust status written before the first
  transmission, and retry
  recovery (409 with id, hub-scoped `sent_lookup`, original-attempt header
  verification then `rc` comparison using its recorded hub/deadline, the wrap
  fallback and `content_unverifiable`). Preserve every attempt across migration,
  recover/fence it before a new attempt and fail closed after local expiry.
  Require-verified and every current-use refusal apply on each spool drain.
- `src/brigade/fleet_client_messages.py`: `send()`, `reply()` and the spool
  call into `fleet_mesh_send`. The spool stores the sealed envelope before the
  first transmission. `seal: "pending"` records for offline sends and replies.
  `_drain_locked` keeps and reseals on `recipient_key_stale` and
  `sender_key_stale`.
- `src/brigade/fleet_action_adapters.py`: `_inbox_main` sends through `send()`.
- `src/brigade/fleet_mesh_keys.py`: publish on first use and on rotate,
  `local_key_superseded`, and fetching signed statements with
  `manifests_get` through the slice 2 verifier.

Tests (`tests/test_fleet_mesh_send.py`):
- Every row of the decision table, including `observed` with and without
  require-verified, `observed_not_member`, `unverified`, `pending_binding`
  and `changed`.
- Removal: after a manifest drops P, a send to P fails with
  `recipient_key_not_member` and nothing is spooled as plain text. A spooled
  sealed send to P drains with the same refusal after the manifest arrives.
- Confidentiality on every send surface: sealing to an `observed` recipient
  key labels the `send()` and `reply()` responses, the `fleet_sent` and CLI
  sent lists, and a `sent_lookup` retry recovery `opportunistic`, and sealing
  to a `verified` key labels them `end_to_end`. The label survives a restart
  and a retry that reseals.
- Retry recovery verifies the `sent_lookup` row in the `outbound` context,
  and a reply opens its original in the `inbound` context.
- The spool holds the sealed envelope before the first request, and a forced
  retry sends identical bytes.
- A stale-key 409 keeps the spool record and reseals it.
- Crash and resend with a deterministic `client_id` (patrol and Dot shapes)
  returns the stored id. The same `client_id` with a different body is a
  permanent 409.
- An MCP `fleet_send` retried with the same `client_id` and arguments returns
  the original id, and with a changed body gets #2010's permanent 409.
- Retry recovery after the stored row is purged to a tombstone still returns
  the stored id through `rc`.
- A hub that answers `sent_lookup` with a row not signed by the caller does
  not count as delivered.
- A pinned recipient with no key gets `recipient_key_missing`, never plain
  text.
- Round trips through MCP and through the message CLI, and a dump of every hub
  table contains neither the subject nor the body marker.

Acceptance: all four #2005 acceptance bullets hold, including the database
dump with real sealing.

### Slice 7: user docs for sealed mail (needs 6)

Files: `docs/fleet-messages-sealed.md` (new), links from the fleet messaging
docs and `docs/fleet-mcp-events.md`.

Content: install with the retained 50.0.2 floor, supported-platform evidence
before offering Intel macOS/Windows arm64 instructions, and
`mesh_backend_unsupported` for builds without ML-KEM. Mark the old Intel
`--only-binary` 48.0.x advice stale. Cover hub and
local policy, key commands, membership manifests, how to compare
fingerprints out of band, the local boundary statement from design D3, what
stays plain text, the threat model, and, until slice 7b merges, the statement
that ask_user answers are Hub-trusted wherever plaintext is allowed, and
unavailable under require-sealed. Distinguish merged code from deployment,
require opener/sealer/labels/docs in one release, and document the mixed-version
upgrade/refusal procedure and fail-closed schema downgrade. A visible section titled
"Opportunistic confidentiality" explains that mail to and from `observed`
peers is readable by a hub that substituted the key on first contact, shows
the `confidentiality: opportunistic` label as each surface renders it, and
recommends `BRIGADE_FLEET_MESSAGES_REQUIRE_VERIFIED=1` for anyone who
verifies fingerprints. The membership section explains removal and
`observed_not_member`, the fresh-station limitation of the rollback rule and
the optional trusted floor. The revocation section explains
`compromised_from`, the receipt rule and its clock assumption, that history
never confers authority, and that a hub reporting an earlier
`compromised_from` can suppress history but never grant authority.

Acceptance: `brigade guard scan` with
`src/brigade/guard/policies/public-repo.json` is clean, Vale reports no
errors, and the documented-command inventory test passes.

### Slice 7b: sealed operator questions (needs 6, waits for #1951)

Files:
- `src/brigade/fleet_ask_user.py`: the question goes out as a sealed `kind:
  question` envelope with a signed deadline through `fleet_mesh_send`, not
  `messages._online()`. The ask idempotency record (design D5) is persisted
  before the first transmission and reused for every retry with the same
  `request_id`. `_terminal()` is replaced by one verification function
  used for hub-inbox and desktop answers (design D7).
- `src/brigade/fleet_hub_questions.py`: `question_create` takes the envelope.
  The hub no longer builds the message. `fleet_questions` drops `question`,
  `answer` and `payload_hash` and keeps references (`message_id`,
  `answer_id`) and routing metadata (design D7), with a schema migration that
  clears the dropped columns of existing rows. Idempotency on
  `(caller, generation, request_id)` compares `to`, `kind` and `rc` from the
  referenced question row's signed header. The trigger sets only `status`,
  `answer_id` and `answered_at`.
- `docs/fleet-messages-sealed.md`: replace the interim hub-trusted/disabled
  note only when signed questions, authority checks and plaintext-data migration
  pass. Older deployments still retain the interim behavior.

Tests (`tests/test_fleet_ask_user_sealed.py`):
- A hub that writes `status='answered'`, `source='operator_reply'` and
  `answered_by=<operator>` with its own answer text produces
  `operator_authenticated: false`.
- An answer signed by the operator's `verified` key, linked to this ask and
  inside the deadline, is authenticated. The same answer linked to a different
  pending question, signed after the deadline, sent to another principal, or
  signed by an `observed` operator key is not.
- A replayed operator answer from an earlier question is refused.
- Retry after response loss: the first public `ask()` call stores the
  question and the client sees a timeout. The clock advances by a minute and a
  second `ask()` with the same `request_id` and arguments returns the existing
  question, with the original `client_id` and deadline, and no conflict. The
  same holds after a process restart between the two calls. A second `ask()`
  with the same `request_id` and a different question or timeout gets
  `question_request_conflict` without a hub request.
- Across a hub migration: a question asked under `h_old` and answered by the
  operator after a signed migration to `h_new`, with `in_reply_to` naming the
  `h_old` triple, is authenticated. An answer whose `in_reply_to` names the
  same `(from, client_id)` under any hub id outside the lineage is not.
- A desktop answer without an operator signature is fenced and not
  authenticated. A desktop answer signed with the operator's own key on the
  same machine is authenticated.
- An answer signed by an operator key that is later revoked opens as history
  at most and is never authenticated.
- A dump of the hub database contains neither the question nor the answer
  marker.
- Hash guessing: for a short question and every timeout from 1 to 120, the
  SHA-256 of `json.dumps([question, timeout])`, of the question alone and of
  the answer alone appears in no column of any hub table.
- Idempotency: a retried `question_create` with the same `request_id` and a
  resealed envelope of the same question returns the existing question, and
  the same `request_id` with a different question gets
  `question_request_conflict`, including after the question's ciphertext is
  purged to a tombstone.
- No copies after purge: after the question and answer rows are purged to
  tombstones, no table contains either envelope's `ct` or `recipients`
  values, and after retention removes the message rows, the question row is
  gone too.

Acceptance: no unsigned hub field can confer operator authority, and the hub
stores no question or answer content, copy or plaintext hash.

## #2006: teammate invites

### Slice 8: teammate role, symmetric grants and the audit log (no crypto)

Files:
- `src/brigade/fleet_hub_audit.py` (new): `fleet_audit_log`, an append helper
  that refuses secret-shaped values, admin paging, and aggregation for refused
  redemptions.
- `src/brigade/fleet_hub_messages.py`: `role` and `person` columns, audit rows
  for grant changes and revocation.
- `src/brigade/fleet_hub_message_grants.py`: one caller lookup helper that
  treats only enrolled node ids as unrestricted, replacing the prefix tests in
  `permits()`, `require_granted()`, `require_recipient_cap()`,
  `_station_read()` and `fleet_hub_events.permits()`. Symmetric grants for any
  pair that includes a teammate, both checked in the insert's
  `BEGIN IMMEDIATE` transaction. Node grant storage: `principal_id` accepts an
  enrolled, unrevoked node id, consulted only in pairs that include a
  teammate, and revoking a node deletes its rows in the same transaction.
- `src/brigade/fleet_hub_messages.py` admin `grants` action and
  `src/brigade/cli/fleet_messages.py`: the action accepts a node id for an
  enrolled node and refuses an unknown or revoked one, and `fleet msg
  principals grants` gains a planned `--node <id>` option.
- `_station_read`: teammates get `principal_role_forbidden` on `claims` and a
  reduced `who`.
- `src/brigade/fleet_hub_events.py`: teammates subscribe only to their own
  `message.created`.
- `src/brigade/fleet_hub_http.py`: `GET /audit` (admin).

Tests (`tests/test_fleet_hub_teammates.py`, extending
`tests/test_fleet_message_grants.py`):
- A teammate cannot read claims, subscribe to `claim.expired` or `run.failed`,
  or message principals outside its grant.
- Reciprocity: agent A to teammate T succeeds only when A's grant names T and
  T's grant names A, and the same holds from T to A. A node principal cannot
  message a teammate that has not granted it.
- Node grants, both directions: with node N's grant naming T and T's grant
  naming N, N to T and T to N both succeed. Removing N's grant for T blocks
  the next send from N to T and from T to N, and so does removing T's grant
  for N. Revoking N deletes its grant rows and blocks both directions. A node
  with no grant rows still messages non-teammate principals as before.
- The admin `grants` action accepts an enrolled node id, refuses an unknown
  or revoked node id, and writes an audit row.
- Revocation: removing one side of a reciprocal grant blocks the next send in
  both directions. A grant revoked on another connection while a send waits on
  the write lock blocks that send.
- A principal id with an unknown prefix gets agent restrictions.
- Audit rows exist for grant changes and revocation and contain no token.

Acceptance: hub-side half of #2006's "a teammate principal cannot read claims,
submit work or message principals outside its grant".

### Slice 9: hub invites, multi-use redemption and stored bindings (needs 3 and 8)

Files:
- `src/brigade/fleet_hub_invites.py` (new): `fleet_invites` and
  `fleet_invite_uses`, admin `create`, `list` and `revoke`, redemption under
  `BEGIN IMMEDIATE` with the `join_attempt_id` rule of design D9 (following
  enrollment in `fleet_hub_messages.py:445-505`), constant-time digest
  comparison, one generic refusal, rate limits, principal creation with role,
  grants and client-supplied token digest, the reciprocal grant pair with the
  inviting node or agent principal, and audit rows. Each `fleet_invite_uses`
  row stores `join_attempt_id`, `principal_id`, the token digest, the name,
  the full key record and the binding, and `list` returns them.
- `src/brigade/fleet_hub_http.py`: `POST /invites` (admin) and
  `POST /invites/redeem`. No join page.

Tests (`tests/test_fleet_hub_invites.py`):
- A one-use invite refuses a second attempt id. Expired and revoked invites
  refuse new attempts. A wrong secret fails like an unknown id.
- With `--max-uses 3`, three different attempt ids each succeed and create
  three principals, a fourth is refused, and a retry of any of the three
  admitted attempts still returns its own success without counting a use.
- The same attempt id with the same token, key record and name returns the
  same success without counting a use. The same attempt id with any value
  changed is refused.
- Retry after removal: an admitted attempt whose member was removed, retried
  with identical values, gets the generic refusal, its token stays revoked,
  and no principal is created or reactivated.
- Two concurrent redemptions of a one-use invite produce exactly one
  principal, and two concurrent new attempts against the last remaining use
  of a multi-use invite produce exactly one.
- Every use row keeps the exact key record and binding the attempt sent, and
  `invite list` returns them for every use.
- Redemption writes the reciprocal grants: the inviter's grant names the new
  teammate and the teammate's grant names the inviter, for a node inviter and
  for an agent inviter.
- The hub stores only `sha256(redeem_key)`, the token digests, the key
  records and the bindings.
- `GET /join` returns 404.
- Removing a member makes its token return 401.

Acceptance: #2006's first two acceptance bullets hold against the Python hub.

### Slice 10: invite and join client (needs 2, 2b, 5, 6 and 9)

Files:
- `src/brigade/fleet_mesh_invites.py` (new): `bfi1` token build and parse,
  HKDF derivation, the persisted join attempt, the inviter's invite record
  holding `bind_key` until the invite can produce no unprocessed use,
  per-use `pending_binding` handling, binding verification against each use's
  stored key record, `binding_mismatch` and `binding_missing`.
- `src/brigade/cli/fleet_invites.py` (new): `fleet invite create | list |
  verify | revoke`, `fleet join --hub <url> --invite-file <path>` and
  `--invite-stdin`, `fleet members list | remove`, `fleet audit list`.
- `src/brigade/fleet_client_message_credentials.py`: a
  `write_message_token_file()` that mirrors the reader.

Tests (`tests/test_fleet_mesh_invites.py`):
- End to end: create, join, the joiner pins the inviter from the token, the
  inviter verifies the binding, and a sealed round trip works.
- Before the inviter verifies, sends to the new principal and mail from it are
  refused with `invite_binding_pending`.
- A hub that swaps the joiner's key is caught by the binding, and one that
  swaps the inviter's key is caught by the token fingerprint.
- A hub that serves no binding leaves the principal blocked as
  `binding_missing`, never `observed`.
- `bind_key` survives the invite's expiry until the binding is processed.
- Multi-use: with `--max-uses 3`, the inviter verifies the binding of the
  first use, `bind_key` is still present, and the second and third uses
  verify later. `bind_key` is deleted only after every use is processed and
  no use can remain.
- The inviter pins exactly the kid in the stored use row. If the hub later
  serves a different key for that principal, it is `changed`.
- Third station: after the inviter verifies T, station C still sees T as
  `unverified` until it accepts an operator-signed key manifest listing T's
  fingerprint.
- A node inviter (`invite create --as <node id>`) and the teammate exchange
  sealed mail in both directions after the binding verifies.
- A token whose hub id does not match the verified identity statement is
  refused before redemption.
- Response loss: the hub stores the redemption and the client sees a timeout.
  Rerunning the command installs the credential. Crash after redemption and
  before token install: restarting from the persisted attempt installs the
  same credential.
- `fleet join` refuses a token passed as a positional argument, and the token
  never appears in logs or error text.
- `invite create --as` a principal whose key is not local is refused.
- Membership floor: a token with `m<seq>-<digest>` makes the joiner refuse a
  hub-served manifest below that `seq` or with a different digest at it, and
  a token without the field joins as before.
- A `--bootstrap` token joins a fresh hub, and the next invite comes from the
  joined machine.
- A pre-mesh principal listed in the membership manifest is `observed`, and
  an unlisted one is `unverified`, until `keys accept` with its
  fingerprint.

Acceptance: #2006's third bullet ("a teammate's sealed mail cannot be read by
the hub") holds, using the slice 6 database dump with a teammate sender.

## #2008: Hub Lite

### Slice 11: hub contract suite (needs 4, 7b and 9, waits for #1939, #1951, #2009 and #2010)

Files:
- `tests/contract/fleet_hub_contract.py` (new): a backend-neutral HTTP suite
  that takes a base URL, an admin token and a storage dump callback. It covers
  identity, signed statement upload and `manifests_get`, invites with
  multi-use redemption, retries and stored bindings, node and agent grants,
  `who`, sealed `send`, `list` with `wait_ms`, `count`, `ack`, `reply`,
  `sent`, `sent_lookup`, key publish and revoke, role restrictions, the
  envelope cap, admission errors, and the no-plaintext storage check, with
  two nodes. The signed `capabilities` of each backend's identity statement
  select the question section: the full hub must run the D7 question flow
  from slice 7b, and Hub Lite must answer every `question_*` action and
  `/claims` with 404.
- `tests/test_fleet_hub_contract_python.py` (new): runs it against the Python
  hub.
- `scripts/hub-contract` (new): runs it against a URL.

Acceptance: the suite passes on the Python hub.

### Slice 12a: Hub Lite skeleton under workerd (needs 11, G4 approved)

Files under `workers/hub-lite/`:
- `wrangler.toml` with one SQLite-backed Durable Object class,
  `workers_dev = true`, a `HUB_ADMIN_TOKEN` secret and `.dev.vars.example`.
- `package.json` pinning `wrangler` as a development dependency, and the
  committed lockfile (G4).
- `src/index.js` (routing, request limits, 404 for every route outside design
  D12) and `src/fleet.js` (the Durable Object: schema, token hashing,
  principals, identity statement storage, `GET /mesh/identity`, audit).
- `test/*.test.js` on `node:test` with a `node:sqlite` fake for fast checks.
- `.github/workflows/ci.yml`: a `workers` job for `workers/**` that runs
  `node --test`, then starts `wrangler dev` and runs the contract suite's
  identity and auth sections against it. It also runs
  `workers/github-webhook-relay` tests.

Acceptance: the identity and auth contract sections pass against
`wrangler dev` in CI.

### Slice 12b: Hub Lite keys and invites (needs 12a)

Files: `workers/hub-lite/src/fleet.js` and tests. `keys_publish`, `keys_get`,
key state in `who`, the `statements` table with `POST /mesh/statements` and
`manifests_get`, invites, `invite_uses` with `principal_id`, the full key
record and the binding, multi-use redemption with idempotent retries, refusal
of retries after member removal, node and agent grants, and the reciprocal
grant pair at redemption.

Acceptance: the key, statement and invite contract sections, including the
multi-use, retry and removal cases, pass against `wrangler dev`.

### Slice 12c: Hub Lite messages, admission and retention (needs 12b, waits for #2009)

Files: `workers/hub-lite/src/fleet.js` and tests. Sealed-only `send` and
`reply`, the 32 KiB cap, `list` with waiters capped at 20 s, `count`, `ack`,
`sent`, `sent_lookup`, tombstones, the alarm that purges ciphertext at the
earlier of ack plus 7 days and creation plus 14 days, and the admission
ceilings with `hub_storage_full`, `sender_storage_quota` and
`hub_daily_quota`. If the D8 retry-receipt proposal is adopted, retain its
bounded 90-day metadata after visible 30-day tombstone deletion and include
receipt bytes in every quota. Otherwise document the explicitly approved
shorter retry/fail-closed horizon.

Tests: the messaging contract sections, a storage dump with no plaintext
marker, a send without an envelope refused, a 32 KiB plus one byte envelope
refused, a 20 KiB padded payload accepted, each ceiling refusing before the
next write, the purge alarm leaving a verifiable header, and every
`question_*` action returning 404 (the client maps it to
`hub_feature_unavailable`).

Acceptance: the messaging contract sections pass against `wrangler dev`.

### Slice 13: Hub Lite presence and client throttles (needs 12c)

Files: `workers/hub-lite/src/fleet.js`, `src/brigade/fleet_session_presence.py`
and `src/brigade/fleet_inbox_delivery.py`, with tests.
- `POST /sessions` and `GET /sessions`, heartbeats in memory, rows written only
  on state, repository or branch change or once per third of the TTL.
- Client throttles applied when the signed identity statement reports
  `backend: "hub-lite"`: one
  heartbeat per session per 60 seconds unless state changes, and a 60 second
  delivery poll.

Tests: a heartbeat storm writes a bounded number of rows, and the client
throttles hold under a burst of tool events.

Acceptance: the presence contract section passes against `wrangler dev`.

### Slice 14: deploy template, limits docs and live measurements (needs 10 and 13)

Files:
- `workers/hub-lite/README.md`: the deploy button, the `npx wrangler deploy`
  fallback, identity upload and bootstrap invite, the free plan limits and
  admission ceilings with what happens at each, one Hub Lite per account,
  optional Cloudflare Access for admin routes, and the claims deferral.

Operator step, recorded in the pull request: deploy to a fresh Free plan
account with the button, confirm the Durable Object migration applied, run
`scripts/hub-contract` with two nodes, and record rows written per message,
requests per waiting station, and the per-object storage limit. The design's
D12 figures, including the 1 GB assumption and the 800 MB ceiling, are
replaced by the measured numbers.

Acceptance: #2008's three acceptance bullets for messaging, invites and
presence. Claims are out of scope (decision Q7) and get their own issue after
this slice merges.

## #2007: no-hub mode

### Slice 15: spike (G2)

Files: `engines/mesh/` (new Rust crate, iroh 1.x) with a minimal binary that
opens an endpoint on ALPN `brigade-mesh/1` and exchanges one length-prefixed
frame. No Brigade integration.

Thresholds, fixed before measuring: stripped release binary at most 20 MB per
platform, endpoint ready within 2 seconds of a cold start, and 10 of 10
relayed connections between two machines on different networks through a
self-hosted `iroh-relay`. The direct-path rate and behavior with
`--relay none` on a LAN and a tailnet are recorded without a threshold.

Acceptance: a go or no-go recorded in design D10. On no-go, D10 is rewritten
to the fallback (LAN and tailnet only, Hub Lite for the rest) before any later
no-hub slice starts.

### Slice 15b: no-hub wire protocol (needs 15 with a go)

Files: `docs/phase-agent-mesh-wire.md` (new). Frame format and size limits,
transport receipts and signed application acks, outbox retry and backoff with
idempotency on `(from, client_id)`, per-contact admission (frame size, rate,
stored bytes), inbox caps and retention matching design D8, contact
revocation, and the `bmi1` invite exchange.

Acceptance: reviewed and merged before slice 17 or 18a starts.

### Slice 16: backend interface (waits for #1939, #1951, #2009 and #2010)

Files:
- `src/brigade/fleet_backend.py` (new): a `MessageBackend` protocol covering
  all eight tools, including `ask_user` and `count`, and `HubBackend` wrapping
  the existing client functions. The protocol has a `capabilities()` method
  that reads the signed `capabilities` of the accepted identity statement
  (design D4), so Hub Lite answers `ask_user` and `claims` with
  `hub_feature_unavailable` without a network call.
- `src/brigade/fleet_mcp.py`, `src/brigade/fleet_inbox_delivery.py`,
  `src/brigade/fleet_ask_user.py` and `src/brigade/cli/fleet_messages.py`:
  call through the backend.

Tests: the existing MCP and delivery tests pass unchanged, `tool_specs()` is
identical for every backend and lists eight tools including
`fleet_ask_user`, and against a Hub Lite identity `fleet_ask_user` and
`fleet_claims` return `{"code": "hub_feature_unavailable", "permanent":
true}` with zero requests. A full hub statement without `claims` in
`capabilities` makes `fleet_claims` return the same code.

Acceptance: pure refactor.

### Slice 17: local mesh store and backend (needs 1, 2, 5, 6, 15b and 16, waits for #1951, #2009 and #2010)

Files:
- `src/brigade/fleet_mesh_store.py` (new): SQLite inbox, outbox, contacts,
  delivery receipts, the seen-id cache and pending requests in WAL mode, with the
  admission and storage limits of slice 15b.
- `src/brigade/fleet_mesh_backend.py` (new): `MeshBackend` per design D11,
  reusing `open_rows()` and `fleet_mesh_send`.
- `src/brigade/cli/fleet_mesh.py` (new): `mesh init --relay
  <url|n0-public|none>` with no default, `mesh status`, contacts, and `bmi1`
  tokens through `fleet join`.
- `fleet.mode` configuration and selection in `brigade fleet mcp`.

Tests (`tests/test_fleet_mesh_backend.py`), with a fake engine between two
temporary databases:
- Two nodes exchange sealed messages with no hub, through the same MCP calls
  the hub tests use, and the D6 replay and reply-linking tests pass against
  the mesh backend.
- An offline peer receives queued mail on reconnect, and the outbox drops
  entries after 7 days.
- `mesh init` without `--relay` refuses.
- `fleet_claims` and `fleet_ask_user` return `hub_feature_unavailable`.
- No fallback: with `fleet.mode = "mesh"` and a reachable fake hub URL and
  token also configured, all eight tools make zero requests to the hub, and
  `fleet_ask_user` and `fleet_claims` still return
  `hub_feature_unavailable`.

Acceptance: #2007's "the same MCP tool schemas work in hub and no-hub mode"
holds against the fake engine.

### Slice 18a: engine protocol crate (needs 15 and 17)

Files: `engines/mesh/src/proto/` and `.github/workflows/ci.yml` (cargo check,
clippy and test for `engines/mesh/**`). Framing, size limits, receipt types
and admission limits from slice 15b, with no networking loop.

Tests: Rust unit tests for framing, oversize frames, malformed receipts and
admission refusals.

### Slice 18b: engine daemon (needs 18a)

Files: `engines/mesh/src/daemon/`. Reads the outbox, sends frames, writes
received envelopes to the inbox, returns receipts, honors the relay setting
and exposes a status file. It never decrypts.

Tests: an integration test in CI starts `iroh-relay --dev`, runs two daemons
that can reach each other only through it, and exchanges a sealed message. A
capture at the relay contains no plaintext marker. Killing a daemon mid-send
and restarting it delivers exactly once.

### Slice 18c: Linux service, setup and doctor (needs 18b)

Files: a systemd user unit template, `brigade setup` installing the engine
like the code-graph engine, `brigade doctor` `WARN` or `MANUAL` checks for a
missing engine, a stopped daemon and an unset relay choice, and
`docs/fleet-no-hub.md` (setup, relay choices and what each exposes, and the
plain statement that claims, runs, patrol, dispatch and MCP Events need a
hub).

Acceptance: #2007's "two nodes on different networks exchange sealed messages
with no hub configured, and a relay cannot read them", shown by the slice 18b
relay test and one manual run across two real networks recorded in the pull
request.

### Slice 18d: macOS service (needs 18c)

Files: a launchd agent template, setup and doctor support. Tests: template
rendering and doctor checks.

### Slice 18e: Windows service (needs 18c)

Files: a scheduled task template, setup and doctor support. Tests: template
rendering and doctor checks.

### Slice 19: anchor mode (optional, needs 18b)

Files: `engines/mesh/` and `src/brigade/fleet_mesh_store.py`. An always-on
node started with `--anchor` holds sealed envelopes for contacts that opted in
and forwards them when the recipient connects, storing ciphertext and routing
ids only, with the same 7 day limit and the slice 15b admission limits.

Tests: delivery succeeds when sender and recipient are never online at the
same time, and the anchor's database contains no plaintext marker.

## Deferred

- Hub Lite claims (decision Q7): after slice 14, as a new issue.
- OS separation of keys and pins from agents (design D3).
- The `bm2` suite with ML-DSA signatures (design D2).
