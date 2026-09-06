# Brigade Control Crosswalk

Evidence index for configured Brigade artifacts. Not a compliance determination, not evidence that a control operated.

Crosswalk version: 1. Schema: `brigade.evidence_controls.v1`.

Scope: workspace.

## CSA AI Controls Matrix (`csa-aicm-v1-1`)

Not mapped in crosswalk version 1.

## Regulation (EU) 2024/1689 Artificial Intelligence Act (`eu-ai-act-2024-1689`)

| Control | Claim | Relationship | Obligation | Applicability | State | Rationale | Source |
|---------|-------|--------------|------------|---------------|-------|-----------|--------|
| `Art.11` | EC-07 | supports | provider | provider of a high-risk AI system | untested | Technical documentation includes agent, model, and tool inventory. | https://artificialintelligenceact.eu/article/11/ |
| `Art.11` | EC-12 | no-relationship | provider | provider of a high-risk AI system | not_applicable | Evidence package is not yet implemented; artifact is absent until issue 1407 merges. | https://artificialintelligenceact.eu/article/11/ |
| `Art.12` | EC-01 | supports | provider | provider of a high-risk AI system | evidenced_passed | Automatic event logging provides traceability of verification executions. | https://artificialintelligenceact.eu/article/12/ |
| `Art.12` | EC-02 | partially-supports | provider | provider of a high-risk AI system | untested | Signed verification results support log integrity for high-risk systems. | https://artificialintelligenceact.eu/article/12/ |
| `Art.12` | EC-03 | partially-supports | provider | provider of a high-risk AI system | untested | External verification supports log integrity for high-risk systems. | https://artificialintelligenceact.eu/article/12/ |
| `Art.12` | EC-06 | supports | provider | provider of a high-risk AI system | evidenced_passed | Automatic logging of events supports traceability. | https://artificialintelligenceact.eu/article/12/ |
| `Art.12` | EC-08 | supports | provider | provider of a high-risk AI system | untested | Traceability through commit linkage to receipts. | https://artificialintelligenceact.eu/article/12/ |
| `Art.14` | EC-04 | partially-supports | provider | provider of a high-risk AI system | untested | Human oversight can request and authorize changes before dispatch. | https://artificialintelligenceact.eu/article/14/ |
| `Art.14` | EC-05 | supports | provider | provider of a high-risk AI system | untested | Competent human oversight and approval before changes proceed. | https://artificialintelligenceact.eu/article/14/ |
| `Art.17` | EC-10 | supports | provider | provider of a high-risk AI system | evidenced_passed | Quality management system records. | https://artificialintelligenceact.eu/article/17/ |
| `Art.26` | EC-11 | supports | deployer | deployer of a high-risk AI system under Art. 26 | untested | Deployers keep logs under their control for six months. | https://artificialintelligenceact.eu/article/26/ |
| `Art.9` | EC-09 | no-relationship | provider | provider of a high-risk AI system | not_applicable | Risk management system records are outside Brigade evidence scope. | https://artificialintelligenceact.eu/article/9/ |

## ISO/IEC 27001:2022 (`iso-27001-2022`)

| Control | Claim | Relationship | Obligation | Applicability | State | Rationale | Source |
|---------|-------|--------------|------------|---------------|-------|-----------|--------|
| `A.5.9` | EC-07 | supports | service-organisation | any | untested | Inventory of information assets. | https://www.iso.org/standard/27001 |
| `A.8.12` | EC-09 | supports | service-organisation | any | untested | Guard audit scans content for leaks, not logs. | https://www.iso.org/standard/27001 |
| `A.8.15` | EC-02 | supports | service-organisation | any | untested | Log protection is supported by signed verification results. | https://www.iso.org/standard/27001 |
| `A.8.15` | EC-03 | supports | service-organisation | any | untested | Log protection supports external verification. | https://www.iso.org/standard/27001 |
| `A.8.15` | EC-06 | supports | service-organisation | any | evidenced_passed | Logging of lifecycle events. | https://www.iso.org/standard/27001 |
| `A.8.15` | EC-10 | supports | service-organisation | any | evidenced_passed | Log content protection for outcome records. | https://www.iso.org/standard/27001 |
| `A.8.15` | EC-11 | supports | service-organisation | any | untested | Log retention and protection through archive index. | https://www.iso.org/standard/27001 |
| `A.8.15` | EC-12 | no-relationship | service-organisation | any | not_applicable | Evidence package is not yet implemented; artifact is absent until issue 1407 merges. | https://www.iso.org/standard/27001 |
| `A.8.32` | EC-01 | supports | service-organisation | any | evidenced_passed | Changes are tested and verified before implementation. | https://www.iso.org/standard/27001 |
| `A.8.32` | EC-04 | supports | service-organisation | any | untested | Changes are authorized before implementation. | https://www.iso.org/standard/27001 |
| `A.8.32` | EC-05 | supports | service-organisation | any | untested | Changes are approved by authorized personnel. | https://www.iso.org/standard/27001 |
| `A.8.32` | EC-08 | supports | service-organisation | any | untested | Change management records linked to commits. | https://www.iso.org/standard/27001 |

## ISO/IEC 42001:2023 (`iso-42001-2023`)

| Control | Claim | Relationship | Obligation | Applicability | State | Rationale | Source |
|---------|-------|--------------|------------|---------------|-------|-----------|--------|
| `7.5` | EC-02 | partially-supports | provider | any | untested | Signed Test Result attestation supports integrity of documented verification information. | https://www.iso.org/standard/42001 |
| `7.5` | EC-03 | partially-supports | provider | any | untested | Cosign bundle supports external verification of documented information. | https://www.iso.org/standard/42001 |
| `7.5` | EC-08 | supports | deployer | any | untested | Commit trailers link documented information to code changes. | https://www.iso.org/standard/42001 |
| `7.5` | EC-12 | no-relationship | provider | any | not_applicable | Evidence package is not yet implemented; artifact is absent until issue 1407 merges. | https://www.iso.org/standard/42001 |
| `9.1` | EC-10 | supports | deployer | any | evidenced_passed | Monitoring and measurement evidence in outcome ledger. | https://www.iso.org/standard/42001 |
| `A.3.2` | EC-05 | supports | deployer | any | untested | Segregation of duties for human approval of AI changes. | https://www.iso.org/standard/42001 |
| `A.4.2` | EC-07 | supports | deployer | any | untested | Inventory of resources including agents, models, and tools. | https://www.iso.org/standard/42001 |
| `A.5` | EC-07 | no-relationship | deployer | any | not_applicable | no sourced sub-control identifier | https://www.iso.org/standard/42001 |
| `A.6.2.1` | EC-04 | supports | deployer | any | untested | Signed change request supports AI system lifecycle control. | https://www.iso.org/standard/42001 |
| `A.6.2.8` | EC-01 | supports | service-organisation | any | evidenced_passed | Verify receipts capture per-command exit status and are immutable, supporting AI system event log recording. | https://www.iso.org/standard/42001 |
| `A.6.2.8` | EC-06 | supports | deployer | any | evidenced_passed | Append-only lifecycle journal records AI system events. | https://www.iso.org/standard/42001 |
| `A.6.2.8` | EC-11 | supports | deployer | any | untested | Archive index ensures verification evidence is preserved before pruning. | https://www.iso.org/standard/42001 |
| `A.7` | EC-09 | no-relationship | deployer | any | not_applicable | no sourced sub-control identifier | https://www.iso.org/standard/42001 |

## NIST AI 600-1 Generative AI Profile (`nist-ai-600-1`)

Not mapped in crosswalk version 1.

## NIST AI Risk Management Framework (`nist-ai-rmf-1.0`)

| Control | Claim | Relationship | Obligation | Applicability | State | Rationale | Source |
|---------|-------|--------------|------------|---------------|-------|-----------|--------|
| `GOVERN-1.2` | EC-04 | supports | deployer | any | untested | Roles and responsibilities for change requests are defined. | https://www.nist.gov/itl/ai-risk-management-framework |
| `GOVERN-2.1` | EC-05 | supports | deployer | any | untested | Human oversight and accountability for AI risks. | https://www.nist.gov/itl/ai-risk-management-framework |
| `GOVERN-5.1` | EC-07 | supports | deployer | any | untested | Inventory AI systems and supply chain. | https://www.nist.gov/itl/ai-risk-management-framework |
| `GOVERN-5.2` | EC-02 | supports | provider | any | untested | Signed verification results manage supply-chain risks. | https://www.nist.gov/itl/ai-risk-management-framework |
| `GOVERN-5.2` | EC-03 | supports | provider | any | untested | Cross-organizational verification through Sigstore bundle. | https://www.nist.gov/itl/ai-risk-management-framework |
| `GOVERN-5.2` | EC-12 | no-relationship | provider | any | not_applicable | Evidence package is not yet implemented; artifact is absent until issue 1407 merges. | https://www.nist.gov/itl/ai-risk-management-framework |
| `MANAGE-2.4` | EC-08 | supports | deployer | any | untested | Track changes and communicate risks through commit linkage. | https://www.nist.gov/itl/ai-risk-management-framework |
| `MANAGE-2.4` | EC-11 | supports | deployer | any | untested | Change management and record retention through archive index. | https://www.nist.gov/itl/ai-risk-management-framework |
| `MAP-2.3` | EC-09 | supports | deployer | any | untested | Categorize risks and impacts including data leakage. | https://www.nist.gov/itl/ai-risk-management-framework |
| `MEASURE-1.1` | EC-01 | supports | deployer | any | evidenced_passed | Test execution evidence with captured exit codes supports valid and reliable measurement. | https://www.nist.gov/itl/ai-risk-management-framework |
| `MEASURE-1.1` | EC-10 | supports | deployer | any | evidenced_passed | Valid and reliable measurement through outcome records. | https://www.nist.gov/itl/ai-risk-management-framework |
| `MEASURE-2.1` | EC-06 | supports | deployer | any | evidenced_passed | Monitor AI systems for risks and incidents through lifecycle logs. | https://www.nist.gov/itl/ai-risk-management-framework |

## NIST SP 800-218 Secure Software Development Framework (`nist-sp-800-218`)

| Control | Claim | Relationship | Obligation | Applicability | State | Rationale | Source |
|---------|-------|--------------|------------|---------------|-------|-----------|--------|
| `PO.5` | EC-04 | supports | supplier | any | untested | Protect development environments with signed change requests. | https://csrc.nist.gov/projects/ssdf |
| `PS.1` | EC-09 | supports | supplier | any | untested | Protect software from tampering and unauthorized access. | https://csrc.nist.gov/projects/ssdf |
| `PS.2` | EC-02 | supports | supplier | any | untested | Release integrity is supported by signed verification attestations. | https://csrc.nist.gov/projects/ssdf |
| `PS.2` | EC-03 | supports | supplier | any | untested | Release integrity is verifiable by external parties using cosign. | https://csrc.nist.gov/projects/ssdf |
| `PS.2` | EC-08 | supports | supplier | any | untested | Release integrity through commit linkage. | https://csrc.nist.gov/projects/ssdf |
| `PS.2` | EC-12 | no-relationship | supplier | any | not_applicable | Evidence package is not yet implemented; artifact is absent until issue 1407 merges. | https://csrc.nist.gov/projects/ssdf |
| `PS.3` | EC-07 | supports | supplier | any | untested | Archive and protect each software release to support inventory. | https://csrc.nist.gov/projects/ssdf |
| `PW.7` | EC-05 | supports | supplier | any | untested | Review and approval of code changes. | https://csrc.nist.gov/projects/ssdf |
| `PW.8` | EC-01 | supports | supplier | any | evidenced_passed | Verify the software and confirm it behaves as intended with captured results. | https://csrc.nist.gov/projects/ssdf |
| `PW.8` | EC-10 | supports | supplier | any | evidenced_passed | Verify software behavior and record results. | https://csrc.nist.gov/projects/ssdf |
| `RV.1` | EC-06 | no-relationship | supplier | any | not_applicable | Lifecycle journal is not vulnerability response. | https://csrc.nist.gov/projects/ssdf |
| `RV.1` | EC-11 | no-relationship | supplier | any | not_applicable | Archive index is not vulnerability response. | https://csrc.nist.gov/projects/ssdf |

## NIST SP 800-53 Revision 5.1 (`nist-sp-800-53-r5-1`)

| Control | Claim | Relationship | Obligation | Applicability | State | Rationale | Source |
|---------|-------|--------------|------------|---------------|-------|-----------|--------|
| `AC-2` | EC-04 | supports | service-organisation | any | untested | Account management for change requesters. | https://csrc.nist.gov/Projects/risk-management/sp800-53-controls/downloads |
| `AU-11` | EC-11 | supports | service-organisation | any | untested | Audit record retention through archive index. | https://csrc.nist.gov/Projects/risk-management/sp800-53-controls/downloads |
| `AU-12` | EC-01 | supports | service-organisation | any | evidenced_passed | Audit record generation captures verification events. | https://csrc.nist.gov/Projects/risk-management/sp800-53-controls/downloads |
| `AU-12` | EC-06 | supports | service-organisation | any | evidenced_passed | Audit record generation for lifecycle events. | https://csrc.nist.gov/Projects/risk-management/sp800-53-controls/downloads |
| `AU-12` | EC-10 | supports | service-organisation | any | evidenced_passed | Audit record content for outcomes. | https://csrc.nist.gov/Projects/risk-management/sp800-53-controls/downloads |
| `AU-6` | EC-09 | partially-supports | service-organisation | any | untested | Audit record review for content findings. | https://csrc.nist.gov/Projects/risk-management/sp800-53-controls/downloads |
| `CM-3` | EC-02 | supports | service-organisation | any | untested | Configuration change control with integrity evidence. | https://csrc.nist.gov/Projects/risk-management/sp800-53-controls/downloads |
| `CM-3` | EC-03 | supports | service-organisation | any | untested | Change control evidence is externally verifiable. | https://csrc.nist.gov/Projects/risk-management/sp800-53-controls/downloads |
| `CM-3` | EC-05 | supports | service-organisation | any | untested | Change approval with segregation of duties. | https://csrc.nist.gov/Projects/risk-management/sp800-53-controls/downloads |
| `CM-3` | EC-08 | supports | service-organisation | any | untested | Change control records linked to commits. | https://csrc.nist.gov/Projects/risk-management/sp800-53-controls/downloads |
| `CM-3` | EC-12 | no-relationship | service-organisation | any | not_applicable | Evidence package is not yet implemented; artifact is absent until issue 1407 merges. | https://csrc.nist.gov/Projects/risk-management/sp800-53-controls/downloads |
| `CM-8` | EC-07 | supports | service-organisation | any | untested | Information system component inventory. | https://csrc.nist.gov/Projects/risk-management/sp800-53-controls/downloads |

## OWASP Top 10 for Agentic Applications (`owasp-agentic-2026`)

| Control | Claim | Relationship | Obligation | Applicability | State | Rationale | Source |
|---------|-------|--------------|------------|---------------|-------|-----------|--------|
| `ASI02` | EC-09 | supports | deployer | any | untested | Tool misuse prevention through content scanning. | https://genai.owasp.org/resource/owasp-top-10-for-agentic-applications-for-2026/ |
| `ASI03` | EC-04 | supports | deployer | any | untested | Identity and privilege controls for change requests. | https://genai.owasp.org/resource/owasp-top-10-for-agentic-applications-for-2026/ |
| `ASI03` | EC-05 | supports | deployer | any | untested | Identity and privilege abuse prevention through approval. | https://genai.owasp.org/resource/owasp-top-10-for-agentic-applications-for-2026/ |
| `ASI04` | EC-02 | partially-supports | deployer | any | untested | Agentic supply chain integrity through signed verification results. | https://genai.owasp.org/resource/owasp-top-10-for-agentic-applications-for-2026/ |
| `ASI04` | EC-03 | supports | deployer | any | untested | Supply chain verification through cosign bundle. | https://genai.owasp.org/resource/owasp-top-10-for-agentic-applications-for-2026/ |
| `ASI04` | EC-07 | supports | deployer | any | untested | Agentic supply chain inventory. | https://genai.owasp.org/resource/owasp-top-10-for-agentic-applications-for-2026/ |
| `ASI04` | EC-08 | supports | deployer | any | untested | Supply chain traceability through commit trailers. | https://genai.owasp.org/resource/owasp-top-10-for-agentic-applications-for-2026/ |
| `ASI04` | EC-12 | no-relationship | deployer | any | not_applicable | Evidence package is not yet implemented; artifact is absent until issue 1407 merges. | https://genai.owasp.org/resource/owasp-top-10-for-agentic-applications-for-2026/ |
| `ASI05` | EC-01 | partially-supports | deployer | any | evidenced_passed | A passing test suite is not detection of unexpected execution. | https://genai.owasp.org/resource/owasp-top-10-for-agentic-applications-for-2026/ |
| `ASI05` | EC-06 | partially-supports | deployer | any | evidenced_passed | Lifecycle logs provide observability but do not directly detect unexpected code execution. | https://genai.owasp.org/resource/owasp-top-10-for-agentic-applications-for-2026/ |
| `ASI05` | EC-10 | partially-supports | deployer | any | evidenced_passed | Outcome records provide observability but do not directly detect unexpected code execution. | https://genai.owasp.org/resource/owasp-top-10-for-agentic-applications-for-2026/ |
| `ASI05` | EC-11 | partially-supports | deployer | any | untested | Verification archives provide observability but do not directly detect unexpected code execution. | https://genai.owasp.org/resource/owasp-top-10-for-agentic-applications-for-2026/ |

## AICPA SOC 2 Trust Services Criteria (`soc2-tsc-2017`)

| Control | Claim | Relationship | Obligation | Applicability | State | Rationale | Source |
|---------|-------|--------------|------------|---------------|-------|-----------|--------|
| `CC6.1` | EC-07 | supports | service-organisation | any | untested | Inventory of logical access assets. | https://www.aicpa-cima.com/resources/download/2017-trust-services-criteria-with-revised-points-of-focus-2022 |
| `CC7.2` | EC-02 | partially-supports | service-organisation | any | untested | Signed attestations are not operational monitoring. | https://www.aicpa-cima.com/resources/download/2017-trust-services-criteria-with-revised-points-of-focus-2022 |
| `CC7.2` | EC-03 | partially-supports | service-organisation | any | untested | External verification is not operational monitoring. | https://www.aicpa-cima.com/resources/download/2017-trust-services-criteria-with-revised-points-of-focus-2022 |
| `CC7.2` | EC-06 | supports | service-organisation | any | evidenced_passed | System activity logging for monitoring. | https://www.aicpa-cima.com/resources/download/2017-trust-services-criteria-with-revised-points-of-focus-2022 |
| `CC7.2` | EC-09 | supports | service-organisation | any | untested | Monitoring for sensitive data handling. | https://www.aicpa-cima.com/resources/download/2017-trust-services-criteria-with-revised-points-of-focus-2022 |
| `CC7.2` | EC-11 | supports | service-organisation | any | untested | Log retention for monitoring through archive index. | https://www.aicpa-cima.com/resources/download/2017-trust-services-criteria-with-revised-points-of-focus-2022 |
| `CC7.2` | EC-12 | no-relationship | service-organisation | any | not_applicable | Evidence package is not yet implemented; artifact is absent until issue 1407 merges. | https://www.aicpa-cima.com/resources/download/2017-trust-services-criteria-with-revised-points-of-focus-2022 |
| `CC8.1` | EC-01 | supports | service-organisation | any | evidenced_passed | Changes are authorized, tested, and implemented with captured exit status. | https://www.aicpa-cima.com/resources/download/2017-trust-services-criteria-with-revised-points-of-focus-2022 |
| `CC8.1` | EC-04 | supports | service-organisation | any | untested | Logical access controls for change requesters. | https://www.aicpa-cima.com/resources/download/2017-trust-services-criteria-with-revised-points-of-focus-2022 |
| `CC8.1` | EC-05 | supports | service-organisation | any | untested | Segregation of duties for approval. | https://www.aicpa-cima.com/resources/download/2017-trust-services-criteria-with-revised-points-of-focus-2022 |
| `CC8.1` | EC-08 | supports | service-organisation | any | untested | Changes are documented and linked to receipts. | https://www.aicpa-cima.com/resources/download/2017-trust-services-criteria-with-revised-points-of-focus-2022 |
| `CC8.1` | EC-10 | supports | service-organisation | any | evidenced_passed | Change management records in outcome ledger. | https://www.aicpa-cima.com/resources/download/2017-trust-services-criteria-with-revised-points-of-focus-2022 |
