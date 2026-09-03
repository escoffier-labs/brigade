## Evaluation of Proposed Evidence Features

### Issue #1404: Public-Key Signing and in-toto/DSSE Attestation Export

#### 1. Acceptance as Audit Evidence
* **ISO/IEC 42001 (Lead Auditor):** Conditionally accepted[cite: 1, 2]. Under **A.6.2.8** (logging) and **A.6 lifecycle controls**, an auditor will sample receipts corresponding to deployed production changes[cite: 1]. **Rejection condition:** If `digests.pubkey_id` is an untrusted local SSH key without an organizational public-key infrastructure (PKI) binding or proof of hardware-backed custody (e.g., FIDO2/PIV or corporate KMS)[cite: 2], it fails non-repudiation and will be rejected.
* **SOC 2 Type II (CC8.1, CC6.6, CC6.8):** Conditionally accepted[cite: 1, 2]. Sampling will target 25–40 production merges over the audit period, verifying matching attestations for test passes and change lineage[cite: 2]. **Rejection condition:** If the signature is verified via an ad-hoc local key file (`ssh-keygen -Y sign`) stored on an ephemeral worker VM without key-lifecycle controls[cite: 2], the evidence fails **CC6.6** (logical access controls over cryptographic keys).
* **EU AI Act Counsel (Art. 12, Art. 14):** Accepted for integrity[cite: 1, 2]. Under **Art. 12(1)** (automatic logging to ensure traceability)[cite: 1], DSSE-wrapped attestations demonstrate tamper-evident execution[cite: 1, 2]. **Rejection condition:** If the attestation’s `subject` digest points to an uncommitted git tree rather than an immutable, traceable git commit SHA deployed to production[cite: 2], it fails traceability requirements under **Art. 12(2)**[cite: 1].

#### 2. Technical Evaluation
* **Fields:** The mapping of verify receipts to `[https://in-toto.io/attestation/test-result/v0.1](https://in-toto.io/attestation/test-result/v0.1)` is clean[cite: 1, 2], but `agent-change/v1` requires an explicit correlation field linking the `BRIGADE_RUN_ID` to an upstream tracking issue/pull-request identifier to satisfy **SOC 2 CC8.1** (change ticket authorization)[cite: 1, 2].
* **Key Management & Identity:** Relying on `ssh-keygen -Y` relies on a static `allowed_signers` file[cite: 2]. The issue lacks a mechanism for key rotation, revocation (e.g., KRLs), or binding the key to an authenticated IAM/corporate identity.
* **Over-engineered / Missing:** Calling `ssh-keygen` or `minisign` via subprocess preserves zero Python dependencies[cite: 2], but omitting Sigstore keyless OIDC token extraction when running in CI/CD leaves enterprise customers without identity attribution.

---

### Issue #1405: First-Class Human Approval Lifecycle Event

#### 1. Acceptance as Audit Evidence
* **ISO/IEC 42001 (Lead Auditor):** Accepted for **A.9 (Responsible Use)** and **Clause 8.3 (Treatment records)**[cite: 1]. The auditor will sample run journals where consequential code was generated, checking for the `approval` event prior to commit synthesis[cite: 1, 3]. **Rejection condition:** Rejection occurs if `approver_kind: "seat"` is used on high-risk pipelines without human intervention[cite: 3], or if `reason` is empty/boilerplate.
* **SOC 2 Type II (CC8.1, CC6.3):** Highly accepted and mandatory[cite: 1, 3]. Change management requires proof that the author cannot approve their own pull request / change. The auditor will inspect population samples specifically looking for self-approvals[cite: 3]. **Rejection condition:** If `approver_id` matches the operator or agent identity that initiated/dispatched the run[cite: 3], or if the approval signature key is identical to the agent’s execution key[cite: 3].
* **EU AI Act Counsel (Art. 14):** Critical evidence[cite: 1, 3]. **Art. 14(4)** requires human overseers to have the ability to "interrupt, override, or reverse" system operation[cite: 1, 3]. **Rejection condition:** A recorded `hold` or `deny` that does not halt the pipeline, or an approval executed automatically via an autonomous agent seat without an identifiable human deployer[cite: 3].

#### 2. Technical Evaluation
* **Fields:** The schema needs a `timeout_or_expiry` field and an explicit `change_hash` binding (the exact tree/patch SHA approved, not just `run_id`), preventing Time-of-Check to Time-of-Use (TOCTOU) modifications between approval and final merge.
* **Key Management & Identity:** The check "approving identity must differ from every producing seat" is weak if both keys belong to the same local developer environment (`id_ed25519_agent` and `id_ed25519_human`)[cite: 3].
* **Over-engineered / Missing:** Allowing `approver_kind: "seat"` inside an approval control defeats the core intent of human-in-the-loop compliance; automated gates belong in verification receipts, not the approval lifecycle[cite: 3, 7].

---

### Issue #1406: Control Crosswalk and `brigade evidence controls`

#### 1. Acceptance as Audit Evidence
* **ISO/IEC 42001 (Lead Auditor):** Informational only; not direct audit evidence[cite: 4]. The crosswalk assists during the audit opening meeting for document discovery, but auditors audit the underlying records (e.g., journals, verify manifests)[cite: 1, 7], not self-asserted mapping tables[cite: 4]. **Rejection condition:** If the CLI claims a control is "evidenced" when only a blank template or failed check exists[cite: 4].
* **SOC 2 Type II:** Not accepted as operational control evidence[cite: 4]. A crosswalk is a management mapping document[cite: 1, 4]. Auditors sample raw population listings from Git/CI systems.
* **EU AI Act Counsel:** Useful for **Annex IV (Technical Documentation)** compliance files[cite: 1], but carries no evidentiary weight for **Art. 12** or **Art. 17 (QMS)** without raw underlying artifacts[cite: 1].

#### 2. Technical Evaluation
* **Fields:** The proposed schema maps receipt families to control IDs[cite: 4]. It lacks an evidentiary state descriptor (e.g., `not_applicable`, `untested`, `evidenced_passed`, `evidenced_failed`).
* **Key Management & Retention:** N/A (static mapping table)[cite: 4].
* **Over-engineered / Missing:** Substantial risk of maintenance drift. As standards release updates (e.g., NIST AI RMF revisions, CSA AICM updates)[cite: 1], hardcoded mappings in the CLI binary decay rapidly unless backed by versioned external JSON schemas or OSCAL component definitions[cite: 1, 4].

---

### Issue #1407: Commit Trailer, Retention Floor, and Journal Export

#### 1. Acceptance as Audit Evidence
* **ISO/IEC 42001 (Lead Auditor):** High value[cite: 5]. Satisfies **A.6.2.8** (retention and protection of logs)[cite: 1, 5]. Sampling will check whether logs for a random production release from 180 days ago can be fetched and cryptographic linkages verified. **Rejection condition:** If `brigade work prune` can be bypassed via local environment variables or forced CLI flags on the host writing the files.
* **SOC 2 Type II (CC7.2, CC8.1):** Accepted[cite: 1, 5]. The `Brigade-Receipt` trailer provides the end-to-end audit trail from production Git commit to the verify receipt[cite: 5, 7]. **Rejection condition:** If SIEM export cannot prove transmission integrity or if local developers can rewrite history (`git commit --amend` dropping trailers).
* **EU AI Act Counsel (Art. 12, Art. 26):** Crucial[cite: 1, 5]. **Art. 26(5)** requires deployers of high-risk AI systems to keep automatically generated logs for a period appropriate to the intended purpose (at least six months)[cite: 1, 5]. **Rejection condition:** Any default retention setting below 183 days or the inability to export logs in a format suitable for regulatory inspection[cite: 5].

#### 2. Technical Evaluation
* **Fields:** Good alignment with GitHub's pattern (`Brigade-Run` and `Brigade-Receipt`)[cite: 1, 5]. Needs an explicit `Repository-ID` or remote URI binding in the trailer to prevent cross-repo receipt reuse collisions.
* **Key Management & Retention:** Implementing the retention floor inside the client tool is an architectural anti-pattern for audit defense. A developer or local script with file write permissions can execute `rm -rf .brigade/work` regardless of `minimum_days`[cite: 5, 7].
* **Over-engineered / Missing:** Mapping to OTel GenAI span names for batch SIEM export is somewhat over-engineered when standard newline-delimited JSON (JSONL) with signed batch manifests is what enterprise SIEMs (Splunk, Datadog) actually index for compliance log retention[cite: 1, 5].

---

### Issue #1408: Governance Inventory (`agent-registry`, `model-provider`, `tools/MCP`)

#### 1. Acceptance as Audit Evidence
* **ISO/IEC 42001 (Lead Auditor):** Directly accepted and mandatory under **A.4.2** (AI inventory), **A.6.2.1**, and **A.10.1** (third-party/supplier mapping)[cite: 1, 6]. Auditors will inspect the generated inventory against live configuration during fieldwork[cite: 6]. **Rejection condition:** Absence of model version/revision tracking (e.g., stating `claude-3-5-sonnet` instead of pinned checkpoint revisions) or omission of dynamic MCP servers loaded at runtime[cite: 1, 6].
* **SOC 2 Type II (CC6.1, CC6.6):** Accepted as an inventory asset management control[cite: 6]. **Rejection condition:** Discrepancies between tool permissions declared in `tools.toml` and actual system capabilities assigned to worker processes[cite: 6, 7].
* **EU AI Act Counsel (Annex IV, Art. 13):** High evidentiary value for the technical documentation technical file[cite: 1]. **Rejection condition:** If third-party model providers fail to record geographical data egress routes or whether input prompts are used for downstream provider model training.

#### 2. Technical Evaluation
* **Fields:** Emitting CycloneDX 1.7 ML-BOM is standard[cite: 1, 6]. The schema needs an explicit binding to active API credential scopes and token permission ceilings.
* **Key Management & Identity:** Inventories are unsigned state snapshots[cite: 6]. To serve as non-repudiable audit evidence, the generated inventory artifact must be signed by the orchestrator at run closeout.
* **Over-engineered / Missing:** Generating three separate JSON documents plus CycloneDX creates synchronization debt[cite: 6]. A single canonical CycloneDX 1.7 ML-BOM document containing components (models), services (MCP tools), and actors (agent seats) is cleaner and directly aligns with software supply chain tools[cite: 1, 6].

---

## Known Pitfalls & Auditor Scrutiny

### 1. `ssh-keygen -Y sign` vs. Sigstore Keyless
* **Auditor Acceptance:** `ssh-keygen -Y` produces raw cryptographic signatures, but from an evidentiary perspective, an auditor cannot confirm *who* held the key at the timestamp of signing without a rigorous key management lifecycle policy (CC6.6).
* **The Keyless Advantage:** Sigstore Keyless ties the signature to an OpenID Connect (OIDC) identity token (e.g., corporate Okta/Google workspace or GitHub Actions workflow runner) and includes an automated cryptographic timestamp via a transparency log (Rekor).
* **Verdict:** For enterprise audits, SSH signatures are accepted only if accompanied by an explicit, audited `allowed_signers` management procedure and Git commit signing policy. Sigstore is preferred for zero-friction non-repudiation.

### 2. DSSE Without a Transparency Log
* A DSSE envelope signs payload bytes and a MIME type (`application/vnd.in-toto+json`), binding the payload to an identity[cite: 1]. Without an immutable transparency log (Rekor or SCITT RFC 9943)[cite: 1], DSSE signatures provide **integrity**, but not **temporal non-repudiation**.
* An operator can backdate signatures or sign multiple divergent statements after the fact. Auditors will require external timestamp evidence (e.g., Git commit timestamp countersigned by a remote hosting forge or an RFC 3161 timestamp authority) to corroborate DSSE statements created offline.

### 3. Segregation of Duties (SoD) Pitfall
* **The Failure Mode:** If a developer runs Brigade locally, generating code with their personal agent seat and executing `brigade run approve` using their own developer SSH key, there is **zero segregation of duties**[cite: 3, 7].
* **Auditor Stance:** A SOC 2 or SOX ITGC auditor will flag this as a critical control deficiency under CC8.1.
* **Remediation:** To pass an audit, SoD rules must be verified upstream at the code hosting platform (e.g., branch protection rules requiring an independent peer review before merge) or Brigade must require approval signatures to match an authorized approver identity distinct from the author's identity provider (IdP) account[cite: 3].

### 4. Retention Floor in the Authoring Tool
* **Auditor Stance:** An auditor will **not** accept a retention policy enforced strictly within a client CLI (`brigade work prune`) as an effective control for **ISO 27001 A.8.15** or **EU AI Act Art. 26**[cite: 1, 5].
* **Reasoning:** Local controls lack tamper-resistance; any developer or compromised subprocess can execute `rm -rf .brigade/` or edit `.brigade/work/config`[cite: 5, 7].
* **Requirement:** Retention compliance requires streaming logs to an immutable, external location (e.g., S3 Object Lock in compliance mode, central SIEM, or WORM storage) with separate administrative access controls.

### 5. Ad-Hoc Verification Receipts vs. Tracked Manifests
* Under the receipt schema contract, ad-hoc `--command` runs omit `subject_binding` and remain non-scoreable[cite: 7].
* **Auditor Stance:** Ad-hoc verification receipts are **rejected** as formal audit evidence for change verification (SOC 2 CC8.1 / SSDF PW.8)[cite: 1, 7]. Because ad-hoc runs do not track the verifier manifest digest (`manifest_binding.payload_sha256`)[cite: 7], a malicious actor or buggy agent could run `true` or an assertion-free test script and generate a `PASSED` receipt[cite: 1, 7]. Only tracked manifests under version control provide reproducible, admissible evidence[cite: 7].

---

## 2026 GRC & Supply-Chain Ingestion Landscape

| Tool | Can Ingest As-Is? | Required Format / Ingestion Path |
| :--- | :--- | :--- |
| **Vanta** | **No** (Direct) / **Yes** (API/Custom) | Ingests via Vanta Custom Evidence API or Custom Tests. Expects JSON metadata linked to a monitored Account/Host, or formatted PDF/CSV audit summaries. Cannot parse raw DSSE envelopes directly. |
| **Drata** | **No** (Direct) / **Yes** (API) | Requires pushing JSON payloads via Drata Public API (`/v1/workspaces/{id}/custom-evidence`) mapped to specific internal control IDs. Does not natively parse in-toto statements. |
| **Secureframe** | **No** | Ingests via API or webhook integrations as raw evidence attachments (PDF/JSON text) linked to tests. |
| **AuditBoard** | **No** | Requires flat CSV/XLSX evidence packages or generic file uploads to OpsAudit/CrossComply requests. |
| **Dependency-Track** | **Yes** (Partial) | Accepts **CycloneDX 1.5/1.6/1.7** XML or JSON via REST API (`/api/v1/bom`). Ingests `agent-registry` and tools if exported as standard components/services[cite: 1, 6]. Rejects in-toto DSSE receipts. |
| **GUAC** | **Yes** | Natively ingests in-toto statements wrapped in DSSE (`application/vnd.in-toto+json`), SLSA provenance, and CycloneDX BOMs via the GUAC collector (`guacone collect files`)[cite: 1, 2]. |
| **OSCAL Importers** | **No** (for raw Brigade receipts) | Importers (e.g., ComplianceAsCode, FedRAMP tooling) require strictly validated OSCAL `assessment-results` or `component-definition` JSON/XML[cite: 1, 4]. Brigade must convert its crosswalk/receipts into valid OSCAL model instances[cite: 1, 4]. |

---

## Issue Re-Ranking (Value per Unit of Effort)

1. **Rank 1: Issue #1405 (Human Approval Lifecycle Event)**
   * **Justification:** Highest compliance ROI[cite: 1, 3]. Without distinct human authorization, every autonomous coding action fails basic change management (SOC 2 CC8.1, PCI DSS 6.5.1, EU AI Act Art. 14)[cite: 1, 3]. The effort is modest (one event kind and a CLI hook)[cite: 3], but it unlocks deployment into regulated environments[cite: 1, 3].
2. **Rank 2: Issue #1404 (Signing & in-toto/DSSE Export)**
   * **Justification:** High audit value[cite: 1, 2]. Elevates internal JSON logs into mathematically verifiable, non-repudiable supply-chain artifacts acceptable by tools like GUAC and cosign[cite: 1, 2].
3. **Rank 3: Issue #1407 (Commit Trailer & SIEM Export)**
   * **Justification:** Direct table stakes[cite: 1, 5]. Commit trailers link Git commits directly to audit runs[cite: 5], and JSONL/SIEM export solves real-world retention requirements (EU AI Act Art. 26)[cite: 1, 5].
4. **Rank 4: Issue #1408 (Governance Inventory)**
   * **Justification:** Moderate effort, high utility for initial procurement and vendor questionnaires (ISO 42001 A.4/A.10)[cite: 1, 6]. Straightforward extraction from existing configuration[cite: 6].
5. **Rank 5: Issue #1406 (Control Crosswalk)**
   * **Justification:** Lowest priority. Self-attested crosswalks do not prove operational effectiveness to auditors[cite: 1, 4]. High ongoing maintenance burden to keep regulatory citations accurate[cite: 1, 4].

**Build First:** **Issue #1405 (Human Approval)**[cite: 3]. In an autonomous coding workflow, unapproved automated changes are non-starters for SOC 2, SOX, and the EU AI Act[cite: 1, 3]. Cryptographically signing and exporting artifacts (#1404) provides little value if the underlying payload demonstrates an unapproved, unsegregated change[cite: 2, 3].

---

## 2026 Standards Alignment for Agent Change Predicate

Instead of a proprietary URI (`[https://brigade.dev/attestation/agent-change/v1](https://brigade.dev/attestation/agent-change/v1)`)[cite: 1, 2], Brigade should align with:

* **Primary Recommendation: in-toto SCAI (Supply Chain Attribute Integrity)**
  * **URI/Type:** `[https://in-toto.io/attestation/scai/attribute-report/v0.3](https://in-toto.io/attestation/scai/attribute-report/v0.3)`
  * **Fit:** Designed specifically to express verifiable claims about software artifacts (e.g., "generated by AI agent X", "reviewed by human Y", "evaluated under policy Z") with supporting evidence links.
* **Secondary / Emerging Standard: IETF WIMSE (Workload Identity in Multi-System Environments) & draft-klrc-aiagent-auth**
  * **Fit:** Focuses on dynamic agent workload identity and execution context propagation[cite: 1]. Use these conventions for structuring the agent identity and session fields inside the attestation payload[cite: 1].
* **Note on OWASP Agent Control Standard:** While the OWASP Top 10 for Agentic Applications (ASI01-ASI10) is finalized, broad consensus around a formal machine-readable predicate schema remains unsettled; adopting standard in-toto predicates remains the most robust choice for cross-tool interoperability.

---

## Fact-Checking & Errata for `research.md`

1. **IETF SCITT Status Error:**
   * *`research.md` Claim:* Notes "SCITT (RFC 9943, Oct 2025)"[cite: 1].
   * *Correction:* RFC 9943 (*An Architecture for Trustworthy and Transparent Digital Supply Chains*) was officially published by the IETF in **June 2026**, not October 2025.
2. **Colorado AI Legislation Status:**
   * *`research.md` Claim:* "Colorado (SB 24-205 repealed and replaced by SB 26-189, effective 1 Jan 2027, disclosure-only)"[cite: 1].
   * *Correction:* SB 26-189 was signed into law on May 14, 2026, replacing SB 24-205 and targeting ADMT in consequential decisions with a January 1, 2027 effective date. However, describing it as "disclosure-only" is inaccurate: SB 26-189 enacts statutory requirements including a three-year compliance record retention mandate, affirmative consumer rights to inspect/correct personal data, mandatory meaningful human review of adverse decisions, and a statutory fault-allocation framework between developers and deployers.
3. **ISO/IEC 42006 Publication Date:**
   * *`research.md` Claim:* "ISO/IEC 42006:2025 (Jul 2025)"[cite: 1].
   * *Confirmation:* Accurate. ISO/IEC 42006:2025 (*Requirements for bodies providing audit and certification of artificial intelligence management systems*) was officially published on **July 7, 2025**.
4. **OWASP Agentic vs. LLM Releases:**
   * *`research.md` Claim:* Notes OWASP Agentic Top 10 released 9 Dec 2025, and mentions 2026 updates[cite: 1].
   * *Clarification:* OWASP Agentic Top 10 v2.01 was formally released on June 1, 2026, and the OWASP Top 10 for LLM Applications 2026 edition landed in August 2026, solidifying the operational boundary between model-level risks and agentic execution risks.
