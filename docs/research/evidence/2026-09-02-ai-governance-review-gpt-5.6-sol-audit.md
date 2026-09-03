BLUF: None of the five issues, exactly as written, produces sufficient stand-alone audit evidence. #1405 has the highest control value, but a narrowed #1404 must land first because #1405 depends on trustworthy identity and signing. Very likely, confidence High - the present #1404 signing design is not cosign-compatible, and #1405 compares labels rather than controlling principals.

Alternative: Build #1405 first only if an external IdP/KMS already authenticates human approvals and binds them to the exact commit.

Next: Rewrite #1404 and #1405 as one trust model, then implement the minimal #1404 substrate.

This applies the current [ISO/IEC 42001:2023](https://www.iso.org/standard/42001), [AICPA Trust Services Criteria, revised points of focus 2022](https://www.aicpa-cima.com/resources/download/2017-trust-services-criteria-with-revised-points-of-focus-2022), and the [EU AI Act consolidated with its 2026 amendment](https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX%3A32026R1744). ISO/IEC 42006 accredits certification bodies, not individual auditors. It governs audit competence and consistency, not receipt formats. [ISO published it on 2025-07-07](https://www.iso.org/standard/42006?browse=tc).

## Scope qualification

The AI Act citations are conditional. Articles 12, 14, 17, 19, and 26 concern high-risk AI systems. A general coding agent is not high-risk merely because it edits software. The company must first determine whether the agent is part of, or substantially modifies, an Annex I or Annex III high-risk system and whether the company is provider, deployer, or both.

Articles 12 and 14 are primarily provider design obligations. Article 17 is a provider QMS duty. Article 26(2) requires deployers to assign competent, trained, authorised natural persons to oversight, while Article 26(6) imposes the six-month log floor. The 2026 Omnibus moved Annex III obligations to 2027-12-02 and Annex I obligations to 2028-08-02. [Regulation (EU) 2026/1744, adopted 2026-07-08](https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX%3A32026R1744).

For SOC 2 Type II, every artifact also needs population completeness. I would reconcile Brigade runs against the complete forge, CI, deployment, and change-ticket populations before sampling receipts.

## #1404: Public-key signing and in-toto export

**Acceptance:** No, not as specified. After correction, it would be useful evidence of integrity, attribution, and test execution under ISO 42001 §§7.5.2-7.5.3, 9.1 and Annex A.6.2.8; SOC 2 CC7.2 and CC8.1; and, conditionally, AI Act Arts. 12, 19, and 26(6).

**What I would sample:** Receipts across the audit period, including every key rotation/revocation, failures, timeouts, rejected commands, tracked-manifest and ad-hoc runs, and signatures produced on different hosts. For each selection I would:

- Verify the envelope against the period-valid trust policy.
- Recompute the subject, receipt, log, manifest, and patch digests.
- Trace the subject to the forge commit and the test result to original logs.
- Confirm the signature existed contemporaneously, not just when exported for audit.

**Reject if:** The subject binding is null or mismatched; the key is untrusted, revoked, or ownerless; signed-byte canonicalisation is undefined; the signature postdates the change without trusted time evidence; the manifest or report is missing; or an ad-hoc test is presented as proof that the standard change control operated throughout the period.

Specific defects:

- `ssh-keygen -Y sign` produces an SSHSIG object. It does not produce the raw DSSE signature expected by cosign or typical in-toto libraries. DSSE signs `PAE(payloadType, payload)`, and its `keyid` is explicitly an unauthenticated hint. [DSSE envelope contract](https://github.com/secure-systems-lab/dsse/blob/master/envelope.proto), [OpenSSH `-Y sign` and `-Y verify`](https://man.openbsd.org/OpenBSD-7.6/ssh-keygen.1).
- Consequently, "`cosign verify-blob-attestation` accepts the DSSE envelope" will not pass merely because an SSHSIG blob was placed in `signatures[].sig`.
- Use `cosign attest-blob --statement` for the Sigstore profile. For SSH signing, define a separate Brigade SSHSIG profile and verifier. Do not describe the two as interoperable.
- Embedding `pubkey_signature` inside the receipt creates self-reference unless the signed bytes explicitly exclude the signature fields. Prefer a detached Statement/envelope that signs the immutable receipt digest.
- `pubkey_id` is insufficient. Add or externally bind signer principal, issuer/trust domain, algorithm, certificate chain or allowed-signers policy, key-validity interval, revocation source, trust-policy version, and trusted signing time.
- A bare public key proves possession, not employee identity, role, authorisation, or key custody.
- The in-toto Test Result predicate requires `result` and a `configuration` array of ResourceDescriptors. Its standard fields do not include per-command exit codes. Map commands to `passedTests`, `warnedTests`, and `failedTests`, and reference the complete Brigade receipt separately. [Test Result v0.1 specification](https://github.com/in-toto/attestation/blob/main/spec/predicates/test-result.md).
- A Reference predicate must include `attester.id`, plus `downloadLocation` and `mediaType` for every reference, not merely a digest. [Reference v0.1 specification](https://github.com/in-toto/attestation/blob/main/spec/predicates/reference.md).
- Each in-toto subject must have a digest. Git commits and trees should use the actual algorithm or semantic digest key, such as `gitCommit` and `gitTree`, rather than assuming SHA-256. [Statement v1 subject rules](https://github.com/in-toto/attestation/blob/main/spec/v1/statement.md).
- A Sigstore bundle using a Fulcio certificate needs a transparency-log signed entry time or RFC 3161 timestamp for verification after certificate expiry. [Sigstore bundle format](https://docs.sigstore.dev/about/bundle/).
- Pin patched cosign versions. A legacy-bundle identity-verification bypass was fixed in cosign 2.6.5 and 3.1.3 on 2026-08-06. [GHSA-fx35-mq7g-6g98](https://github.com/sigstore/cosign/security/advisories/GHSA-fx35-mq7g-6g98).

**DSSE without a transparency log:** Acceptable as supporting evidence when keys are managed independently, trust roots and revocations are controlled, timestamps are corroborated, and the envelope is stored in an external immutable repository. It does not prove historical existence or prevent signer equivocation. Sigstore keyless plus Rekor is easier for cross-company verification because Fulcio binds a short-lived key to OIDC identity and Rekor records signing time. [Sigstore keyless overview](https://docs.sigstore.dev/cosign/signing/overview/). A private SCITT transparency service is a reasonable alternative where public metadata disclosure is unacceptable.

**Ad-hoc receipts:** Acceptable for auditor-directed reperformance or corroboration of a selected change. They are not acceptable as primary Type II operating evidence because the test criteria were not fixed in a tracked manifest before execution. The supplied schema correctly calls them audit-only and non-scoreable.

## #1405: Human approval event

**Acceptance:** No, not with the proposed identity fields. A corrected event would be the strongest evidence among the five for ISO 42001 §§5.3, 7.2, 8.1 and Annex A.3.2/A.9; SOC 2 CC6.1-CC6.3 and CC8.1; and, conditionally, AI Act Arts. 14(3)-(4) and 26(2).

The assertion that every cited regime requires an approver different from the author is too broad:

- SOC 2 CC8.1 requires changes to be authorised, documented, tested, approved, and implemented. It does not prescribe one universal separation model.
- AI Act Article 14 requires effective human oversight, including the ability to monitor, override, reverse, or stop. Its explicit two-natural-person rule is limited to specified remote biometric identification. [AI Act Art. 14 text](https://eur-lex.europa.eu/eli/reg/2024/1689/oj/eng).
- SOX is relevant only when the change affects controls over financial reporting.
- PCI DSS change approval does not automatically mean a different human for every change.

If company policy declares independent approval, a SOC 2 auditor will test that policy as written.

**What I would sample:** All deny, hold, override, revocation, and emergency approvals, plus representative allows across approvers, teams, repositories, and the entire period. I would compare the approval time with merge/deployment time, verify the approver’s directory role and competence at that time, inspect what evidence the approver received, and establish that the agent could not access the approval credential.

**Reject if:** The approver is a model seat; approval occurred after merge or deployment; the approval does not identify the exact commit/tree/patch; both credentials are controlled by one person or service account; the key is available to the agent; the approval is reusable or expired; or authority at the decision time cannot be proved.

Required changes:

- `approver_id != producing seat id` is not a segregation-of-duties test. Compare stable controlling principals, delegated subject, credential ownership, organisational role, and access paths.
- Reserve `approver_kind: human` for human oversight. A model review should be `reviewer` or `automated_check`, never a human approval substitute.
- Add: authenticated subject, issuer/trust domain, credential thumbprint, authentication method or user-presence claim, role and authority snapshot, competence reference, request/correlation ID, exact subject digest, policy/version, evidence reviewed, decision time, expiry, single-use nonce, revocation/override state, repository/ref/environment, and approval-before-action proof.
- Require independent key custody. A hardware-backed or IdP-bound human key is preferable. Agent workers must not inherit the approval key through environment variables, an SSH agent, filesystem access, or the same unrestricted vault role.
- If one operator controls both the agent key and approver key, two signatures still represent one control actor. For a small company, documented compensating monitoring may be defensible, but it should not report `SOD-OK`.
- Make approval applicability risk-based. Forcing `UNAPPROVED` on read-only and low-risk runs would generate noise and incentivise blanket approvals.
- Bind `reason` to structured reason codes and evidence references. Keep free text bounded and redact sensitive material.
- The supplied current contract already contains `approval_reference` and references to `approval.consumed` and daily approvals. I could not confirm from the supplied contract whether those are human-authenticated. #1405 must extend that model rather than create a conflicting approval state.

## #1406: Control crosswalk

**Acceptance:** Yes, but only as design documentation and an evidence index. It is not evidence that a control operated. It supports ISO 42001 §§4.3, 6.1, 7.5, 9.1, and 9.2, and may support an AI Act Article 17 QMS for an in-scope provider. Merely listing CC8.1 beside a receipt does not satisfy CC8.1.

**What I would sample:** Mappings from every framework version, several `full`, `partial`, and `none` outcomes, and all high-risk AI Act mappings. For each, I would trace the mapping to primary text and then to an actual run artifact. I would deliberately test false positives, such as an unsigned log being called complete evidence for log integrity.

**Reject if:** “Evidenced” means only that an artifact exists; applicability and organisational role are absent; the framework version is stale; a row has no mapping rationale; required receipt fields are missing; or the crosswalk cannot identify the control population and audit period.

Missing fields:

- Framework identifier, edition, publication/effective date, jurisdiction, and source locator.
- Obligation bearer: provider, deployer, service organisation, supplier, or customer.
- Applicability condition and system boundary.
- Mapping relationship: supports, partially supports, conflicts with, or no relationship.
- Control objective, expected procedure, required fields, evidence claim, collection cadence, owner, population query, test method, known limitations, reviewer, and review date.
- Versioned retention of the crosswalk used for each evidence package.

Over-engineering:

- “At least one row per receipt family per framework” invites invented mappings. Keep explicit `none`, but organise around control objectives and evidence claims rather than forcing every artifact into every framework.
- Do not reproduce licensed ISO or AICPA clause text in an open-source template without permission. Store identifiers, links, and Brigade’s own rationale.

The proposed OSCAL follow-up uses the wrong model:

- Crosswalks belong in OSCAL 1.2.2 **Control Mapping**.
- Brigade’s reusable control implementation statements belong in **Component Definition**.
- **Assessment Results** is appropriate only after an assessment was performed against a specific system, Assessment Plan, SSP, and control set. [NIST OSCAL layers and models, updated 2026-06-02](https://pages.nist.gov/OSCAL/learn/concepts/layer/), [Control Mapping model, updated 2026-01-29](https://pages.nist.gov/OSCAL/learn/concepts/layer/control/mapping/).

## #1407: Commit linkage, retention, and export

**Acceptance:** Mixed.

- Commit trailers are linkage evidence.
- JSONL exports can be the audit population if completeness is reconcilable.
- The retention control is not acceptable as designed because the same tool and administrative domain write, configure, prune, and attest to the logs.

Relevant requirements are ISO 42001 §7.5.3, §9.1, and Annex A.6.2.8; SOC 2 CC7.2 and CC8.1; and, conditionally, AI Act Arts. 19 and 26(6).

**What I would sample:** Commits to receipts in both directions, force-pushed or rebased history, earliest and latest period events, failed exports, every retention-policy change and prune, legal-hold cases, and a restore from the external archive.

**Reject if:** A trailer is dangling or rewriteable without detection; commits exist with no receipt; receipts exist with no reconciled commit disposition; the retention floor is editable by log writers; no external export acknowledgement exists; clocks cannot be trusted; legal hold is unsupported; or deletion occurs without independently retained proof.

Specific defects:

- There is a cryptographic dependency cycle if the commit contains the receipt digest while the receipt’s attestation subject contains that commit digest. Break it by putting only `Brigade-Run` or an immutable pre-commit intent digest in the commit, then issue the signed post-commit attestation referencing the commit. A forge check, Git note, OCI attachment, or transparency-log lookup can hold the post-commit receipt reference.
- A commit trailer is metadata, not protection. Branch protection, signed forge audit events, and external retention remain necessary.
- `minimum_days` in writable Brigade configuration is only a safety guard. Enforce retention in independent object storage or a SIEM with object lock/WORM, separate deletion IAM, legal hold, retention-lock audit events, and restore testing.
- `183` days does not always represent six calendar months. Use calendar-period semantics such as `P6M`, plus a safety margin, and allow longer contractual or statutory periods.
- A prune receipt must itself be signed and exported before deletion. If the same process can delete the record and its prune receipt, it is weak evidence.
- The current receipt contract already archives verify runs before count-based pruning and writes `brigade.verify_archive_index.v1`. The issue should extend that mechanism with time, external custody, and legal-hold controls.
- OTLP needs a defined OTLP Logs or Traces mapping, resource attributes, trace/span IDs, status conversion, privacy rules, and schema version. `invoke_agent` and `execute_tool` are operation values and prefixes for span names, not complete export schemas.
- As of 2026-09-02, the dedicated OpenTelemetry GenAI conventions, including agent and model conventions, are still marked Development. [OpenTelemetry GenAI status](https://github.com/open-telemetry/semantic-conventions-genai/blob/main/docs/gen-ai/README.md), [agent span definitions](https://github.com/open-telemetry/semantic-conventions-genai/blob/main/docs/gen-ai/gen-ai-agent-spans.md).

## #1408: Governance inventory

**Acceptance:** Yes as point-in-time evidence of Brigade’s configured subsystem. No as the company’s complete AI inventory. It may support ISO 42001 §4.3, Annex A.4.2, A.9 and A.10; SOC 2 CC6.1 and CC9.2; and provider technical documentation for an in-scope high-risk system.

**What I would sample:** Roster, `tools.toml`, MCP state, and provider configuration to output, followed by observed runs back to the inventory. I would also search forge, endpoint, API gateway, expense, and IdP records for agents and providers absent from Brigade.

**Reject if:** The owner, purpose, environment, lifecycle status, permissions, exact version, or system boundary is absent; provider retention claims have no source/as-of date; an observed model/tool is missing; the inventory is stale; or the self-digest cannot be reproduced.

Missing or incorrect properties:

- Add accountable owner, intended use, business process, environment, lifecycle status, risk classification, data categories, data residency, privilege scope, allowed resources/actions, provider/processor role, DPA/contract reference, last observed date, and configuration revision.
- Separate logical agent definitions, runtime workload identities, model calls, and the human or service principal that delegated authority.
- For provider retention settings, record tenant/account, source, retrieval timestamp, effective date, and `unknown` reason. “If known” without provenance is not audit evidence.
- The CLI has no stated time-window flags even though `agent-registry.json` includes runs “in the window.” Add `--since` and `--until`, or remove the run-population field.
- A document cannot normally include its own SHA-256 without defined exclusion/canonicalisation rules. Use a detached digest manifest or signed attestation.
- In CycloneDX, CLI binaries and local tools are components. Remote APIs and MCP endpoints may be services. Do not model every tool as a service. `modelCard` is permitted only for `machine-learning-model` components. [CycloneDX 1.7 model and tool semantics](https://cyclonedx.org/docs/1.7/proto/).
- Follow the official `$schema` form, `http://cyclonedx.org/schema/bom-1.7.schema.json`, plus `bomFormat`, `specVersion`, `serialNumber`, document `version`, stable `bom-ref` values, and dependencies/compositions. [CycloneDX AI model example](https://cyclonedx.org/use-cases/ai-models-and-model-cards/).

## Interoperability in 2026

“As-is” has two meanings here. Vanta, Drata, and Secureframe can store the files as manual attachments. They do not natively interpret Brigade predicates, validate DSSE, or infer control satisfaction.

| Tool | Brigade output accepted as-is | Exact required form |
|---|---|---|
| Vanta | All five as manual evidence only | Upload `.json` or `.zip`, maximum 50 MB, or attach a URL to an IRL request. Control mapping remains manual. [Vanta IRL file rules, accessed 2026-09-02](https://help.vanta.com/en/articles/12293647-managing-an-information-request-list-irl) |
| Drata | All five as evidence artifacts only | `POST /workspaces/{workspaceId}/evidence-files` using `multipart/form-data` `file` or a data-URL `base64File`; receive `fileKey`, then create/update the Evidence item and associate controls. JSON and ZIP are accepted. [Drata Evidence API, accessed 2026-09-02](https://developers.drata.com/openapi/reference/v2/tag/Evidence/) |
| Secureframe | All five as attachments to tests | Create a Custom Upload Test, map it to controls, then `POST /tests/{test_id}/evidences` with `multipart/form-data` `file` and optional `activity_completion`. It does not semantically import Brigade JSON. [Secureframe evidence endpoint, accessed 2026-09-02](https://support.secureframe.com/en/articles/15111235-faqs-tests-and-controls-evidence-frameworks-and-troubleshooting) |
| AuditBoard | No confirmed direct Brigade JSON or attestation import | The only public import I could confirm is AuditBoard Analytics flat-file import: CSV, XLS/XLSX, TSV, SAV, or HYPER. #1406/#1408 therefore need CSV/XLSX conversion or a custom integration. Evidence-attachment API support could not be confirmed from public primary documentation. [AuditBoard Analytics import formats](https://docs.auditboardanalytics.com/tools/import/import-file) |
| Dependency-Track | #1408 only, and only as CycloneDX | Dependency-Track 5.1.0 accepts CycloneDX 1.7 through its BOM ingestion path. Version 4.14 requires CycloneDX 1.6. It does not ingest approvals, control crosswalks, retention records, or custom in-toto predicates as audit evidence. Full `machine-learning-model`/model-card presentation should be integration-tested. [Dependency-Track 5.1.0, 2026-08-31](https://www.dependencytrack.org/news/dependency-track-5-1/) |
| GUAC | #1404 DSSE/in-toto and #1408 CycloneDX can be collected | Feed a standard DSSE envelope/in-toto Statement or valid CycloneDX document through GUAC’s filesystem/blob collectors. GUAC lists DSSE, in-toto ITE6, and CycloneDX ingestion as stable. I could not confirm semantic graph support for Test Result v0.1 or Brigade’s custom predicate, so collection must not be represented as policy interpretation. [GUAC supported formats](https://docs.guac.sh/guac/guac-components/) |
| OSCAL importers | None of the proposed Brigade JSON is OSCAL as-is | Emit valid OSCAL 1.2.2 JSON, XML, or YAML. Use `mapping-collection` for #1406, `component-definition` for reusable Brigade implementation claims, and `assessment-results` only for a specific assessment referencing an Assessment Plan and SSP. Importer support is product-specific. [OSCAL 1.2.2 model reference](https://pages.nist.gov/OSCAL-Reference/models/v1.2.2/) |

## Ranking and build order

Audit value per unit of effort, assuming the issues receive the corrections above:

1. **#1405 human approval** - highest control value; medium effort.
2. **#1408 governance inventory** - moderate value; low-to-medium effort and useful for scoping every other control.
3. **#1407 external export and retention** - high value, but the issue must be split and requires infrastructure outside Brigade.
4. **#1404 full signing/attestation scope** - useful integrity and interoperability, but high effort and over-scoped with incompatible signer profiles.
5. **#1406 crosswalk** - inexpensive documentation, but low direct evidence value and recurring maintenance cost.

Build a narrowed #1404 first as an enabling dependency:

- Detached in-toto Statement/DSSE.
- One signer profile, preferably cosign with an enterprise KMS or keyless Sigstore.
- Trust-policy file with issuer/subject or key/CA constraints.
- Offline verification and predicate-policy validation.
- No SSHSIG, minisign, SCITT, or multiple optional profiles in the first slice.

Then build #1405 immediately on that identity substrate.

## Predicate URI alignment

No published standard predicate fully represents an autonomous code change, its human delegation, test evidence, policy decision, and approval.

Keep `https://brigade.dev/attestation/agent-change/v1`, but align its parts with:

- **in-toto Statement v1 and DSSE** for the envelope.
- **in-toto Test Result v0.1** for standard test summaries.
- **in-toto SCAI v0.3** for attribute assertions and authenticated evidence references. It does not prescribe approval semantics. [SCAI v0.3](https://github.com/in-toto/attestation/blob/main/spec/predicates/scai.md).
- **draft-munoz-wimse-authorization-evidence-01** for the authorization subobject: authenticated agent, delegated subject, resource/tool, action, decision, timestamp/correlation, risk/posture, revocation, and a digest of the exact authorised request. It is an individual Internet-Draft with no IETF standing. [Draft revision, July 2026](https://datatracker.ietf.org/doc/draft-munoz-wimse-authorization-evidence/01/).
- **draft-klrc-aiagent-auth-03** for treating agents as workloads and preserving delegated-user context. It is also an individual draft. [Draft dated 2026-07-06](https://datatracker.ietf.org/doc/draft-klrc-aiagent-auth/).
- **RFC 9943 SCITT** or Sigstore/Rekor for registration and trusted time.
- **ISO/IEC FDIS 24970** for event triggers, log contents, storage, and access. It reached FDIS on 2026-08-28 but is not yet an International Standard and defines no predicate URI. [ISO project status](https://www.iso.org/cms/%20render/live/es/sites/isoorg/contents/data/standard/08/87/88723.html?browse=tc).

OWASP ACS, AAIF, and the NIST NCCoE agent-identity project do not currently publish an attestation predicate. ACS is a runtime-hook and policy-enforcement standard. The NCCoE document is a concept paper. Do not mint an OWASP, NIST, AAIF, or IETF URI for Brigade’s schema.

## `research.md` corrections

| Claim | Finding as of 2026-09-02 |
|---|---|
| “Auditors and GRC tools only consume public-key signatures” | Wrong. Auditors routinely accept unsigned system-generated records after testing relevance, reliability, completeness, accuracy, and custody. Vanta, Drata, and Secureframe accept JSON/ZIP attachments but do not verify Brigade signatures. |
| “Without public-key signatures, a receipt is a private log, not evidence” | Wrong. It is weaker evidence, particularly across trust domains, but can still be evidence. A signature also cannot establish completeness or truth. |
| Test Result directly carries per-command exit codes | Wrong. Test Result v0.1 has `result`, ResourceDescriptor `configuration`, optional URL, and named pass/warn/fail lists. No standard exit-code field. |
| #1404 makes receipts verifiable by `slsa-verifier` | Wrong for Test Result and the custom predicate. `slsa-verifier` verifies supported SLSA provenance and VSA formats, not arbitrary predicates. [slsa-verifier documentation](https://github.com/slsa-framework/slsa-verifier). |
| SCITT RFC 9943, “Oct 2025” | Wrong date. RFC 9943 was published in June 2026. [RFC Editor record](https://www.rfc-editor.org/info/rfc9943/). |
| OWASP 2026 LLM Top 10 and ACS text “not yet confirmed” | Out of date. OWASP published ACS on 2026-09-01 and announced the 2026 LLM Top 10 on 2026-09-02. [ACS](https://genai.owasp.org/resource/agent-control-standard-acs/), [release announcement](https://genai.owasp.org/2026/09/01/owasp-genai-security-project-unveils-2026-top-10-for-llm-applications-new-agent-control-standard-and-sponsors-as-community-tops-30000-members/). |
| Art. 14 and CC8.1 require a different approver in every case | Wrong. Independent approval may be the company’s chosen control, but those provisions do not impose that universal implementation. |
| Art. 26 requires six-month retention for Brigade generally | Overbroad. Article 26(6) applies to deployers of high-risk AI systems and only to automatically generated logs under their control. |
| `minimum_days=183` satisfies six months | Unsafe. Six calendar months can exceed 183 days. Use calendar-month semantics plus a margin. |
| OpenTelemetry client spans are stable and only agent spans are experimental | Out of date. The dedicated GenAI conventions currently label the overall set and agent spans Development. |
| Anthropic Compliance API excludes Claude Code inference activity | Out of date as a blanket claim. The March 2026 activity feed excluded inference events, but current documentation covers eligible Claude Code session transcripts. It still excludes several API-key, cloud-platform, web, zero-retention, and system-prompt cases. [Current Compliance API coverage](https://platform.claude.com/docs/en/manage-claude/compliance-api). |
| `draft-ni-wimse-ai-agent-identity` is current | Out of date by one day. Revision 02 expired on 2026-09-01. [IETF Datatracker status](https://datatracker.ietf.org/doc/draft-ni-wimse-ai-agent-identity/). |
| The current schema has no retention mechanism | Incomplete. `receipt-schemas.md` already specifies archive-before-prune with `brigade.verify_archive_index.v1`; what is missing is time-based, independently enforced retention. |
| Brigade has no approval representation | Incomplete or internally inconsistent. The supplied schema already defines `approval_reference` and mentions `approval.consumed` and daily approval flows. I could not confirm whether these carry a human-authenticated signature from the supplied documents alone. |
| CycloneDX tools should all be modelled as services | Wrong. CycloneDX 1.7 models software/hardware tools as components and network or intra-process services as services. |
| “Certificates are getting stricter” because of ISO 42006 | Unconfirmed and imprecise. ISO 42006 adds requirements for certification bodies and auditor competence. No primary source supports the claimed trend in certificate strictness. |
| GitLab Duo audit events are ordinary production evidence | Missing qualification. GitLab documents the feature as beta, and audit-event storage must be explicitly enabled before events are retained. [GitLab AI audit events, accessed 2026-09-02](https://docs.gitlab.com/user/duo_agent_platform/ai-audit-events/). |
| Credo AI publishes no portable attestation schema | I could not confirm this from public primary documentation. |
| Colorado SB 26-189 is “disclosure-only” | Oversimplified. It repeals and reenacts the prior provisions and includes consumer protections around consequential automated decisions, not merely a generic disclosure obligation. [Colorado Attorney General status page](https://coag.gov/ai/). |

## Decisions and open questions

- **Evidence:** Brigade can emit useful supporting evidence, but completeness must be reconciled against forge, CI, deployment, and ticket populations.
- **Judgment:** Identity-bound, pre-action human approval and external log custody provide more audit value than an extensive control crosswalk.
- **Judgment:** Keep the project-owned predicate URI until a standards body publishes a close semantic match.

Open questions:

- Is Brigade used in any Annex I or Annex III high-risk system?
- Is the company a deployer only, or does it place a modified high-risk system on the market?
- Which IdP/KMS is authoritative for human and workload identity?
- Which independent system will enforce retention and legal hold?
- Is the target integration manual GRC evidence attachment or machine-enforced supply-chain policy?