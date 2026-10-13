# Evidence identity and audit acceptance guide

This guide helps an adopting organization decide whether Brigade evidence is
acceptable for a particular review. It is an audit-readiness checklist, **not
an audit conclusion or product certification**. Brigade authenticates and
binds software artifacts; it does not discover the human behind a key, operate
the organization's identity lifecycle, choose an audit sample, or certify a
control.

An approval signature establishes that a permitted key made a principal
assertion over the signed subjects. It does not, by itself, establish who
controlled the key, whether that person was authorized at the relevant time,
whether custody was independent, or whether an organization's process
operated.

## Three acceptance owners

Keep these decisions separate in an evidence policy:

1. **Software evidence.** Brigade can bind a request, tree and patch identity,
   actual checks, approval fields, journal order, expiry, and configured
   key/principal assertions. Verification reports what those artifacts support
   and preserves invalid, failed, held, stale, expired, and indeterminate
   outcomes.
2. **Organizational process.** The adopter owns identity proofing, role and
   employment records, controlling-principal mapping, authorization,
   independent custody, trust-root administration, revocation, release
   procedure, retention, and archival verification.
3. **Auditor decision.** The assessor defines the population, period, sampling
   method, sample size, exceptions, sufficiency, and conclusion. No Brigade
   status makes that decision.

Do not infer that every standard mandates a distinct human approver or
immutable storage. SOC 2 scope and control operation belong to the adopting
organization and its CPA. ISO management-system scope belongs to the
organization. EU duties depend on the actual provider, deployer, manufacturer,
or steward role and intended use; this guide assumes neither a universal
high-risk classification nor a universal retention period.

## Organization-supplied identity history

Before accepting a signature as evidence of a human action, retain an
organization-controlled record that can answer the following questions as of
the signed event time:

| Required fact | Acceptance record |
| --- | --- |
| Controlling human | Stable synthetic employee/contractor subject mapped to the exact principal and key ID; record the mapping source, source digest, and source revision. This is the **controlling-principal mapping**. |
| Authorization | Role and permitted action with a half-open **authorized interval** (`valid_from`, `valid_until`) and the policy source/digest/revision that was effective then. |
| Custody | Whether requester, producer, approver, release operator, and trust-root administrator were independently controlled where the organization's policy requires that separation. |
| Trust roots | Who may amend allowed signers and identity mappings; use **independent trust-root administration** when the assessed policy requires it, and retain amendments outside the workspace being assessed. |
| Revocation | Key/principal status, revocation effective time, reason/reference, and the policy for events before and after revocation. |
| Archives | Retention period, protected location, deletion/legal-hold rules, algorithms/trust material retained, and the archival verification procedure used after rotation or revocation. |

The record should be append-only or independently protected to the degree the
adopter's policy requires; Brigade does not require or promise immutable
storage. Preserve an explicit `unknown` state rather than substituting a
current directory entry, guessing from a display name, or treating missing
history as “valid.” A current mapping cannot prove a historical mapping.

### Time has three different grades

- A **local declared time** is a timestamp written by a local process or
  included in signed content. Its integrity may be protected, but its clock
  correctness is not independently established.
- An **authenticated source assertion** proves that the identified source
  asserted a time, subject to the source's clock and custody controls.
- An **independently anchored time** is corroborated by an accepted external
  timestamp, transparency log, trusted receipt, or other organization-approved
  anchor whose custody and clock are independent of the destination.

A signed destination timestamp remains a local declared time or authenticated
source assertion; the signature does not independently establish clock
correctness. Define which grade is required for authorization intervals,
expiry, revocation, approval-before-release, and the assessment-period cut.
When the required grade is absent, report it as unknown, not as on time.

## Interpretation examples

All names and identifiers below are synthetic. They illustrate acceptance
decisions, not additional Brigade verification states.

| Case | Brigade observation | Organizational acceptance |
| --- | --- | --- |
| **Same human, two keys** | Requester `principal:alpha`/`key-a` and approver `principal:beta`/`key-b` can pass a comparison of distinct recorded identities. | The historical mapping says both were controlled by `person-example-1`; reject a policy requiring independent humans. Two keys or principals do not create another human. |
| **Key rotation** | `principal:reviewer` changes from `key-old` to `key-new`; each signature can verify under retained trust material. | Rotation preserves the mapped human unless identity history says otherwise. It improves key lifecycle hygiene, not human independence. Verify the event against the key and authorization interval effective at that time. |
| **Revoked key** | A cryptographically valid signature from `key-revoked` can still verify. | Apply the recorded revocation effective time and archival policy. Accepting a pre-revocation event may be allowed; a later event is rejected or unknown if trustworthy time ordering is unavailable. |
| **Expired approval** | Current strict verification reports `APPROVAL-EXPIRED` for an expired allow decision. | Do not use it to authorize a later release. Retain the expired result as evidence rather than deleting the case. |
| **Unknown requester** | Approval v2 segregation of duties is `INDETERMINATE` when verified requester identity is unavailable; selected verification can report `SOD-INDETERMINATE`. | Do not promote indeterminate to independent-human approval. Apply the adopter's reject, exception, or follow-up rule and retain that disposition. |
| **Missing historical policy** | The signed artifact may bind Brigade's approval policy while the adopter's historical role or identity policy is absent. | Report historical authorization as unknown. The current policy is not a substitute for the policy source/digest/revision effective at decision time. |
| **Shared-workspace signer** | A permitted signer can produce a valid assertion in a workspace visible to several operators. | Shared visibility and a signature do not establish independent custody. Reject independence claims unless external custody and controlling-human history support them. |

These cases complement, rather than replace, Brigade's existing exact-subject,
requester/approver identity, policy, ordering, and expiry checks. A failed
cryptographic or Brigade policy check cannot be repaired by favorable
organizational metadata.

## Evidence-packet sampling and rejection example

Define the population before sampling—for example, every attempted release in
an assessment period, not only successful releases. Retain terminal successes
and all **failed** and **interrupted** cases so selection cannot silently omit
exceptions. The assessor chooses the sample size, selection method, acceptable
exceptions, and audit conclusions.

For each selected synthetic release `release-example-7`, join without changing
the original artifacts:

1. the authenticated **request** and its verified requester identity (or an
   explicit unknown);
2. the baseline, final tree, and retained patch containing the **exact
   changes**;
3. the verify receipt and logs identifying the **actual checks**, argv, result,
   interruption, and relevant digests—not merely a planned checklist;
4. the signed **approval**, decision, reason binding, expiry, subjects, and
   strict verification outcome;
5. the human **review** record plus the mapping/policy source, digest, revision,
   authorization interval, custody conclusion, and exception disposition; and
6. the **release** or non-release record, linked commit/package identity and an
   accepted time anchor where policy requires one.

Reject or qualify a sampled packet when any required join is missing,
ambiguous, digest-mismatched, outside its authorized interval, expired before
release, revoked under the adopter's policy, based on unknown historical
policy, or unable to establish required independent human control. Record the
reason and keep the packet in the population. `brigade receipts verify
--strict-approvals`, `brigade run audit`, `brigade receipts verify-package`, and
the package/attestation verification commands inspect their respective
software boundaries; none supplies missing organizational facts.

## Fleet and storage boundary

Brigade's current fleet surfaces provide shared visibility, coordination, and
client intent within a trusted operational fleet. They do **not** promise
hostile-tenant isolation. A shared workspace signer, same-UID process, writable
local journal, or administrator of both evidence and trust configuration may
sit inside the same custody boundary even when the UI displays distinct
principals.

Threat-model the deployment and place required identity history, trust roots,
anchors, and archives across genuinely independent administrative boundaries.
Related security tracks [#1093](https://github.com/escoffier-labs/brigade/issues/1093),
[#1151](https://github.com/escoffier-labs/brigade/issues/1151),
[#1352](https://github.com/escoffier-labs/brigade/issues/1352), and
[#1213](https://github.com/escoffier-labs/brigade/issues/1213) provide context
for this boundary. Referencing them does not assert that every historical
defect described there persists; evaluate the deployed revision and
configuration.

Local manifests, journals, and digest chains are not independent anchors when
an actor can consistently rewrite both evidence and its local reference.
Exporting an [evidence package](evidence-package.md) checks copied-file
integrity, not trusted provenance or retention. The adopter must define
revocation and archival verification policy, retain the needed historical
trust material, and test restoration and verification for the chosen period.

## Minimum acceptance record

An assessment worksheet should therefore record, without coercing unknown to
false or true:

- artifact verification outcome and exact deployed Brigade revision;
- request, change, check, approval, review, and release joins;
- controlling-principal mapping source/digest/revision and effective interval;
- authorization policy source/digest/revision and effective interval;
- key status and revocation facts at the required time grade;
- custody and trust-root administrators, including the basis for independence;
- local, source-authenticated, and independently anchored timestamps used;
- retention/archive location and last successful archival verification; and
- assessor-owned population, sample method, exceptions, and conclusion.

An absent field stays `unknown`. That result can be useful evidence of a gap,
but it is not evidence that the corresponding control passed.
