# Survey: attestations for autonomous coding-agent orchestration

As of 2026-09-02, no single standard proves the full chain “human approved task -> agent/model acted -> code changed -> tests passed -> deployment occurred.” The interoperable foundation is:

1. in-toto Statement and DSSE for signed claims.
2. SLSA Provenance for source, build inputs, builder, and execution.
3. in-toto Test Result for test evidence.
4. A small custom in-toto predicate for agent, model, human, authorization, tool calls, and policy decisions.
5. Sigstore bundles for keyless signatures and Rekor transparency.
6. SPDX or CycloneDX for software, model, and dataset inventories.
7. OpenTelemetry for high-volume runtime traces, referenced by digest from attestations.

The standards below are cited to official sources, with publication or access dates where available.

## 1. Supply-chain attestation standards

| Standard | Issuing body and 2026 status | Relevant requirement | Machine-readable format | Reference tooling |
|---|---|---|---|---|
| SLSA v1.1 | OpenSSF/SLSA. Previous stable generation; v1.2 is now approved and current. | Build levels express increasing guarantees: L1 provenance exists, L2 signed provenance from a hosted builder, L3 hardened builder. Source L2 requires contemporaneous source provenance recording who made a revision and which controls were enforced. Source L4 requires code review. | in-toto Statement: `"_type": "https://in-toto.io/Statement/v1"`, `subject[]`, `"predicateType": "https://slsa.dev/provenance/v1"`, and `predicate`. Source provenance uses `"https://slsa.dev/source/v1"`. | `slsa-verifier`, SLSA GitHub generator, GitHub `attest-build-provenance`, GitLab provenance attestations. [SLSA specification](https://slsa.dev/spec/v1.2/) and [source requirements](https://slsa.dev/spec/v1.2/source-requirements), accessed 2026-09-02. |
| SLSA v1.2 | OpenSSF/SLSA. Approved/current as of 2026-09-02. | Reintroduces the Source Track and defines Build L0-L3. Source L2 requires immutable history and source provenance; Source L4 requires review. Provenance describes what was built, by which process, from which inputs. | Predicate URI remains `https://slsa.dev/provenance/v1`, deliberately stable across compatible minor revisions. Typical predicate fields include `buildDefinition`, `externalParameters`, `resolvedDependencies`, `runDetails`, `builder`, and `metadata`. | `slsa-verifier`, `slsa-github-generator`, `in-toto`, `gitsign`, Sigstore. [Build levels](https://slsa.dev/spec/v1.2/build-track-basics), [provenance schema](https://slsa.dev/spec/v1.2/build-provenance). |
| in-toto Attestation Framework v1.2.0 | CNCF/in-toto. v1.2.0 released 2026-03-18; framework still has some predicates and verifier implementations under active development. | Authenticated claims bind a subject digest to arbitrary metadata. The verifier separates envelope signature verification, Statement validation, subject matching, and policy evaluation. | Core Statement: `{ "_type": "https://in-toto.io/Statement/v1", "subject": [{"name": "...", "digest": {"sha256": "..."}}], "predicateType": "https://...", "predicate": {} }`. Usually DSSE-wrapped. | `in-toto-python`, Go/Rust/Java bindings, `in-toto/attestation-verifier` prototype, GitHub artifact attestations. [Framework repository](https://github.com/in-toto/attestation), accessed 2026-09-02. |
| in-toto SCAI v0.3 | in-toto community predicate. Vetted, but not a universal compliance vocabulary. | Evidence-backed attribute assertions about an artifact, producer, toolchain, or platform. Useful for facts such as “agent ran with network disabled,” “human approval required,” or “test evidence exists.” | Predicate URI: `https://in-toto.io/attestation/scai/v0.3`. Shape: `{"attributes":[{"attribute":"...", "target":{}, "conditions":{}, "evidence":{}}], "producer":{}}`. Evidence is a `ResourceDescriptor` with URI, digest, media type, and optional name. | in-toto libraries and policy engines. [SCAI predicate](https://github.com/in-toto/attestation/blob/main/spec/predicates/scai.md), accessed 2026-09-02. |
| SLSA Verification Summary Attestation | SLSA/in-toto. Stable predicate, useful as a derived verification decision. | Records that a verifier checked an artifact and its provenance against a policy or SLSA level. It can cache or delegate verification decisions. | Predicate URI: `https://slsa.dev/verification_summary/v1`. The exact predicate contains verifier identity, policy decision, and verification materials. | `slsa-verifier`, policy engines, in-toto verifiers. [VSA predicate](https://github.com/in-toto/attestation/blob/main/spec/predicates/vsa.md). |
| in-toto Test Result v0.1 | in-toto community predicate. Vetted, version 0.1.0. | Expresses whether a test invocation passed, warned, or failed; records test configuration and individual passed, warned, and failed tests. | Predicate URI: `https://in-toto.io/attestation/test-result/v0.1`. Shape: `{"result":"PASSED|WARNED|FAILED","configuration":[ResourceDescriptor], "url":"...", "passedTests":[], "warnedTests":[], "failedTests":[]}`. | in-toto verifier, CI adapters, GitHub Actions, GitLab CI. [Test Result predicate](https://github.com/in-toto/attestation/blob/main/spec/predicates/test-result.md). |
| in-toto Runtime Trace v0.1 | in-toto community predicate. Vetted predicate; implementation maturity varies. | Captures build-time or execution-time observations such as process activity, network connections, and file access. | Predicate URI: `https://in-toto.io/attestation/runtime-trace/v0.1`. Exact schema should be treated as predicate-specific and verified against the current repository. | in-toto tooling and runtime monitors. [Predicate catalog](https://github.com/in-toto/attestation/blob/main/spec/predicates/README.md). |
| in-toto Reference v0.1 | in-toto community predicate. Vetted. | References externally stored evidence, such as SBOMs, logs, test reports, or approval records, while signing their digest and location. | Predicate URI: `https://in-toto.io/attestation/reference/v0.1`. Shape: `{"attester":{"id":"..."}, "references":[{"downloadLocation":"...", "digest":{"sha256":"..."}, "mediaType":"..."}]}`. | in-toto libraries, GUAC, OCI registries. [Reference predicate](https://github.com/in-toto/attestation/blob/main/spec/predicates/reference.md). |

### Recommended custom predicate

No existing predicate cleanly models agent-specific change accountability. Add one under a project-controlled URI, for example:

```json
{
  "_type": "https://in-toto.io/Statement/v1",
  "subject": [
    {
      "name": "git+https://example.com/repo",
      "digest": {"gitCommit": "COMMIT_SHA"}
    }
  ],
  "predicateType": "https://example.org/agent-change/v1",
  "predicate": {
    "task": {
      "id": "issue-or-request-id",
      "digest": {"sha256": "task-record-hash"}
    },
    "human": {
      "id": "urn:example:employee:123",
      "role": "requester"
    },
    "agent": {
      "id": "urn:example:agent:orchestrator",
      "version": "1.4.0",
      "sessionId": "..."
    },
    "model": {
      "provider": "openai",
      "name": "model-name",
      "versionOrSnapshot": "snapshot-or-release",
      "requestHash": "sha256:..."
    },
    "authorization": {
      "policy": "https://example.org/policies/agent-change/v3",
      "decision": "ALLOW",
      "approver": {
        "id": "urn:example:employee:456",
        "role": "code-owner"
      },
      "approvedAt": "2026-09-02T12:00:00Z"
    },
    "actions": [
      {
        "type": "write",
        "tool": "git",
        "target": "src/example.ts",
        "before": {"sha256": "..."},
        "after": {"sha256": "..."}
      }
    ],
    "tests": [
      {
        "name": "unit",
        "result": "PASSED",
        "evidence": {
          "uri": "https://ci.example/run/123",
          "digest": {"sha256": "..."},
          "mediaType": "text/plain"
        }
      }
    ],
    "humanReview": {
      "required": true,
      "completed": true,
      "reviewer": "urn:example:employee:456"
    }
  }
}
```

The custom predicate should reference, rather than duplicate, detailed logs. Use the in-toto Test Result predicate for test evidence and SLSA Provenance for build evidence.

## 2. Signing, transparency, and hardware evidence

| Standard | Status and relevant requirement | Machine-readable format | Tooling |
|---|---|---|---|
| Sigstore | CNCF/OpenSSF project; production-grade keyless signing is established. | Fulcio issues short-lived identity certificates binding ephemeral keys to OIDC identities. Cosign signs artifacts or generic predicates. Rekor records signed material and certificates in a transparency log. Sigstore Bundles use the protobuf-defined JSON representation, including message signature, verification material, and transparency-log inclusion proof. | `cosign`, Fulcio, Rekor, `gitsign`, `rekor-cli`, `slsa-verifier`. [Keyless signing](https://docs.sigstore.dev/cosign/signing/overview/), accessed 2026-09-02. |
| gitsign | Sigstore project. | Signs Git commits using an ephemeral key and OIDC identity. Rekor records a `HashedRekord` containing the commit hash and signing certificate. | `gitsign`, Git’s signing hooks, Rekor. [gitsign repository](https://github.com/sigstore/gitsign). |
| Rekor | Sigstore transparency log. | Append-only, publicly auditable records. The receipt or log entry proves that a signature or attestation was registered at a particular time. It does not prove the claim was true. | Rekor REST API, `rekor-cli`, Cosign. [Rekor project](https://github.com/sigstore/rekor). |
| SCITT, RFC 9943 | IETF. RFC 9943 published 2025-10-10. SCITT WG and reference APIs remain active. | A producer signs a statement; a Transparency Service registers it; the service returns a receipt. The ledger provides an auditable history and visibility into issuer statements. | Signed statements use COSE-based structures. Receipt details are specified by SCITT receipt documents and service profiles. Exact media types and API details remain profile-dependent. | SCITT reference implementations and COSE libraries. [RFC 9943](https://www.ietf.org/ietf-ftp/rfc/rfc9943.html), [IETF SCITT status](https://datatracker.ietf.org/group/scitt/). |
| TPM 2.0 and TCG Remote Attestation | Trusted Computing Group. Mature hardware attestation technology. | A TPM signs selected PCR values in a `TPM2_Quote`, accompanied by an event log. A verifier compares measurements with expected reference values. This proves properties of the execution platform, not the intent or quality of code changes. | Binary TPM structures: `TPMS_ATTEST`, `TPMT_SIGNATURE`, PCR selections, and TCG event logs. No in-toto-compatible universal JSON format is mandated. | `tpm2-tools`, Linux IMA, Keylime, EAT/RATS implementations, TCG reference integrity manifests. [TCG attestation overview](https://trustedcomputinggroup.org/wp-content/uploads/Overview-of-TCG-Technologies-for-Device-Identification-and-Attestation-Version-1.0-Revision-1.39.pdf). |
| C2PA Content Credentials 2.2/2.3 | Coalition for Content Provenance and Authenticity. 2.2 is a mature published specification; 2.3 is also published in current documentation. | Analogous provenance model for AI-generated media: assertions describe creation and transformations; claims are signed; ingredients link prior manifests; content bindings bind claims to the asset. It explicitly distinguishes association and tamper evidence from judgments about trustworthiness. | CBOR claims packaged in JUMBF. Claim map includes `claim_generator`, `signature`, assertions, format, and `instanceID`. Signatures use COSE_Sign1. CDDL is normative for claim structures. | `c2pa-rs`, `c2pa-js`, Adobe Content Authenticity tools, `c2patool`. [C2PA 2.2 specification](https://spec.c2pa.org/specifications/specifications/2.2/specs/C2PA_Specification.html). |

C2PA is useful as a design analogy for agent-produced documentation, diagrams, release notes, and other non-code outputs. It should not replace in-toto for software supply-chain attestations.

## 3. Component, model, and dataset inventories

| Standard | Status and relevant requirement | Machine-readable format | Tooling |
|---|---|---|---|
| CycloneDX 1.6 | OWASP CycloneDX; ratified as Ecma-424, 1st edition, in 2024. | Adds CBOM and CycloneDX Attestations, or CDXA. CDXA represents requirements, claims, evidence, assessors, conformance, confidence, and mitigation strategies. | JSON Schema: `https://cyclonedx.org/schema/bom-1.6.schema.json`; XML Schema and Protobuf also exist. | `cyclonedx-cli`, `cyclonedx-python-lib`, Syft, cdxgen, Dependency-Track. [CDXA](https://cyclonedx.org/capabilities/attestations/), [v1.6 release](https://cyclonedx.org/news/cyclonedx-v1.6-released/). |
| CycloneDX 1.7 | OWASP CycloneDX; published October 2025 and listed as current stable BOM version in 2026. | Extends AI/ML-BOM, model cards, formulations, declarations, citations, and attestation maps. It can express how software, models, datasets, and workflows were created, tested, assembled, or certified. | JSON Schema: `https://cyclonedx.org/schema/bom-1.7.schema.json`. Relevant structures include `metadata`, `components`, `formulation`, `declarations`, `modelCard`, and `declarations.attestations[].map`. | `cyclonedx-cli`, cdxgen, Syft, Dependency-Track. [v1.7 reference](https://cyclonedx.org/docs/1.7/proto/), accessed 2026-09-02. |
| SPDX 3.0/3.0.1 AI and Dataset profiles | SPDX Workgroup/Linux Foundation. SPDX 3.0 released 2024; 3.0.1 is published and stable. | AI profile documents AI systems and models, including capabilities, limitations, training methods, data handling, explainability, and energy use. Dataset profile documents dataset type, size, collection, preparation, intended use, quality, and privacy. | JSON-LD is the primary extensible machine form. Namespace for 3.0.1 AI profile: `https://spdx.org/rdf/3.0.1/terms/AI`. Dataset profile uses the corresponding Dataset namespace. SPDX also provides RDF, JSON-LD, YAML, and other serializations. | SPDX tools, `spdx-tools`, `spdx-java`, `spdx-python`, Fossology, ORT. [SPDX specifications](https://spdx.dev/use/specifications/), [SPDX 3 model](https://github.com/spdx/spdx-3-model). |
| OpenSSF Scorecard | OpenSSF project; active, with stable checks and newer structured-result work. | Evaluates repository security practices: branch protection, code review, CI tests, signed releases, release provenance, pinned dependencies, token permissions, and vulnerabilities. It is evidence about repository posture, not an attestation format for an individual agent action. | JSON output and structured results. The exact schema is Scorecard-version-specific; no stable predicate URI comparable to SLSA is defined. | `scorecard`, Scorecard GitHub Action, OSSF dashboards. [Checks](https://github.com/ossf/scorecard), [probe documentation](https://github.com/ossf/scorecard/blob/main/docs/probes.md). |
| OpenSSF SBOM work | OpenSSF SBOM Everywhere and SBOM Operations workgroups are active. | Promotes SBOM generation, exchange, lifecycle management, signatures, and operational consumption. Public OpenSSF work does not establish a separate canonical AI-BOM predicate. | Use SPDX or CycloneDX, then sign with in-toto/Sigstore. OpenSSF materials generally assume a machine-readable SBOM with an associated signature. | Syft, cdxgen, SPDX tools, GUAC, Dependency-Track. [OpenSSF SBOM material](https://openssf.org/wp-content/uploads/2025/09/Improving_Risk_Management_Decisions_with_SBOM_Data.pdf). |
| Model cards, datasheets, and system cards | Research and industry documentation patterns, not a single ratified standards body. | Model cards document intended use, limitations, evaluation, biases, and performance. Datasheets document dataset motivation, composition, collection, preparation, and recommended uses. System cards describe system-level safety and evaluation properties. | No universal schema or predicate URI. Usually Markdown, HTML, YAML, or JSON. CycloneDX and SPDX can carry links or selected structured fields. | Hugging Face model cards, model registry systems, custom templates. [Model Cards paper](https://arxiv.org/abs/1810.03993), [Datasheets paper](https://arxiv.org/abs/1803.09010). |

## 4. Secure development and audit controls

| Standard | Status and relevant requirement | Machine-readable format | Tooling |
|---|---|---|---|
| NIST SP 800-218 SSDF v1.1 | Final, published February 2022. | PS: protect software and development infrastructure. PW: produce well-secured software, including security requirements, design decisions, code review, testing, and release integrity. RV: identify and remediate residual vulnerabilities. The practices are control objectives, not attestation schemas. | No native SSDF attestation format. Map practice IDs to in-toto predicates, test results, SBOM/VEX documents, signed CI records, and OSCAL components. | NIST SSDF tables, OSCAL mappings, CI/CD security tools. [SP 800-218](https://csrc.nist.gov/pubs/sp/800/218/final). |
| NIST SP 800-218A | NIST SSDF Community Profile for generative AI and dual-use foundation models. Final according to NIST’s SSDF project page. | Adds AI-specific lifecycle practices for model data, training, evaluation, deployment, monitoring, provenance, and risks. | No native schema. Use SPDX/CycloneDX AI and Dataset profiles, model cards, in-toto, and OSCAL mappings. | NIST SSDF and AI profile materials. [NIST SSDF project](https://csrc.nist.gov/projects/ssdf). |
| NIST SP 800-53 Rev. 5, AU family | NIST control catalog, with current control data maintained in machine-readable formats. | AU controls require event logging, content sufficient for review, centralized analysis where appropriate, protection from unauthorized modification, retention, and audit review. | OSCAL catalog/control JSON, XML, or YAML. | OSCAL tools, NIST control catalogs, SIEM systems. [NIST controls](https://csrc.nist.gov/Projects/risk-management/sp800-53-controls/downloads). |
| NIST SP 800-53 CM family, especially CM-3 | Current control catalog includes later maintenance releases. | CM-3 requires configuration change control, including documenting proposed changes, analyzing security and privacy impact, approving or disapproving changes, documenting decisions, implementing approved changes, and retaining records. | OSCAL control implementations and assessment results. | OSCAL, change-management systems, CI/CD gates, Git protections. [SP 800-53 Rev. 5](https://csrc.nist.gov/pubs/sp/800/53/r5/upd1/final). |
| FedRAMP Rev5 and FedRAMP 20x | Rev5 remains the conventional path. FedRAMP 20x was in Phase 3 and had 2026 consolidated rules as of 2026-09-02. | 20x emphasizes continuously maintained, machine-readable certification data, automated validation, reusable evidence, and Key Security Indicators. It is explicitly evidence-oriented but does not define one agent-attestation predicate. | OSCAL is the established Rev5 machine format. FedRAMP 20x uses machine-readable certification data and Key Security Indicator structures; exact schemas are program-specific and evolving. | OSCAL tools, FedRAMP package tooling, GRC integrations, automated evidence collectors. [FedRAMP 20x](https://www.fedramp.gov/20x/), [2026 package rules](https://www.fedramp.gov/2026/agencies/use/packages/20x/), [OSCAL RFC](https://www.fedramp.gov/rfcs/0024/). |
| SOC 2 Trust Services Criteria | AICPA. Current public resources refer to 2017 TSC with 2022 revised points of focus and 2018 description criteria with 2022 implementation guidance. | CC6: logical access and authentication. CC7: system operations, detection, and monitoring. CC8: change management, including authorization, testing, implementation, and documentation of changes. SOC 2 requires auditor evidence, not a public interchange schema. | No canonical machine-readable SOC 2 evidence format. Evidence is typically tickets, approvals, Git history, CI results, deployment logs, access reviews, and auditor-selected samples. | Vanta, Drata, Secureframe, AuditBoard, Jira/GitHub/GitLab integrations. [AICPA SOC 2 resources](https://www.aicpa-cima.com/topic/audit-assurance/audit-and-assurance-greater-than-soc-2/). |
| SOX ITGC change management | SEC/PCAOB financial-control regime. Requirements are risk- and auditor-driven rather than a software serialization standard. | Common ITGC evidence includes request and business rationale, authorized approval, developer/tester separation, testing evidence, migration/deployment authorization, and emergency change review. Segregation of duties generally prevents one person from developing, approving, and deploying the same change. | No canonical format. Use immutable ticket records, Git/forge audit logs, signed approvals, CI artifacts, and deployment logs. | GRC platforms, Jira/ServiceNow, GitHub/GitLab protected branches, CI/CD systems, SIEM. |
| ISO/IEC 27001:2022 Annex A 8.15, 8.28, 8.32 | ISO/IEC standard, normally accessed through licensed publication. | 8.15 logging: record, protect, monitor, and review relevant events. 8.28 secure coding: apply secure coding principles. 8.32 change management: changes must follow controlled procedures, including authorization, testing, impact assessment, and records. | No native public attestation schema. Use ISMS evidence, change records, signed CI/CD artifacts, test predicates, and OSCAL mappings where useful. | ISO audit platforms, SIEM, Git controls, CI/CD, GRC tools. |
| PCI DSS v4.0.1 Requirement 6.5 | PCI SSC. v4.0.1 published June 2024 and contains corrections and clarifications, not new requirements. | 6.5.1 requires reason and description, security-impact documentation, authorized approval, testing showing no adverse security impact, secure failure/rollback procedures, and pre-deployment testing for custom software. 6.5.2 requires post-significant-change confirmation and documentation. 6.5.3 requires pre-production separation. | No PCI-specific attestation schema. Use signed in-toto test and change predicates plus ticket and deployment evidence. | PCI ROC/SAQ evidence, change systems, CI/CD, vulnerability scanners. [PCI v4.0.1 announcement](https://blog.pcisecuritystandards.org/just-published-pci-dss-v4-0-1), [Requirement 6.5 example](https://www.pcisecuritystandards.org/documents/PCI-DSS-v4-0-SAQ-D-Merchant.pdf). |
| DORA, EU Regulation 2022/2554 and Delegated Regulation 2024/1774 | EU Digital Operational Resilience Act. Applicable obligations are being phased in; the delegated technical standards provide operational detail. | Major changes require risk assessment. ICT change management must ensure changes are recorded, tested, assessed, approved, implemented, and verified in a controlled manner. Logging procedures must identify events, retention, protection, and detail sufficient to detect anomalous activity. | No DORA-native attestation format. Use signed change, approval, test, deployment, and logging evidence, with OSCAL or in-toto as transport. | GRC platforms, SIEM, ITSM, CI/CD. [DORA Regulation](https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX%3A32022R2554), [Delegated Regulation 2024/1774](https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX%3A32024R1774). |

## 5. DevOps metrics and platform-native evidence

| Item | What it provides | Format and limitation |
|---|---|---|
| DORA software delivery metrics | Deployment frequency, change lead time, failed deployment recovery time, change fail rate, and deployment rework rate. These measure delivery performance and instability, not authorship or authorization. | Usually derived from Git, CI/CD, deployment, and incident data. No canonical attestation schema. [DORA metrics guide](https://dora.dev/guides/dora-metrics/), updated 2026-01-05. |
| GitHub artifact attestations | GitHub can bind artifacts to workflow-run provenance and SBOM attestations. Cloud-agent commits include a Copilot author, initiating human as co-author, signed commits, and session-log links. | in-toto Statements, generally SLSA provenance, stored and verified through GitHub’s artifact-attestation system and Sigstore-compatible tooling. [GitHub artifact attestations](https://docs.github.com/en/actions/how-tos/secure-your-work/use-artifact-attestations/use-artifact-attestations), [agent sessions](https://docs.github.com/en/enterprise-cloud%40latest/copilot/how-tos/copilot-on-github/use-copilot-agents/manage-and-track-agents). |
| GitHub Copilot enterprise audit logs | Agent events include action, `actor_is_agent`, `agent_session_id`, initiating `user`, and streamed API usage records with request/response type, user, endpoint, body, timestamp, event ID, and GitHub request ID. | JSON audit events and streamed records. They are operational audit logs, not signed in-toto attestations. Agent audit activity has a 180-day viewing window in the documented feature. [GitHub agent audit events](https://docs.github.com/en/copilot/reference/enterprise-administrators/agentic-audit-log-events), accessed 2026-09-02. |
| GitLab Duo and GitLab attestations | GitLab Duo agent sessions generate AI audit events containing inputs, model context, event timeline, and outputs. GitLab also exposes experimental provenance attestations using SLSA provenance. | AI audit events are GitLab audit-event records. Provenance uses `https://slsa.dev/provenance/v1`; GitLab’s Attestations API was marked Experiment in the current documentation. [GitLab audit events](https://docs.gitlab.com/user/compliance/audit_events/), [Attestations API](https://docs.gitlab.com/api/attestations/). |
| Anthropic Claude Enterprise and Claude Code | Enterprise audit logs are exportable for organization events. The Compliance API returns retained per-session transcripts; Claude Code monitoring and analytics can stream per-event telemetry including token, cost, and host metadata. | Anthropic-specific CSV, API, and telemetry formats. Public documentation does not establish a signed in-toto predicate or a complete public schema for every Claude Code event. [Anthropic audit logs](https://support.claude.com/en/articles/9970975-access-audit-logs), [Compliance API](https://platform.claude.com/docs/en/manage-claude/compliance-api). |
| OpenAI ChatGPT Enterprise/Edu Compliance API | Provides workspace compliance data, including supported audit, authentication, app, and conversation-log categories, subject to plan and admin permissions. | OpenAI Admin/Compliance API objects. Public documentation does not establish a coding-agent-specific signed provenance predicate. [OpenAI Compliance Platform](https://help.openai.com/en/articles/9261474-compliance-apis-for). |
| OpenTelemetry GenAI semantic conventions | Standardizes telemetry attributes for model calls, embeddings, retrieval, tool execution, sessions, inputs, outputs, and errors. Useful for detailed agent traces and correlating actions with a session. | OTLP spans, logs, and metrics, usually protobuf or JSON. GenAI conventions were still marked Development in the current repository, so attribute names may change. They are not cryptographic attestations. [GenAI spans](https://github.com/open-telemetry/semantic-conventions-genai/blob/main/docs/gen-ai/gen-ai-spans.md), accessed 2026-09-02. |

## 6. AI-agent governance and observability products

Public product claims show a common table-stakes bundle: agent inventory, owner and identity, model and tool context, policy decisions, action-level telemetry, human escalation, immutable or tamper-evident evidence, and mappings to SOC 2, ISO 27001, NIST, EU AI Act, or similar controls.

| Product/category | Publicly claimed artifact or capability | Standardization status |
|---|---|---|
| Credo AI / Agent Governor | Agent registry, executable policies, runtime enforcement, policy decisions, blocked actions, governance telemetry, and audit-ready evidence. Agent Governor was described as Research Preview/Beta and initially focused on Claude Code. | Vendor-specific governance events. No public in-toto predicate or signed evidence format confirmed. [Agent Governor](https://www.credo.ai/agent-governor), accessed 2026-09-02. |
| Holistic AI | Discovery of models, APIs, agents, and pipelines; runtime controls; incident logs; audit trails; risk and compliance monitoring. | Vendor-specific logs and evidence. No public portable attestation schema confirmed. [Holistic AI](https://go.holisticai.com/), accessed 2026-09-02. |
| Drata Agent Governance | Agent discovery, owner/identity/permissions/scope mapping, pre-action policy enforcement, and tamper-evident evidence trails. Limited availability. | Likely GRC evidence records plus vendor telemetry; no public signed predicate confirmed. [Drata](https://drata.com/products/agent-governance), accessed 2026-09-02. |
| Vanta AI governance | Maps agents across developer laptops, production code, and vendor platforms to a Trust Graph; records model, prompt, data, access, and risk context. | Trust Graph and compliance evidence are vendor-specific; no public portable attestation format confirmed. [Vanta](https://www.vanta.com/products/ai-governance), accessed 2026-09-02. |
| Lasso | Secure-AI platform focused on discovering and protecting enterprise AI use and agent risks. | Public page reviewed did not confirm a specific signed audit artifact or schema. [Lasso](https://www.lasso.security/), accessed 2026-09-02. |
| Noma, Zenity, Kiln | These names are associated with agent security, governance, evaluation, or deployment tooling in the 2026 market. | I did not confirm, from primary public documentation during this survey, a stable interoperable evidence schema for coding-agent authorship and approval. Treat product claims as unconfirmed until API/export documentation is obtained. |
| Langfuse | Open-source LLM observability with traces, sessions, timelines, users, agent graphs, prompt/version tracking, evaluations, and dashboards. | Internal trace model and APIs; not a cryptographic attestation format. [Langfuse](https://langfuse.com/docs), accessed 2026-09-02. |
| AgentOps | Agent observability category and research taxonomy covering traces, monitoring, logging, analytics, and lifecycle artifacts. | No single industry-wide AgentOps serialization standard confirmed. [AgentOps taxonomy paper](https://arxiv.org/abs/2411.05285). |
| OpenTelemetry | Vendor-neutral runtime telemetry for model calls and tool executions. | Best common transport for detailed event traces, but Development GenAI semantics and unsigned telemetry mean it should be referenced by signed attestations rather than treated as proof by itself. |

## 7. Smallest practical format set

An open-source orchestrator can cover most of the standards above with four emitted artifacts:

### A. Signed agent-change attestation

An in-toto Statement with:

```text
_type: https://in-toto.io/Statement/v1
predicateType: https://example.org/agent-change/v1
subject: exact commit or merge result digest
predicate:
  human requester and reviewer
  agent identity and version
  model provider, name, version/snapshot, request hash
  task and policy identifiers
  approval decision and timestamps
  tool/action summary
  changed-file before/after digests
  references to test, review, and deployment evidence
```

Wrap it in DSSE and sign with Cosign/Sigstore. Require the human approval signature to be distinct from the agent identity.

### B. SLSA Provenance attestation

Use:

```text
predicateType: https://slsa.dev/provenance/v1
```

Include the repository revision, build definition, resolved dependencies, builder identity, build parameters, environment claims, and produced artifact digests.

### C. Test-result attestation

Use:

```text
predicateType: https://in-toto.io/attestation/test-result/v0.1
```

Record the test configuration, overall result, named suites, CI URL, and digest of the complete test report.

### D. Inventory and trace references

Emit either:

- CycloneDX 1.7 for software, models, datasets, formulations, and CDXA declarations; or
- SPDX 3.0.1 for organizations already using SPDX AI/Dataset profiles.

Reference the SBOM, model card, dataset card, logs, screenshots, and full OpenTelemetry trace from the signed in-toto predicate using digests.

### Signing and storage

Use:

```text
DSSE-signed in-toto Statement
  -> Sigstore Bundle
  -> Rekor entry or SCITT receipt
  -> OCI registry, artifact store, or transparency service
```

Use Rekor for direct Sigstore interoperability. Support SCITT as an optional enterprise or jurisdictional transparency backend. The transparency receipt proves registration and ordering, not the truth of the underlying claim.

## What this set proves

With correct identity and policy controls, the system can prove:

- which repository object changed;
- which human initiated and approved the work;
- which agent and model session generated or applied it;
- which tools and files were involved;
- which policy authorized the action;
- whether approval and deployment duties were separated;
- which tests ran against which digest;
- which builder produced the released artifact;
- which model, dataset, and software components were present;
- whether the signed evidence was logged without later undetected alteration.

It cannot independently prove that a model’s reasoning was correct, that a human meaningfully reviewed every line, or that an approved test suite was sufficient. Those remain policy and control-design questions.

## Source list

- [SLSA v1.2 specification](https://slsa.dev/spec/v1.2/), accessed 2026-09-02.
- [SLSA Build Track basics](https://slsa.dev/spec/v1.2/build-track-basics), accessed 2026-09-02.
- [SLSA provenance](https://slsa.dev/spec/v1.2/build-provenance), accessed 2026-09-02.
- [SLSA source requirements](https://slsa.dev/spec/v1.2/source-requirements), accessed 2026-09-02.
- [in-toto Attestation Framework](https://github.com/in-toto/attestation), release v1.2.0 dated 2026-03-18.
- [in-toto SCAI predicate](https://github.com/in-toto/attestation/blob/main/spec/predicates/scai.md), accessed 2026-09-02.
- [in-toto Test Result predicate](https://github.com/in-toto/attestation/blob/main/spec/predicates/test-result.md), accessed 2026-09-02.
- [Sigstore keyless signing](https://docs.sigstore.dev/cosign/signing/overview/), accessed 2026-09-02.
- [Rekor](https://github.com/sigstore/rekor), accessed 2026-09-02.
- [CycloneDX Attestations](https://cyclonedx.org/capabilities/attestations/), accessed 2026-09-02.
- [CycloneDX v1.6 release](https://cyclonedx.org/news/cyclonedx-v1.6-released/), 2024-04-09.
- [CycloneDX v1.7 Protobuf reference](https://cyclonedx.org/docs/1.7/proto/), accessed 2026-09-02.
- [SPDX specifications](https://spdx.dev/use/specifications/), accessed 2026-09-02.
- [SPDX 3 model](https://github.com/spdx/spdx-3-model), accessed 2026-09-02.
- [OpenSSF Scorecard](https://github.com/ossf/scorecard), accessed 2026-09-02.
- [NIST SP 800-218 SSDF v1.1](https://csrc.nist.gov/pubs/sp/800/218/final), 2022-02.
- [NIST SSDF project and SP 800-218A](https://csrc.nist.gov/projects/ssdf), accessed 2026-09-02.
- [NIST SP 800-53 control downloads](https://csrc.nist.gov/Projects/risk-management/sp800-53-controls/downloads), accessed 2026-09-02.
- [FedRAMP 20x](https://www.fedramp.gov/20x/), accessed 2026-09-02.
- [FedRAMP 2026 package rules](https://www.fedramp.gov/2026/agencies/use/packages/20x/), accessed 2026-09-02.
- [PCI DSS v4.0.1 announcement](https://blog.pcisecuritystandards.org/just-published-pci-dss-v4-0-1), 2024-06-11.
- [DORA Regulation 2022/2554](https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX%3A32022R2554), accessed 2026-09-02.
- [DORA Delegated Regulation 2024/1774](https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX%3A32024R1774), accessed 2026-09-02.
- [SCITT RFC 9943](https://www.ietf.org/ietf-ftp/rfc/rfc9943.html), published 2025-10-10.
- [TCG attestation overview](https://trustedcomputinggroup.org/wp-content/uploads/Overview-of-TCG-Technologies-for-Device-Identification-and-Attestation-Version-1.0-Revision-1.39.pdf), accessed 2026-09-02.
- [C2PA 2.2 specification](https://spec.c2pa.org/specifications/specifications/2.2/specs/C2PA_Specification.html), accessed 2026-09-02.
- [DORA software delivery metrics](https://dora.dev/guides/dora-metrics/), updated 2026-01-05.
- [GitHub agent audit events](https://docs.github.com/en/copilot/reference/enterprise-administrators/agentic-audit-log-events), accessed 2026-09-02.
- [GitLab audit events](https://docs.gitlab.com/user/compliance/audit_events/), accessed 2026-09-02.
- [Anthropic audit logs](https://support.claude.com/en/articles/9970975-access-audit-logs), updated 2026-06-15.
- [OpenAI Compliance Platform](https://help.openai.com/en/articles/9261474-compliance-apis-for), accessed 2026-09-02.
