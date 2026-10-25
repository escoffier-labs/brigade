# Security audit closeout: 2026-08-19

[Issue #1006](https://github.com/escoffier-labs/brigade/issues/1006) tracked
9 confirmed findings and 1 suspected finding from the August 19 defensive pass.
Its [split-work comment](https://github.com/escoffier-labs/brigade/issues/1006#issuecomment-5345228164)
assigned the fixes to #957, #998, and #1007 through #1014, with coverage follow-up
in #1015. Those child issues are closed. Their closure alone does not establish
that every attack in the tracker was addressed.

## Shipped fixes and remaining scope

| Finding | Child issue | Shipped change or disposition |
| --- | --- | --- |
| 1. Same-UID authority-store forgery | #957 | [#1048](https://github.com/escoffier-labs/brigade/pull/1048) added the capability broker and HMAC tier. [#1078](https://github.com/escoffier-labs/brigade/pull/1078) added opt-in external-key signing and sticky downgrade protection. The true same-UID boundary remains in #1093. |
| 2. Ineligible evidence bodies on non-show surfaces | #1007 | [#1019](https://github.com/escoffier-labs/brigade/pull/1019) centralized eligibility across search, MCP, bundles, and export. |
| 3. Replacement text admitted with a stale envelope | #1008 | [#1023](https://github.com/escoffier-labs/brigade/pull/1023) requires a valid envelope and exact content digest before promotion. |
| 4. Malformed envelopes and noncanonical authorization values | #1009 | [#1019](https://github.com/escoffier-labs/brigade/pull/1019) blocks parse errors, invalid digests, and non-clean states. |
| 5. Cross-run message transplant | #1010 | [#1018](https://github.com/escoffier-labs/brigade/pull/1018) binds run, message, assignment, and seats, including fallback and resume. |
| 6. Generic file ingest uses workspace redaction | #998 | [#1186](https://github.com/escoffier-labs/brigade/pull/1186) closed other redaction gaps. Non-context JSONL imports still needed the external origin described below. |
| 7. Vault symlink scope swap | #1011 | [#1027](https://github.com/escoffier-labs/brigade/pull/1027) uses no-follow directory traversal for index and reread. |
| 8. Unbounded worker output capture | #1012 | [#1108](https://github.com/escoffier-labs/brigade/pull/1108) bounds capture and persistence and terminates the process group on overflow. Escaped descendants remain a separate containment concern. |
| 9. Projection parent-swap race | #1013 | [#1026](https://github.com/escoffier-labs/brigade/pull/1026) confirmed the suspected race and holds parent descriptors through publication. |
| 10. Hook stdin allocated before its timeout | #1014 | [#1020](https://github.com/escoffier-labs/brigade/pull/1020) bounds stdin and nested payloads before worker creation. |

## Generic JSONL ingestion

`work import ingest` accepts a file from an arbitrary path and stamps its rows
as `learning-loop`. That importer identity previously selected the workspace
origin, which scans for secrets but leaves email addresses and infrastructure
values in non-context import bodies. Context rows already selected an
external origin.

`learning-loop` now selects `external-web` for every import kind. Redaction and
the provenance envelope record the same external origin before persistence.
The importer source remains `learning-loop`, and the claimed source remains
non-authoritative `declared_source` metadata. Existing stored rows are not
rewritten. The local learning-summary importer also uses `learning-loop` and
receives the external tier. Other producer mappings stay unchanged.

The redaction first covered only the import's `text` field.
[#1712](https://github.com/escoffier-labs/brigade/issues/1712) extends it to
`learning-loop` rows' `acceptance` entries and every string key and value in
the allowlisted metadata, nested ones included, at the same origin. Dedupe identity keys such as
`source_fingerprint` and `source_item_key` keep their raw values, so re-import
dedupe still matches. The field verdict is stored as `metadata.field_redaction`,
next to the provenance envelope. The envelope's `redaction` record still
describes only the item text, which the promote gate checks. A failed field
redaction quarantines the row. The regression ingests personal and
infrastructure values in both fields, then checks the stored inbox and the
promoted task.

The regression exercises the public import boundary with arbitrary-path JSONL,
a claimed local producer, and planted sensitive values. It checks the stored
inbox and returned JSON as well as a safe text control.

## Accepted residuals and follow-up audits

The [operator decision on #957](https://github.com/escoffier-labs/brigade/issues/957#issuecomment-5381056139)
selected opt-in hardening with a user-level sticky marker. A process that can
read the signing key can forge its MAC. A process that can write the marker
directory can remove the downgrade anchor. Closing the original tracker does
not close this attack class. [#1093](https://github.com/escoffier-labs/brigade/issues/1093)
tracks OS-mediated keys or a separate-UID authority service outside the 0.28.0
scope. [SECURITY.md](../SECURITY.md#directory-authority-store-hmac-opt-in)
documents the default trust boundary.

Process-group cleanup does not contain a descendant that escapes its group.
[#1215](https://github.com/escoffier-labs/brigade/issues/1215) owns that containment
follow-up and the remaining inbox-lock findings. This closeout does not alter
those surfaces.

The [#1015 coverage closeout](https://github.com/escoffier-labs/brigade/issues/1015#issuecomment-5464632234)
records the later read-only passes and routes their findings to separate issues.
These linked reports and merged PRs are historical evidence. This document does
not claim a new full defensive audit of the current source tree.
