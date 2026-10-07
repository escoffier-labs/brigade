# Brigade Control Crosswalk

Evidence index for configured Brigade artifacts. Not a compliance determination, not evidence that a control operated.

Evidence states are computed at query time by `brigade evidence controls` and are not part of this document.

Crosswalk version: 2. Schema: `brigade.control_crosswalk.v1`.

Relationship semantics: supports and partially-supports mean conditional corroboration of an outcome when the row's applicability condition holds. Neither means the control is fulfilled, and no Brigade artifact establishes that an organizational control operated. Scope and control operation belong to the adopting organization and its assessor.

Integrity boundary: Local receipts, journals, archive indexes and package manifests are ordinary files on a writable filesystem, not immutable records. They are tamper-evident only where a verifier recomputes a digest, hash chain or signature, and only relative to a trusted reference copy or key. A consistent rewrite of a whole file, including its recorded digests, is not detectable without a digest, signature or copy held outside that file. Custody, storage protection and retention periods belong to the adopting organization.

## CSA AI Controls Matrix (`csa-aicm-v1-1`)

Edition: v1.1. Source: https://cloudsecurityalliance.org/artifacts/ai-controls-matrix-v1-1
Note: Official CSA AICM v1.1 JSON/YAML/OSCAL bundle is the source to add later.

Not mapped in crosswalk version 2.

## Regulation (EU) 2024/1689 Artificial Intelligence Act (`eu-ai-act-2024-1689`)

Edition: as amended by Regulation (EU) 2026/1744, in force 2026-07-27. Source: https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:32024R1689
Note: Edition source verification: the European Commission notice at https://digital-strategy.ec.europa.eu/en/news/ai-omnibus-enters-force reports entry into force of the amending regulation, and the AI Act Service Desk reproduces consolidated Article 113 at https://ai-act-service-desk.ec.europa.eu/en/ai-act/article-113. The Official Journal text at https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=OJ:L_202601744 could not be read directly during review, so the edition line rests on those Commission sources. Rows do not quote amended clause text.

| Control | Claim | Relationship | Obligation | Applicability | Rationale | Source |
|---------|-------|--------------|------------|---------------|-----------|--------|
| `Art.11` | EC-07 | supports | provider | provider of a high-risk AI system | Technical documentation includes agent, model, and tool inventory. | https://artificialintelligenceact.eu/article/11/ |
| `Art.11` | EC-12 | supports | provider | provider of a high-risk AI system | Package export supplies an unsigned manifest. The evaluator checks entries-list digest self-consistency, not file contents; verify-package rehashes files. | https://artificialintelligenceact.eu/article/11/ |
| `Art.12` | EC-01 | supports | provider | provider of a high-risk AI system | Automatic event logging provides traceability of verification executions. | https://artificialintelligenceact.eu/article/12/ |
| `Art.12` | EC-02 | partially-supports | provider | provider of a high-risk AI system | Signed verification results support log integrity for high-risk systems. | https://artificialintelligenceact.eu/article/12/ |
| `Art.12` | EC-03 | partially-supports | provider | provider of a high-risk AI system | External verification supports log integrity for high-risk systems. | https://artificialintelligenceact.eu/article/12/ |
| `Art.12` | EC-06 | supports | provider | provider of a high-risk AI system | Automatic logging of events supports traceability. | https://artificialintelligenceact.eu/article/12/ |
| `Art.12` | EC-08 | supports | provider | provider of a high-risk AI system | Traceability through commit linkage to receipts. | https://artificialintelligenceact.eu/article/12/ |
| `Art.14` | EC-04 | partially-supports | provider | provider of a high-risk AI system | Human oversight can request and authorize changes before dispatch. | https://artificialintelligenceact.eu/article/14/ |
| `Art.14` | EC-05 | supports | provider | provider of a high-risk AI system | Competent human oversight and approval before changes proceed. | https://artificialintelligenceact.eu/article/14/ |
| `Art.17` | EC-10 | supports | provider | provider of a high-risk AI system | Quality management system records. | https://artificialintelligenceact.eu/article/17/ |
| `Art.26` | EC-11 | supports | deployer | deployer of a high-risk AI system under Art. 26 | Deployers keep logs under their control for six months. | https://artificialintelligenceact.eu/article/26/ |
| `Art.9` | EC-09 | no-relationship | provider | provider of a high-risk AI system | Risk management system records are outside Brigade evidence scope. | https://artificialintelligenceact.eu/article/9/ |

## ISO/IEC 27001:2022 (`iso-27001-2022`)

Edition: 2022. Source: https://www.iso.org/standard/27001
Clause text: unverified.
Note: Licensed clause text was not reviewed for this crosswalk. Control identifiers and rationales are unverified against the licensed text and are not reconstructed from secondary summaries.

| Control | Claim | Relationship | Obligation | Applicability | Rationale | Source |
|---------|-------|--------------|------------|---------------|-----------|--------|
| `A.5.9` | EC-07 | supports | service-organisation | any | Inventory of information assets. | https://www.iso.org/standard/27001 |
| `A.8.12` | EC-09 | supports | service-organisation | any | Guard audit scans content for leaks, not logs. | https://www.iso.org/standard/27001 |
| `A.8.15` | EC-02 | supports | service-organisation | any | Log protection is supported by signed verification results. | https://www.iso.org/standard/27001 |
| `A.8.15` | EC-03 | supports | service-organisation | any | Log protection supports external verification. | https://www.iso.org/standard/27001 |
| `A.8.15` | EC-06 | supports | service-organisation | any | Logging of lifecycle events. | https://www.iso.org/standard/27001 |
| `A.8.15` | EC-10 | supports | service-organisation | any | Log content protection for outcome records. | https://www.iso.org/standard/27001 |
| `A.8.15` | EC-11 | supports | service-organisation | any | Archive index records verification evidence before pruning; log protection and retention belong to the adopting organization storage controls. | https://www.iso.org/standard/27001 |
| `A.8.15` | EC-12 | supports | service-organisation | any | Package export supplies an unsigned manifest. The evaluator checks entries-list digest self-consistency, not file contents; verify-package rehashes files. | https://www.iso.org/standard/27001 |
| `A.8.32` | EC-01 | supports | service-organisation | any | Changes are tested and verified before implementation. | https://www.iso.org/standard/27001 |
| `A.8.32` | EC-04 | supports | service-organisation | any | Changes are authorized before implementation. | https://www.iso.org/standard/27001 |
| `A.8.32` | EC-05 | supports | service-organisation | any | Changes are approved by authorized personnel. | https://www.iso.org/standard/27001 |
| `A.8.32` | EC-08 | supports | service-organisation | any | Change management records linked to commits. | https://www.iso.org/standard/27001 |

## ISO/IEC 42001:2023 (`iso-42001-2023`)

Edition: 2023. Source: https://www.iso.org/standard/42001
Clause text: unverified.
Note: Licensed clause text was not reviewed for this crosswalk. Control identifiers and rationales are unverified against the licensed text and are not reconstructed from secondary summaries.

| Control | Claim | Relationship | Obligation | Applicability | Rationale | Source |
|---------|-------|--------------|------------|---------------|-----------|--------|
| `7.5` | EC-02 | partially-supports | provider | any | Signed Test Result attestation supports integrity of documented verification information. | https://www.iso.org/standard/42001 |
| `7.5` | EC-03 | partially-supports | provider | any | Cosign bundle supports external verification of documented information. | https://www.iso.org/standard/42001 |
| `7.5` | EC-08 | supports | deployer | any | Commit trailers link documented information to code changes. | https://www.iso.org/standard/42001 |
| `7.5` | EC-12 | supports | provider | any | Package export supplies an unsigned manifest. The evaluator checks entries-list digest self-consistency, not file contents; verify-package rehashes files. | https://www.iso.org/standard/42001 |
| `9.1` | EC-10 | supports | deployer | any | Monitoring and measurement evidence in outcome ledger. | https://www.iso.org/standard/42001 |
| `A.3.2` | EC-05 | supports | deployer | any | Segregation of duties for human approval of AI changes. | https://www.iso.org/standard/42001 |
| `A.4.2` | EC-07 | supports | deployer | any | Inventory of resources including agents, models, and tools. | https://www.iso.org/standard/42001 |
| `A.5` | EC-07 | no-relationship | deployer | any | no sourced sub-control identifier | https://www.iso.org/standard/42001 |
| `A.6.2.1` | EC-04 | supports | deployer | any | Signed change request supports AI system lifecycle control. | https://www.iso.org/standard/42001 |
| `A.6.2.8` | EC-01 | supports | service-organisation | any | Verify receipts record per-command exit status as local files. Edits are detectable only against a digest held outside the receipt, such as an attestation or manifest. | https://www.iso.org/standard/42001 |
| `A.6.2.8` | EC-06 | supports | deployer | any | Hash-chained lifecycle journal records events. It detects broken links; a consistent rewrite is undetectable without an external anchor. | https://www.iso.org/standard/42001 |
| `A.6.2.8` | EC-11 | supports | deployer | any | Archive index records verification evidence before pruning; preservation depends on the adopting organization storage and retention controls. | https://www.iso.org/standard/42001 |
| `A.7` | EC-09 | no-relationship | deployer | any | no sourced sub-control identifier | https://www.iso.org/standard/42001 |

## NIST AI 600-1 Generative AI Profile (`nist-ai-600-1`)

Edition: 1.0. Source: https://www.nist.gov/publications/artificial-intelligence-risk-management-framework-generative-artificial-intelligence
Note: NIST AI 600-1 action identifiers were not sourced from the final NIST AI 600-1 text, so no rows are assigned; related AI RMF subcategory mappings are retained under nist-ai-rmf-1.0.

Not mapped in crosswalk version 2.

## NIST AI Risk Management Framework (`nist-ai-rmf-1.0`)

Edition: NIST AI 100-1, AI RMF 1.0 (January 2023), final. Source: https://nvlpubs.nist.gov/nistpubs/ai/NIST.AI.100-1.pdf
Note: Rows cite the final NIST AI 100-1 Core tables (Section 5, Tables 1 to 4). The AI RMF Playbook is mutable and is not the normative source. Claims with no direct subcategory are listed as unmapped evidence properties; no control identifier is assigned to them.

| Control | Claim | Relationship | Obligation | Applicability | Rationale | Source |
|---------|-------|--------------|------------|---------------|-----------|--------|
| `GOVERN 1.6` | EC-07 | supports | deployer | The organization defines the inventory scope and the Brigade inventory covers the agents, models and tools within that scope. | A point-in-time inventory can corroborate an inventory mechanism within the defined scope. It does not collect external feedback, which is GOVERN 5.1. | https://nvlpubs.nist.gov/nistpubs/ai/NIST.AI.100-1.pdf Section 5.1, Table 1, GOVERN 1.6 |
| `GOVERN 2.1` | EC-04 | supports | deployer | Roles, responsibilities and lines of communication for mapping, measuring and managing AI risk are documented, and requester identities are bound to those roles. | A signed request can corroborate which documented identity requested a change. It does not integrate trustworthiness characteristics into policy, which is GOVERN 1.2. | https://nvlpubs.nist.gov/nistpubs/ai/NIST.AI.100-1.pdf Section 5.1, Table 1, GOVERN 2.1 |
| `GOVERN 2.1` | EC-05 | supports | deployer | Approver responsibilities and lines of communication are documented and understood, and approver identities are bound to that context. | An approval bound to the final tree with a passed segregation-of-duties check can corroborate that a documented approver role acted on a change. | https://nvlpubs.nist.gov/nistpubs/ai/NIST.AI.100-1.pdf Section 5.1, Table 1, GOVERN 2.1 |
| `MANAGE 4.1` | EC-06 | supports | deployer | The organization has implemented post-deployment monitoring or change-management plans and designates the journal as typed operational or change evidence under them. | A chain-valid journal can corroborate that run lifecycle events were recorded under such a plan. It does not document TEVV test sets, which is MEASURE 2.1. | https://nvlpubs.nist.gov/nistpubs/ai/NIST.AI.100-1.pdf Section 5.4, Table 4, MANAGE 4.1 |
| `MANAGE 4.1` | EC-08 | supports | deployer | The organization has an actual change-management or post-deployment procedure that requires commits to resolve to receipts. | Trailers that resolve to a matching receipt digest can corroborate traceability under that change procedure. They do not supersede, disengage or deactivate AI systems, which is MANAGE 2.4. | https://nvlpubs.nist.gov/nistpubs/ai/NIST.AI.100-1.pdf Section 5.4, Table 4, MANAGE 4.1 |
| `MANAGE 4.3` | EC-06 | partially-supports | deployer | The organization follows documented incident and error tracking processes that consume typed journal events. | Journal failure events can feed incident and error tracking. Communication to relevant AI actors and affected communities is outside the artifact. | https://nvlpubs.nist.gov/nistpubs/ai/NIST.AI.100-1.pdf Section 5.4, Table 4, MANAGE 4.3 |
| `MEASURE 1.1` | EC-01 | supports | deployer | The organization has selected measurement approaches and metrics for AI risks enumerated in MAP, prioritized the most significant risks, and documented risks it will not or cannot measure. | A completed receipt can corroborate that a selected check was executed with a recorded result. It does not select the approach or metric, and MEASURE 1.1 sets no numerical threshold. | https://nvlpubs.nist.gov/nistpubs/ai/NIST.AI.100-1.pdf Section 5.3, Table 3, MEASURE 1.1 |
| `MEASURE 1.1` | EC-10 | supports | deployer | The organization has selected measurement approaches and metrics for AI risks enumerated in MAP, prioritized the most significant risks, and documented risks it will not or cannot measure. | Outcome records can corroborate that selected measurements were recorded. They do not select approaches or metrics, and MEASURE 1.1 sets no numerical threshold. | https://nvlpubs.nist.gov/nistpubs/ai/NIST.AI.100-1.pdf Section 5.3, Table 3, MEASURE 1.1 |
| `MEASURE 2.1` | EC-01 | partially-supports | deployer | The executed commands are the organization's documented TEVV test sets, and metrics and tool details are documented outside the receipt. | Receipts can supply execution details for documented TEVV tooling. They do not document test sets or metrics. | https://nvlpubs.nist.gov/nistpubs/ai/NIST.AI.100-1.pdf Section 5.3, Table 3, MEASURE 2.1 |
| `MEASURE 2.1` | EC-02 | supports | deployer | The attested commands are documented TEVV test sets with documented metrics and tool details. | A signed, rederivable Test Result can corroborate which TEVV commands ran and their results. Adjudicated-feedback outcomes under GOVERN 5.2 are not addressed by a test signature. | https://nvlpubs.nist.gov/nistpubs/ai/NIST.AI.100-1.pdf Section 5.3, Table 3, MEASURE 2.1 |
| `MEASURE 2.10` | EC-09 | supports | deployer | Privacy risks were identified in MAP, and the organization documents its examination of those risks using guard verdicts. | A non-blocked guard verdict can corroborate one documented examination of mapped personal-data leakage risk. | https://nvlpubs.nist.gov/nistpubs/ai/NIST.AI.100-1.pdf Section 5.3, Table 3, MEASURE 2.10 |
| `MEASURE 2.7` | EC-09 | supports | deployer | Security and resilience risks were identified in MAP, and the organization documents its evaluation of those risks using guard verdicts. | A non-blocked guard verdict can corroborate one documented evaluation of mapped leakage risk. It does not identify TEVV considerations, which is MAP 2.3. | https://nvlpubs.nist.gov/nistpubs/ai/NIST.AI.100-1.pdf Section 5.3, Table 3, MEASURE 2.7 |
| `MEASURE 3.1` | EC-10 | partially-supports | deployer | The organization has documented approaches and personnel for regularly tracking existing, unanticipated and emergent AI risks, and uses outcome records within them. | Outcome records can corroborate documented risk tracking over time. | https://nvlpubs.nist.gov/nistpubs/ai/NIST.AI.100-1.pdf Section 5.3, Table 3, MEASURE 3.1 |

### Mapping provenance

- `GOVERN 1.6` / EC-07: corrected-mismatch
  - Supersedes: `GOVERN 5.1`
  - Edition: NIST AI 100-1, AI RMF 1.0 (January 2023), final
  - Primary locator: https://nvlpubs.nist.gov/nistpubs/ai/NIST.AI.100-1.pdf Section 5.1, Table 1, GOVERN 1.6
  - Artifact contract: brigade.governance_inventory.v1 point-in-time inventory from brigade governance inventory.
  - Responsible actor: Adopting organization, which defines inventory scope and resources it by risk priority.
  - Applicability: The organization defines the inventory scope and the Brigade inventory covers the agents, models and tools within that scope.
  - Support rationale: A point-in-time inventory can corroborate an inventory mechanism within the defined scope. It does not collect external feedback, which is GOVERN 5.1.
  - Verification limit: State checks that a parseable inventory exists, not its completeness, freshness or resourcing. A Brigade artifact does not establish that an organizational control or process operated.

- `GOVERN 2.1` / EC-04: corrected-mismatch
  - Supersedes: `GOVERN 1.2`
  - Edition: NIST AI 100-1, AI RMF 1.0 (January 2023), final
  - Primary locator: https://nvlpubs.nist.gov/nistpubs/ai/NIST.AI.100-1.pdf Section 5.1, Table 1, GOVERN 2.1
  - Artifact contract: agent-request/v1 envelope from brigade run --requester-key, verified with brigade receipts verify-attestation.
  - Responsible actor: Adopting organization, which assigns AI risk roles and binds requester keys to them.
  - Applicability: Roles, responsibilities and lines of communication for mapping, measuring and managing AI risk are documented, and requester identities are bound to those roles.
  - Support rationale: A signed request can corroborate which documented identity requested a change. It does not integrate trustworthiness characteristics into policy, which is GOVERN 1.2.
  - Verification limit: Verification proves possession of the requester key, not that the role assignment is documented or understood. A Brigade artifact does not establish that an organizational control or process operated.

- `GOVERN 2.1` / EC-05: conditional-support
  - Edition: NIST AI 100-1, AI RMF 1.0 (January 2023), final
  - Primary locator: https://nvlpubs.nist.gov/nistpubs/ai/NIST.AI.100-1.pdf Section 5.1, Table 1, GOVERN 2.1
  - Artifact contract: human-approval/v2 events from brigade run approve, verified with brigade receipts verify-attestation --strict-approvals.
  - Responsible actor: Adopting organization, which documents approver responsibilities and binds approver identities to them.
  - Applicability: Approver responsibilities and lines of communication are documented and understood, and approver identities are bound to that context.
  - Support rationale: An approval bound to the final tree with a passed segregation-of-duties check can corroborate that a documented approver role acted on a change.
  - Verification limit: The SOD check compares identities recorded in Brigade artifacts only. It does not show that responsibilities are documented or clear to staff. A Brigade artifact does not establish that an organizational control or process operated.

- `MANAGE 4.1` / EC-06: corrected-mismatch
  - Supersedes: `MEASURE 2.1`
  - Edition: NIST AI 100-1, AI RMF 1.0 (January 2023), final
  - Primary locator: https://nvlpubs.nist.gov/nistpubs/ai/NIST.AI.100-1.pdf Section 5.4, Table 4, MANAGE 4.1
  - Artifact contract: brigade.run_event.v1 lifecycle journal from brigade run, read with brigade run audit.
  - Responsible actor: Adopting organization, which owns its post-deployment monitoring, change and incident procedures.
  - Applicability: The organization has implemented post-deployment monitoring or change-management plans and designates the journal as typed operational or change evidence under them.
  - Support rationale: A chain-valid journal can corroborate that run lifecycle events were recorded under such a plan. It does not document TEVV test sets, which is MEASURE 2.1.
  - Verification limit: The hash chain detects edits relative to the recorded digests only. It does not prevent deletion, and it does not detect a consistent rewrite of the whole journal without a digest or copy held outside the file. A Brigade artifact does not establish that an organizational control or process operated.

- `MANAGE 4.1` / EC-08: corrected-mismatch
  - Supersedes: `MANAGE 2.4`
  - Edition: NIST AI 100-1, AI RMF 1.0 (January 2023), final
  - Primary locator: https://nvlpubs.nist.gov/nistpubs/ai/NIST.AI.100-1.pdf Section 5.4, Table 4, MANAGE 4.1
  - Artifact contract: Brigade-Run and Brigade-Receipt commit trailers from brigade receipts trailer, resolved with brigade receipts verify --commit.
  - Responsible actor: Adopting organization change-management owner.
  - Applicability: The organization has an actual change-management or post-deployment procedure that requires commits to resolve to receipts.
  - Support rationale: Trailers that resolve to a matching receipt digest can corroborate traceability under that change procedure. They do not supersede, disengage or deactivate AI systems, which is MANAGE 2.4.
  - Verification limit: State inspects the 20 most recent commits for a matching local receipt digest. A Brigade artifact does not establish that an organizational control or process operated.

- `MANAGE 4.3` / EC-06: corrected-mismatch
  - Supersedes: `MEASURE 2.1`
  - Edition: NIST AI 100-1, AI RMF 1.0 (January 2023), final
  - Primary locator: https://nvlpubs.nist.gov/nistpubs/ai/NIST.AI.100-1.pdf Section 5.4, Table 4, MANAGE 4.3
  - Artifact contract: brigade.run_event.v1 lifecycle journal from brigade run.
  - Responsible actor: Adopting organization incident owner.
  - Applicability: The organization follows documented incident and error tracking processes that consume typed journal events.
  - Support rationale: Journal failure events can feed incident and error tracking. Communication to relevant AI actors and affected communities is outside the artifact.
  - Verification limit: State checks chain validity only, not incident classification, response or communication. A Brigade artifact does not establish that an organizational control or process operated.

- `MEASURE 1.1` / EC-01: conditional-support
  - Edition: NIST AI 100-1, AI RMF 1.0 (January 2023), final
  - Primary locator: https://nvlpubs.nist.gov/nistpubs/ai/NIST.AI.100-1.pdf Section 5.3, Table 3, MEASURE 1.1
  - Artifact contract: brigade.work_verify_receipt from brigade work verify run: per-command argv and exit status.
  - Responsible actor: Adopting organization AI risk owner; Brigade only produces the artifact.
  - Applicability: The organization has selected measurement approaches and metrics for AI risks enumerated in MAP, prioritized the most significant risks, and documented risks it will not or cannot measure.
  - Support rationale: A completed receipt can corroborate that a selected check was executed with a recorded result. It does not select the approach or metric, and MEASURE 1.1 sets no numerical threshold.
  - Verification limit: State reflects receipt status and exit codes only. Brigade cannot judge whether the check measures a mapped risk. A Brigade artifact does not establish that an organizational control or process operated.

- `MEASURE 1.1` / EC-10: conditional-support
  - Edition: NIST AI 100-1, AI RMF 1.0 (January 2023), final
  - Primary locator: https://nvlpubs.nist.gov/nistpubs/ai/NIST.AI.100-1.pdf Section 5.3, Table 3, MEASURE 1.1
  - Artifact contract: brigade.outcome_record ledger from brigade outcome record.
  - Responsible actor: Adopting organization AI risk owner; Brigade only produces the artifact.
  - Applicability: The organization has selected measurement approaches and metrics for AI risks enumerated in MAP, prioritized the most significant risks, and documented risks it will not or cannot measure.
  - Support rationale: Outcome records can corroborate that selected measurements were recorded. They do not select approaches or metrics, and MEASURE 1.1 sets no numerical threshold.
  - Verification limit: State checks that non-empty records exist, not what they measure. A Brigade artifact does not establish that an organizational control or process operated.

- `MEASURE 2.1` / EC-01: conditional-support
  - Edition: NIST AI 100-1, AI RMF 1.0 (January 2023), final
  - Primary locator: https://nvlpubs.nist.gov/nistpubs/ai/NIST.AI.100-1.pdf Section 5.3, Table 3, MEASURE 2.1
  - Artifact contract: brigade.work_verify_receipt: recorded commands, exit status and captured logs.
  - Responsible actor: Adopting organization AI risk owner; Brigade only produces the artifact.
  - Applicability: The executed commands are the organization's documented TEVV test sets, and metrics and tool details are documented outside the receipt.
  - Support rationale: Receipts can supply execution details for documented TEVV tooling. They do not document test sets or metrics.
  - Verification limit: State does not inspect test-set or metric documentation. A Brigade artifact does not establish that an organizational control or process operated.

- `MEASURE 2.1` / EC-02: corrected-mismatch
  - Supersedes: `GOVERN 5.2`
  - Edition: NIST AI 100-1, AI RMF 1.0 (January 2023), final
  - Primary locator: https://nvlpubs.nist.gov/nistpubs/ai/NIST.AI.100-1.pdf Section 5.3, Table 3, MEASURE 2.1
  - Artifact contract: brigade.attestation.sshsig-dsse.v1 Test Result attestation from brigade receipts export attestation, rederived against its receipt.
  - Responsible actor: Adopting organization AI risk owner; Brigade only produces the artifact.
  - Applicability: The attested commands are documented TEVV test sets with documented metrics and tool details.
  - Support rationale: A signed, rederivable Test Result can corroborate which TEVV commands ran and their results. Adjudicated-feedback outcomes under GOVERN 5.2 are not addressed by a test signature.
  - Verification limit: Signature validity depends on the configured signer key and allowed-signers trust. State does not inspect TEVV documentation. A Brigade artifact does not establish that an organizational control or process operated.

- `MEASURE 2.10` / EC-09: corrected-mismatch
  - Supersedes: `MAP 2.3`
  - Edition: NIST AI 100-1, AI RMF 1.0 (January 2023), final
  - Primary locator: https://nvlpubs.nist.gov/nistpubs/ai/NIST.AI.100-1.pdf Section 5.3, Table 3, MEASURE 2.10
  - Artifact contract: brigade.guard.audit verdict from brigade guard audit.
  - Responsible actor: Adopting organization AI risk owner; Brigade only produces the artifact.
  - Applicability: Privacy risks were identified in MAP, and the organization documents its examination of those risks using guard verdicts.
  - Support rationale: A non-blocked guard verdict can corroborate one documented examination of mapped personal-data leakage risk.
  - Verification limit: Pattern-based redaction does not establish that privacy risk is examined in full. A Brigade artifact does not establish that an organizational control or process operated.

- `MEASURE 2.7` / EC-09: corrected-mismatch
  - Supersedes: `MAP 2.3`
  - Edition: NIST AI 100-1, AI RMF 1.0 (January 2023), final
  - Primary locator: https://nvlpubs.nist.gov/nistpubs/ai/NIST.AI.100-1.pdf Section 5.3, Table 3, MEASURE 2.7
  - Artifact contract: brigade.guard.audit verdict from brigade guard audit.
  - Responsible actor: Adopting organization AI risk owner; Brigade only produces the artifact.
  - Applicability: Security and resilience risks were identified in MAP, and the organization documents its evaluation of those risks using guard verdicts.
  - Support rationale: A non-blocked guard verdict can corroborate one documented evaluation of mapped leakage risk. It does not identify TEVV considerations, which is MAP 2.3.
  - Verification limit: State reflects the summary blocked flag in .brigade/work/guard/audit.json only. Guard rules cover configured patterns, not all security risk. A Brigade artifact does not establish that an organizational control or process operated.

- `MEASURE 3.1` / EC-10: conditional-support
  - Edition: NIST AI 100-1, AI RMF 1.0 (January 2023), final
  - Primary locator: https://nvlpubs.nist.gov/nistpubs/ai/NIST.AI.100-1.pdf Section 5.3, Table 3, MEASURE 3.1
  - Artifact contract: brigade.outcome_record ledger from brigade outcome record.
  - Responsible actor: Adopting organization AI risk owner; Brigade only produces the artifact.
  - Applicability: The organization has documented approaches and personnel for regularly tracking existing, unanticipated and emergent AI risks, and uses outcome records within them.
  - Support rationale: Outcome records can corroborate documented risk tracking over time.
  - Verification limit: State does not inspect tracking approaches, personnel or cadence. A Brigade artifact does not establish that an organizational control or process operated.

### Unmapped evidence properties

These claims have no direct control in this framework. No control identifier is assigned.

| Claim | Property | Disposition | Supersedes | Artifact contract | Responsible actor | Applicability | Rationale | Verification limit | Source |
|-------|----------|-------------|------------|-------------------|-------------------|---------------|-----------|--------------------|--------|
| EC-03 | signature integrity | unmapped-evidence-property | `GOVERN 5.2` | brigade.attestation.cosign-dsse.v1 Sigstore bundle (attestation.sigstore.json) from brigade receipts export attestation --profile cosign, checked locally for media type and DSSE envelope. | Adopting organization, which operates cosign verification and its trust root; Brigade only records the bundle. | Not applicable: no AI RMF 1.0 subcategory is assigned to this property, so no applicability condition is claimed. | A cosign bundle supports the integrity of other evidence. No AI RMF 1.0 subcategory addresses signature integrity directly, and GOVERN 5.2 concerns adjudicated feedback. | Bundle presence and media type are checked locally; signature verification needs cosign and an external trust root. A Brigade artifact does not establish that an organizational control or process operated. | https://nvlpubs.nist.gov/nistpubs/ai/NIST.AI.100-1.pdf Section 5.1, Table 1, GOVERN 5.2 |
| EC-11 | evidence retention and availability | unmapped-evidence-property | `MANAGE 2.4` | brigade.verify_archive_index.v1 index written by brigade work verify run before verify-run receipts are pruned. | Adopting organization, which owns storage protection and retention periods; Brigade only writes the index. | Not applicable: no AI RMF 1.0 subcategory is assigned to this property, so no applicability condition is claimed. | The archive index supports availability of verification evidence after pruning. No AI RMF 1.0 subcategory requires it directly, and MANAGE 2.4 concerns superseding or deactivating AI systems. | State checks that a parseable index exists; it assumes no retention period and does not verify archived content. A Brigade artifact does not establish that an organizational control or process operated. | https://nvlpubs.nist.gov/nistpubs/ai/NIST.AI.100-1.pdf Section 5.4, Table 4, MANAGE 2.4 |
| EC-12 | evidence packaging | unmapped-evidence-property | `GOVERN 5.2` | brigade.evidence_package.v1 package from brigade receipts export package (--run-id and --out), with an unsigned manifest checked by brigade receipts verify-package. | Adopting organization, which decides custody and distribution of packages; Brigade only assembles the package. | Not applicable: no AI RMF 1.0 subcategory is assigned to this property, so no applicability condition is claimed. | Packaging alone supplies no adjudicated feedback process. Map the constituent receipt and attestation evidence through EC-01 and EC-02 instead; EC-03 signature integrity is itself an unmapped evidence property. | The evaluator recomputes the unsigned manifest entries digest only. A Brigade artifact does not establish that an organizational control or process operated. | https://nvlpubs.nist.gov/nistpubs/ai/NIST.AI.100-1.pdf Section 5.1, Table 1, GOVERN 5.2 |

## NIST SP 800-218 Secure Software Development Framework (`nist-sp-800-218`)

Edition: SSDF v1.1 (Feb 2022) and SP 800-218A (2024-07-26). Source: https://csrc.nist.gov/projects/ssdf

| Control | Claim | Relationship | Obligation | Applicability | Rationale | Source |
|---------|-------|--------------|------------|---------------|-----------|--------|
| `PO.5` | EC-04 | supports | supplier | any | Protect development environments with signed change requests. | https://csrc.nist.gov/projects/ssdf |
| `PS.1` | EC-09 | supports | supplier | any | Protect software from tampering and unauthorized access. | https://csrc.nist.gov/projects/ssdf |
| `PS.2` | EC-02 | supports | supplier | any | Release integrity is supported by signed verification attestations. | https://csrc.nist.gov/projects/ssdf |
| `PS.2` | EC-03 | supports | supplier | any | Release integrity is verifiable by external parties using cosign. | https://csrc.nist.gov/projects/ssdf |
| `PS.2` | EC-08 | supports | supplier | any | Release integrity through commit linkage. | https://csrc.nist.gov/projects/ssdf |
| `PS.2` | EC-12 | supports | supplier | any | Package export supplies an unsigned manifest. The evaluator checks entries-list digest self-consistency, not file contents; verify-package rehashes files. | https://csrc.nist.gov/projects/ssdf |
| `PS.3` | EC-07 | supports | supplier | any | Archive and protect each software release to support inventory. | https://csrc.nist.gov/projects/ssdf |
| `PW.7` | EC-05 | supports | supplier | any | Review and approval of code changes. | https://csrc.nist.gov/projects/ssdf |
| `PW.8` | EC-01 | supports | supplier | any | Verify the software and confirm it behaves as intended with captured results. | https://csrc.nist.gov/projects/ssdf |
| `PW.8` | EC-10 | supports | supplier | any | Verify software behavior and record results. | https://csrc.nist.gov/projects/ssdf |
| `RV.1` | EC-06 | no-relationship | supplier | any | Lifecycle journal is not vulnerability response. | https://csrc.nist.gov/projects/ssdf |
| `RV.1` | EC-11 | no-relationship | supplier | any | Archive index is not vulnerability response. | https://csrc.nist.gov/projects/ssdf |

## NIST SP 800-53 Revision 5.1 (`nist-sp-800-53-r5-1`)

Edition: 5.1. Source: https://csrc.nist.gov/Projects/risk-management/sp800-53-controls/downloads

| Control | Claim | Relationship | Obligation | Applicability | Rationale | Source |
|---------|-------|--------------|------------|---------------|-----------|--------|
| `AC-2` | EC-04 | supports | service-organisation | any | Account management for change requesters. | https://csrc.nist.gov/Projects/risk-management/sp800-53-controls/downloads |
| `AU-11` | EC-11 | supports | service-organisation | any | Archive index records verification evidence before pruning; audit record retention periods and storage protection belong to the adopting organization. | https://csrc.nist.gov/Projects/risk-management/sp800-53-controls/downloads |
| `AU-12` | EC-01 | supports | service-organisation | any | Audit record generation captures verification events. | https://csrc.nist.gov/Projects/risk-management/sp800-53-controls/downloads |
| `AU-12` | EC-06 | supports | service-organisation | any | Audit record generation for lifecycle events. | https://csrc.nist.gov/Projects/risk-management/sp800-53-controls/downloads |
| `AU-12` | EC-10 | supports | service-organisation | any | Audit record content for outcomes. | https://csrc.nist.gov/Projects/risk-management/sp800-53-controls/downloads |
| `AU-6` | EC-09 | partially-supports | service-organisation | any | Audit record review for content findings. | https://csrc.nist.gov/Projects/risk-management/sp800-53-controls/downloads |
| `CM-3` | EC-02 | supports | service-organisation | any | Configuration change control with integrity evidence. | https://csrc.nist.gov/Projects/risk-management/sp800-53-controls/downloads |
| `CM-3` | EC-03 | supports | service-organisation | any | Change control evidence is externally verifiable. | https://csrc.nist.gov/Projects/risk-management/sp800-53-controls/downloads |
| `CM-3` | EC-05 | supports | service-organisation | any | Change approval with segregation of duties. | https://csrc.nist.gov/Projects/risk-management/sp800-53-controls/downloads |
| `CM-3` | EC-08 | supports | service-organisation | any | Change control records linked to commits. | https://csrc.nist.gov/Projects/risk-management/sp800-53-controls/downloads |
| `CM-3` | EC-12 | supports | service-organisation | any | Package export supplies an unsigned manifest. The evaluator checks entries-list digest self-consistency, not file contents; verify-package rehashes files. | https://csrc.nist.gov/Projects/risk-management/sp800-53-controls/downloads |
| `CM-8` | EC-07 | supports | service-organisation | any | Information system component inventory. | https://csrc.nist.gov/Projects/risk-management/sp800-53-controls/downloads |

## OWASP Top 10 for Agentic Applications (`owasp-agentic-2026`)

Edition: 2026. Source: https://genai.owasp.org/resource/owasp-top-10-for-agentic-applications-for-2026/

| Control | Claim | Relationship | Obligation | Applicability | Rationale | Source |
|---------|-------|--------------|------------|---------------|-----------|--------|
| `ASI02` | EC-09 | supports | deployer | any | Tool misuse prevention through content scanning. | https://genai.owasp.org/resource/owasp-top-10-for-agentic-applications-for-2026/ |
| `ASI03` | EC-04 | supports | deployer | any | Identity and privilege controls for change requests. | https://genai.owasp.org/resource/owasp-top-10-for-agentic-applications-for-2026/ |
| `ASI03` | EC-05 | supports | deployer | any | Identity and privilege abuse prevention through approval. | https://genai.owasp.org/resource/owasp-top-10-for-agentic-applications-for-2026/ |
| `ASI04` | EC-02 | partially-supports | deployer | any | Agentic supply chain integrity through signed verification results. | https://genai.owasp.org/resource/owasp-top-10-for-agentic-applications-for-2026/ |
| `ASI04` | EC-03 | supports | deployer | any | Supply chain verification through cosign bundle. | https://genai.owasp.org/resource/owasp-top-10-for-agentic-applications-for-2026/ |
| `ASI04` | EC-07 | supports | deployer | any | Agentic supply chain inventory. | https://genai.owasp.org/resource/owasp-top-10-for-agentic-applications-for-2026/ |
| `ASI04` | EC-08 | supports | deployer | any | Supply chain traceability through commit trailers. | https://genai.owasp.org/resource/owasp-top-10-for-agentic-applications-for-2026/ |
| `ASI04` | EC-12 | supports | deployer | any | Package export supplies an unsigned manifest. The evaluator checks entries-list digest self-consistency, not file contents; verify-package rehashes files. | https://genai.owasp.org/resource/owasp-top-10-for-agentic-applications-for-2026/ |
| `ASI05` | EC-01 | partially-supports | deployer | any | A passing test suite is not detection of unexpected execution. | https://genai.owasp.org/resource/owasp-top-10-for-agentic-applications-for-2026/ |
| `ASI05` | EC-06 | partially-supports | deployer | any | Lifecycle logs provide observability but do not directly detect unexpected code execution. | https://genai.owasp.org/resource/owasp-top-10-for-agentic-applications-for-2026/ |
| `ASI05` | EC-10 | partially-supports | deployer | any | Outcome records provide observability but do not directly detect unexpected code execution. | https://genai.owasp.org/resource/owasp-top-10-for-agentic-applications-for-2026/ |
| `ASI05` | EC-11 | partially-supports | deployer | any | Verification archives provide observability but do not directly detect unexpected code execution. | https://genai.owasp.org/resource/owasp-top-10-for-agentic-applications-for-2026/ |

## AICPA SOC 2 Trust Services Criteria (`soc2-tsc-2017`)

Edition: 2017 with 2022 points of focus. Source: https://www.aicpa-cima.com/resources/download/2017-trust-services-criteria-with-revised-points-of-focus-2022
Clause text: unverified.
Note: Licensed clause text was not reviewed for this crosswalk. Control identifiers and rationales are unverified against the licensed text and are not reconstructed from secondary summaries.

| Control | Claim | Relationship | Obligation | Applicability | Rationale | Source |
|---------|-------|--------------|------------|---------------|-----------|--------|
| `CC6.1` | EC-07 | supports | service-organisation | any | Inventory of logical access assets. | https://www.aicpa-cima.com/resources/download/2017-trust-services-criteria-with-revised-points-of-focus-2022 |
| `CC7.2` | EC-02 | partially-supports | service-organisation | any | Signed attestations are not operational monitoring. | https://www.aicpa-cima.com/resources/download/2017-trust-services-criteria-with-revised-points-of-focus-2022 |
| `CC7.2` | EC-03 | partially-supports | service-organisation | any | External verification is not operational monitoring. | https://www.aicpa-cima.com/resources/download/2017-trust-services-criteria-with-revised-points-of-focus-2022 |
| `CC7.2` | EC-06 | supports | service-organisation | any | System activity logging for monitoring. | https://www.aicpa-cima.com/resources/download/2017-trust-services-criteria-with-revised-points-of-focus-2022 |
| `CC7.2` | EC-09 | supports | service-organisation | any | Monitoring for sensitive data handling. | https://www.aicpa-cima.com/resources/download/2017-trust-services-criteria-with-revised-points-of-focus-2022 |
| `CC7.2` | EC-11 | supports | service-organisation | any | Archive index keeps verification evidence available for monitoring after pruning; log retention belongs to the adopting organization. | https://www.aicpa-cima.com/resources/download/2017-trust-services-criteria-with-revised-points-of-focus-2022 |
| `CC7.2` | EC-12 | partially-supports | service-organisation | any | Package export supplies an unsigned manifest. The evaluator checks entries-list digest self-consistency, not file contents; verify-package rehashes files. Not operational monitoring. | https://www.aicpa-cima.com/resources/download/2017-trust-services-criteria-with-revised-points-of-focus-2022 |
| `CC8.1` | EC-01 | supports | service-organisation | any | Changes are authorized, tested, and implemented with captured exit status. | https://www.aicpa-cima.com/resources/download/2017-trust-services-criteria-with-revised-points-of-focus-2022 |
| `CC8.1` | EC-04 | supports | service-organisation | any | Logical access controls for change requesters. | https://www.aicpa-cima.com/resources/download/2017-trust-services-criteria-with-revised-points-of-focus-2022 |
| `CC8.1` | EC-05 | supports | service-organisation | any | Segregation of duties for approval. | https://www.aicpa-cima.com/resources/download/2017-trust-services-criteria-with-revised-points-of-focus-2022 |
| `CC8.1` | EC-08 | supports | service-organisation | any | Changes are documented and linked to receipts. | https://www.aicpa-cima.com/resources/download/2017-trust-services-criteria-with-revised-points-of-focus-2022 |
| `CC8.1` | EC-10 | supports | service-organisation | any | Change management records in outcome ledger. | https://www.aicpa-cima.com/resources/download/2017-trust-services-criteria-with-revised-points-of-focus-2022 |

## Evidence state contract

`brigade evidence controls --json` emits schema `brigade.evidence_controls.v2`. Each claim is assessed once under the `brigade.claim_readiness.v1` contract and reported in `evidence_readiness`, keyed by claim id. This is a readiness index. It is not a score, an enforcement gate, an audit conclusion, a certification claim or a guarantee for any tenant. The command exits 0 whatever the outcomes are.

Each assessment records:

- `evaluator`: the state rule id, evaluator version and kind (`in_module`, `structural` or `delegated`).
- `verifier`: the dedicated verifier name and status (`wired`, `not_wired`, `not_required` or `unavailable` when a required tool such as `ssh-keygen` or `git` is missing).
- `validation_level`: the depth of processing actually performed, one of `none`, `discovered`, `structure_observed` or `claim_validated`. It is the lowest level across in-scope artifacts. Levels are observations, not a trust ladder.
- `outcome` and `reason`: a stable outcome and a fixed reason code. `reason` is null when no code applies, as for a validated claim; it is never an empty string. `reason_codes` lists every code observed for in-scope artifacts and the population.
- `dimensions`: `integrity`, `signature`, `authorization`, `subject`, `freshness` and `population`, each with a status (`passed`, `failed`, `unknown`, `unavailable`, `not_checked` or `not_applicable`) and a reason code. `required_dimensions` names the dimensions the claim needs.
- `population`: bounded counts of discovered, in-scope, `wrong_run`, `out_of_period` and `unbound` artifacts, per-outcome counts, the `scan_limit` and whether discovery was `truncated`. EC-08 adds `window`: the most recent 20 commits, with counts `inspected`, `with_trailer`, `without_trailer` and `unreadable`. Only commits carrying both `Brigade-Run` and `Brigade-Receipt` trailers within that window are the claim's artifacts, so one valid trailer never implies that every commit in the window is linked. Older history is outside the claim and is not truncation.
- `artifacts`: up to 20 per-artifact observations sorted by relative path, with scope, run binding field, level, outcome, reason codes and dimensions. Reasons never include artifact content or absolute host paths.

Outcomes: `validated` (claim-level validation passed every required dimension), `structure_observed`, `discovered_only`, `failed` (valid evidence of a failed operation), `rejected` (a verifier or integrity check rejected the artifact), `invalid` (the artifact breaks its own format), `unavailable` (a required verifier or tool could not run), `incomplete` (partial, unbound, empty or truncated evidence), `absent` and `not_applicable`.

Aggregation: per artifact, a failed required dimension yields `rejected`, an unavailable one yields `unavailable` and an unknown one yields `incomplete`. A passed signature never offsets another failed dimension. `validated` requires `claim_validated` depth and every required dimension passed or not applicable. Per claim, the most severe in-scope outcome wins, in the order rejected, invalid, failed, unavailable, incomplete, absent, discovered_only, structure_observed, validated. A claim is therefore `validated` only when every in-scope artifact validated, and one valid artifact cannot hide a rejected, incomplete, structure-only or discovered-only sibling. Claim `validation_level` is the lowest in-scope level and each claim dimension is the worst in-scope status; the claim outcome is reconciled against both. An artifact-level `not_applicable` population becomes `passed` at claim level only when discovery enumerated the population without truncation or unbound artifacts. Wrong-run and out-of-period artifacts are excluded and counted. Under `--run-id`, artifacts that record no run identity are `unbound`, are never assumed to belong to the run, and keep the claim `incomplete`. An EC-08 missing history tool, a whole-claim verifier error or an unreadable discovery root is a claim-wide `unavailable` observation under any scope. Per-artifact verifier failures retain their recorded scope and reasons; an artifact whose run cannot be bound remains `unbound` under a selected run.

Population bounds: a directory scan reads at most 201 entries and inspects the first 200 sorted names it read. Past the cap the claim is `incomplete` with `population_truncated`, and the inspected sample depends on filesystem order. With `--run-id`, EC-04, EC-05 and EC-06 inspect `.brigade/runs/<run-id>` directly, so a selected run is never lost behind the cap. Verify receipts bind to a run through recorded fields, so their scan stays bounded and can truncate. The run id must be a bare directory name. EC-12 discovers `.brigade/evidence-packages/manifest.json` and `.brigade/evidence-packages/<package>/manifest.json`; deeper nesting is not discovered, and no strict package verification runs before #1618. Packages bind through either their recorded producer-run or verify-run identity. JSONL reads are capped at 8 MiB and 10,000 records. `artifacts_reported_truncated` reports when the 20-artifact output cap omits observed artifacts.

Verify receipts (EC-01): `running` is nonterminal (`status_not_terminal`). `canceled` is terminal but `incomplete` (`status_canceled`). `failed` and `rejected` are terminal `failed` outcomes, and a command with a nonzero exit code is `failed`. A `completed` receipt needs a non-empty `planned_commands` list, a `run_id` matching its directory, and one `completed` command with exit code 0 per planned check whose `command` display string equals the planned entry at the same position. A different, missing or reordered command is `incomplete` with `planned_commands_mismatch`. An empty intended check list never shows that a test ran. The receipt is unsigned and its stored digest is not rederived before #1618, so `integrity` is a required dimension reported `not_checked` with `verifier_not_wired`, and even a well-formed successful receipt stops at `structure_observed` (`untested`). A reused receipt (`reused_from`) copies the commands of an earlier run; it is recorded, reused evidence, not a new execution under the new receipt id. EC-02 pins re-derivation to the attestation's own directory receipt and rejects an attestation whose run differs from its directory. EC-06 rejects a journal whose events name another run.

Signed requests and approvals (EC-04, EC-05): EC-04 discovers the producer path `.brigade/runs/<run>/requests/<nonce>.json` written by `agent_request.record_request`, plus the legacy `agent-request.json` and `request.json` names, with a bounded `requests/` scan. It checks signature, key trust and that the statement names the run directory. It does not independently check that the request preceded worker dispatch. EC-05 reads the latest approval event in the lifecycle journal. Without `ssh-keygen`, a failure that needs no signature check (a broken or partial journal chain, an event naming another run, or an invalid approval event nonce, path or statement) stays `rejected`; only otherwise is the claim `unavailable`. A trusted key is a key-trust result; organizational identity and authority remain the adopting organization's evidence.

Commit trailers (EC-08): a trailer whose local `run.json` is missing is `incomplete` with integrity `unknown` and `entry_missing`; it is never reported as a digest comparison failure. A symlinked run directory or `run.json` is refused as `invalid` with `symlink_refused`. A recomputed digest that differs from the trailer is `rejected` with `trailer_digest_mismatch`.

Freshness: only the Python API accepts an explicit assessment period; the CLI has no period option. Without one, freshness is `not_applicable` with `period_not_supplied`. It is never reported as fresh. With a period, an artifact without a timestamp is `unknown` and keeps the claim `incomplete`. EC-10 reads the outcome record `ts` field.

Legacy `state`: every mapping row keeps the four legacy values as a conservative projection. Only `validated` maps to `evidenced_passed`. `failed` and `rejected` map to `evidenced_failed`. `not_applicable` maps to `not_applicable`. Every other outcome maps to `untested`. Rows also carry `validation_level` and `evidence_outcome`, and text output shows `[state | level/outcome]`.

Migration from `brigade.evidence_controls.v1`: existing keys are unchanged, and `readiness_contract`, `assessment_period` and `evidence_readiness` are added. `evidenced_passed` is stricter. Claims checked only for presence or structure (EC-01, EC-03, EC-07, EC-09, EC-10, EC-11 and EC-12) now report `untested` with `structure_observed` until a dedicated verifier is wired. An EC-01 receipt with an empty command list or commands that do not match `planned_commands` is no longer a pass, and legacy receipts without `planned_commands` report `untested`. Artifacts now classified `invalid` (for example a previously accepted malformed cosign bundle or package manifest, or a ledger with a row lacking its required field) report `untested`. v1 passed EC-08 when any one trailer matched and failed it when none did; now every trailered commit in the window counts, and a trailer whose local receipt is missing keeps the claim `untested`. The claim `reason` is null rather than an empty string when no code applies. Receipts with status `failed` or `rejected` now report `evidenced_failed`; v1 counted only completed receipts and reported them `untested`. A missing `ssh-keygen` or `git` reports `unavailable` instead of a failure. EC-08 still assesses the most recent 20 commits. Consumers should read `evidence_readiness` to see what was validated rather than infer validation from `state`.
