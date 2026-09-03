<!-- Verbatim reviewer output. Synthetic example addresses have their at-sign replaced with [at] to satisfy the content guard. -->
## 1. Concrete in-toto Statement v1 and Proposed Predicate

The attestation envelope conforms to **in-toto Statement v1** (`[https://in-toto.io/Statement/v1](https://in-toto.io/Statement/v1)`)[cite: 1]. The predicate defines `[https://brigade.dev/attestation/agent-change/v1](https://brigade.dev/attestation/agent-change/v1)`[cite: 1, 2] to bind the orchestrator session, producing seats, human requester, human approver, patch digests, and verification receipts into an immutable, verifiable claim.

```json
{
  "_type": "https://in-toto.io/Statement/v1",
  "subject": [
    {
      "name": "git:tree",
      "digest": {
        "gitTree": "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
      }
    },
    {
      "name": "patch:changes.patch",
      "digest": {
        "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
      }
    }
  ],
  "predicateType": "https://brigade.dev/attestation/agent-change/v1",
  "predicate": {
    "schemaVersion": "1.0.0",
    "orchestrator": {
      "name": "brigade",
      "version": "0.14.2",
      "runId": "20260902-193000-a1b2c3d4",
      "lifecycleJournalDigest": "sha256:8f434346648f6b96df89dda901c5176b10f60047bf841b80d0dcfbb84ff0d04b"
    },
    "request": {
      "requester": {
        "id": "alice[at]corp.internal",
        "kind": "human",
        "keyId": "SHA256:uN0b904v83kd9K30Dkdf903ldkj02kdkf903lkd"
      },
      "task": "Refactor auth middleware to support OIDC claims validation",
      "requestedAt": "2026-09-02T19:30:00Z"
    },
    "execution": {
      "mode": "orchestrated",
      "harness": {
        "name": "pytest-local",
        "fingerprint": "9a38f7bc2245ae61"
      },
      "codeGraphDelta": {
        "modifiedFiles": 3,
        "affectedSymbols": 12
      },
      "seats": [
        {
          "seatId": "planner-1",
          "role": "orchestrator",
          "transport": "direct",
          "provider": "anthropic",
          "model": "claude-3-7-sonnet-20250219",
          "reasoning": "high",
          "admissibleTools": ["code_search", "git_status"]
        },
        {
          "seatId": "coder-1",
          "role": "worker",
          "transport": "direct",
          "provider": "openai",
          "model": "gpt-5",
          "reasoning": "medium",
          "admissibleTools": ["file_write", "syntax_check"],
          "workerResultDigest": "sha256:5e884898da28047151d0e56f8dc6292773603d0d6aabbdd62a11ef721d1542d8",
          "exitCode": 0
        }
      ]
    },
    "lineage": {
      "baselineCommit": "c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d3",
      "targetTreeFingerprint": "4b825dc642cb6eb9a060e54bf8d69288fbee4904",
      "patchSha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
      "causalChain": [
        {
          "relation": "planned_from",
          "kind": "plan",
          "id": "task-auth-refactor",
          "digest": "sha256:7d8a9f623a8b...32"
        },
        {
          "relation": "executed_from",
          "kind": "run",
          "id": "20260902-193000-a1b2c3d4",
          "digest": "sha256:8f434346648f...4b"
        }
      ]
    },
    "verification": [
      {
        "verifyRunId": "20260902-193410-v9876543",
        "status": "completed",
        "allPassed": true,
        "receiptDigest": "sha256:3f4a6b2c9d1e...ef",
        "attestationRef": "https://in-toto.io/attestation/test-result/v0.1",
        "commands": [
          {
            "command": "pytest tests/auth/ -v",
            "exitCode": 0,
            "checkRole": "effectiveness"
          }
        ]
      }
    ],
    "authorization": {
      "decision": "allow",
      "scope": "run",
      "reason": "Verified unit test execution against OIDC token claims and segregation-of-duties invariant.",
      "approvedAt": "2026-09-02T19:42:15Z",
      "approver": {
        "id": "bob-security[at]corp.internal",
        "kind": "human",
        "keyId": "SHA256:kL893jd08Kdf023jkd98234lksdf0923jksdf90234l"
      },
      "sodVerified": true
    }
  }
}
```

### Schema Field Definitions and Mapping

| Proposed Predicate Field | Data Type | Source in `receipt-schemas.md` / Tracker | Status | Description |
| :--- | :--- | :--- | :--- | :--- |
| `subject[].digest.gitTree` | Hex String | `brigade.work_verify_receipt: tree_fingerprint`[cite: 4] | Existing | Git tree hash after edits applied[cite: 4]. |
| `subject[].digest.sha256` | Hex String | `brigade.work_verify_receipt: changes_patch_sha256`[cite: 4] | Existing | SHA-256 digest of `changes.patch`[cite: 4]. |
| `orchestrator.name` | String | Static binary metadata (`brigade`) | Existing | Orchestrator harness name. |
| `orchestrator.version` | Semver String | `brigade.work-run: exporter_brigade_version`[cite: 4] | Existing | Brigade binary version[cite: 4]. |
| `orchestrator.runId` | String | `brigade.run.v1: run_id`[cite: 4] | Existing | Durable run directory and execution ID[cite: 4]. |
| `orchestrator.lifecycleJournalDigest`| String | `brigade.run.v1: journal_last_event_digest`[cite: 4] | Existing | Terminal SHA-256 digest of `events/lifecycle.jsonl`[cite: 4]. |
| `request.requester.id` | String | Not captured in current schemas | **New Capture** | Human operator initiating run (`user@domain` or SSH pubkey fingerprint). |
| `request.requester.kind` | Enum | Not captured (`human` / `automation`) | **New Capture** | Category of requester. |
| `request.requester.keyId`| String | Not captured in current schemas | **New Capture** | Key fingerprint used to sign initial run request. |
| `request.task` | String | `brigade.run.v1: task`[cite: 4] | Existing | Bounded natural language objective assigned to orchestrator[cite: 4]. |
| `request.requestedAt` | ISO-8601 UTC | `brigade.run.v1: started_at`[cite: 4] | Existing | Run initialization timestamp[cite: 4]. |
| `execution.mode` | String | `brigade.synthesis.v1: mode` / `run.json: mode`[cite: 4] | Existing | Dispatch mode (`orchestrated` or `direct-worker`)[cite: 4]. |
| `execution.harness` | Object | `brigade.work_verify_receipt: harness_session`[cite: 4] | Existing | `{harness, fingerprint}` of sandbox environment[cite: 4]. |
| `execution.codeGraphDelta` | Object | `brigade.run.v1: code_graph_delta`[cite: 4] | Existing | GraphTrail modified symbol and file metrics[cite: 4]. |
| `execution.seats[].seatId`| String | `brigade.roster_snapshot.v1: agents.<name>`[cite: 4] | Existing | Seat label identifier from roster[cite: 4]. |
| `execution.seats[].role` | String | `brigade.roster_snapshot.v1: agents.<name>.role`[cite: 4] | Existing | Role: `orchestrator` or `worker`[cite: 4]. |
| `execution.seats[].transport`| String | `brigade.roster_snapshot.v1: agents.<name>.transport`[cite: 4] | Existing | Adapter transport (`direct`, `acpx`, `app-server`)[cite: 4]. |
| `execution.seats[].provider` | String | Not explicit in roster (inferred from model/env) | **New Capture** | Model vendor name (e.g. `anthropic`, `openai`, `ollama`). |
| `execution.seats[].model` | String | `brigade.roster_snapshot.v1: agents.<name>.model`[cite: 4] | Existing | Exact model string requested and resolved[cite: 4]. |
| `execution.seats[].reasoning` | String | `brigade.roster_snapshot.v1: agents.<name>.reasoning`[cite: 4] | Existing | Configured reasoning effort tier[cite: 4]. |
| `execution.seats[].admissibleTools`| Array of String| `brigade.candidate-set.v1: steps[].admissible`[cite: 4] | Existing | Catalog tool IDs cleared for worker execution[cite: 4]. |
| `execution.seats[].workerResultDigest`| String | `worker-results.json` digest | Existing | SHA-256 digest of the seat's result record[cite: 4]. |
| `execution.seats[].exitCode`| Integer | `brigade.worker_results.v1: results[].exit_code`[cite: 4] | Existing | Exit code of worker process/turn[cite: 4]. |
| `lineage.baselineCommit` | Hex String | `brigade.work_verify_receipt: baseline_commit`[cite: 4] | Existing | Base Git commit before changes were generated[cite: 4]. |
| `lineage.targetTreeFingerprint`| Hex String | `brigade.work_verify_receipt: tree_fingerprint`[cite: 4] | Existing | Post-change Git tree digest[cite: 4]. |
| `lineage.patchSha256` | Hex String | `brigade.work_verify_receipt: changes_patch_sha256`[cite: 4] | Existing | Exact-byte SHA-256 of `changes.patch`[cite: 4]. |
| `lineage.causalChain` | Array of Object| `brigade.causal_receipt.v1: parents`[cite: 4] | Existing | Lineage links (`planned_from`, `executed_from`, `verified_from`)[cite: 4]. |
| `verification[].verifyRunId`| String | `brigade.work_verify_receipt: run_id`[cite: 4] | Existing | Independent verify run directory identifier[cite: 4]. |
| `verification[].status` | String | `brigade.work_verify_receipt: status`[cite: 4] | Existing | Final execution state (`completed`, `failed`)[cite: 4]. |
| `verification[].allPassed` | Boolean | Derived from `commands[].exit_code == 0` | Existing | Aggregate pass/fail boolean. |
| `verification[].receiptDigest`| Hex String | `brigade.work_verify_receipt: digests.receipt_sha256`[cite: 4] | Existing | Canonical SHA-256 digest of the verify receipt[cite: 4]. |
| `verification[].attestationRef`| URI | Static in-toto predicate reference | Existing | Pointer to parallel `test-result/v0.1` statement. |
| `verification[].commands[].command`| String | `brigade.work_verify_receipt: commands[].command`[cite: 4] | Existing | Display command string run by verifier[cite: 4]. |
| `verification[].commands[].exitCode`| Integer | `brigade.work_verify_receipt: commands[].exit_code`[cite: 4] | Existing | Child execution return code[cite: 4]. |
| `verification[].commands[].checkRole`| String | `brigade.work_verify_receipt: commands[].check_role`[cite: 4] | Existing | Role: `effectiveness` or `utility_guardrail`[cite: 4]. |
| `authorization.decision`| Enum | Issue #1405: `brigade.run_event.v1: approval`[cite: 3] | **New Capture** | Decision: `allow`, `deny`, or `hold`[cite: 3]. |
| `authorization.scope` | Enum | Issue #1405: `brigade.run_event.v1: approval`[cite: 3] | **New Capture** | Approval scope (`run`, `station`, `merge`)[cite: 3]. |
| `authorization.reason`| String | Issue #1405: `brigade.run_event.v1: approval`[cite: 3] | **New Capture** | Human review justification[cite: 3]. |
| `authorization.approvedAt`| ISO-8601 UTC | Issue #1405: `brigade.run_event.v1: approval`[cite: 3] | **New Capture** | UTC timestamp of approval signature[cite: 3]. |
| `authorization.approver.id`| String | Issue #1405: `approval.approver_id`[cite: 3] | **New Capture** | Reviewer identifier (email, SPIFFE ID, SSH key alias)[cite: 3]. |
| `authorization.approver.kind`| Enum | Issue #1405: `approval.approver_kind`[cite: 3] | **New Capture** | Target identity type (`human` or `seat`)[cite: 3]. |
| `authorization.approver.keyId`| String | Issue #1405: `approval.pubkey_id`[cite: 3] | **New Capture** | Public key identifier used for the approval signature[cite: 3]. |
| `authorization.sodVerified`| Boolean | Issue #1405: Segregation of Duties policy engine[cite: 3] | **New Capture** | Policy engine assertion that `approver.id != producing_seats`[cite: 3]. |

---

## 2. Comparison with Existing Standards and Frameworks

| Framework / Standard | What it covers that this proposal omits | What this proposal covers that it omits |
| :--- | :--- | :--- |
| **GitHub Copilot Coding Agent** (`Agent-Logs-Url`, `actor_is_agent`) | Forge-integrated PR review UI, direct native binding to GitHub Enterprise audit streaming and user SAML identities, server-side log retention enforcement[cite: 1]. | Cryptographic multi-seat agent roster binding, local reproducible patch diff verification, decoupled independent test execution receipts, portable public-key DSSE envelope[cite: 1, 2, 4]. |
| **GitLab Duo Agent** (AI audit events, SLSA predicate) | GitLab CI/CD runner attestation integration, forge-native pipeline gate enforcement, native GitLab secret and token masking at platform level[cite: 1]. | Fine-grained per-turn tool admission (`candidate-set.v1`)[cite: 4], distinct cryptographic human approval event (Issue #1405)[cite: 3], offline-first multi-model agent seat decomposition. |
| **in-toto SCAI** (Supply Chain Assertions for Artifacts) | Formalized generic attribute-assertion vocabulary with evidence references (`PASSED`, `FAILED`, `VERIFIED`), attribute policies across arbitrary supply-chain hops. | Agentic execution metadata (model version, reasoning tier, prompt-plan lineage, tool-gate evaluations, human segregation-of-duties validation). |
| **in-toto Test Result** (`test-result/v0.1`) | Normalized vocabulary for aggregate test suite runs, standard test configuration schemas, cross-ecosystem test runner interoperability[cite: 1, 2]. | The broader agent context: who instructed the change, which models operated on the files, what tools were authorized, and the explicit human approval state. |
| **SLSA Source Track & VSA** (SLSA v1.2 / Verification Summary Attestation) | Tamper-proof source repository hosting assertions (Source Track), high-level binary evaluation policy verdicts (VSA) intended for deploy gates[cite: 1]. | Granular AI generation steps, token/reasoning parameters, model provider snapshots, multi-agent dispatch journals, and manual human override records. |
| **OpenTelemetry GenAI Semconv** (`invoke_agent`, `execute_tool`) | High-frequency, millisecond-level distributed runtime traces, token counters, client/server HTTP durations, span hierarchy across network RPCs[cite: 1]. | Cryptographic immutability (DSSE signatures), tamper-evident digest chains, segregation-of-duties policy assertions, signed artifact and Git-tree bindings. |
| **C2PA** (Coalition for Content Provenance and Authenticity) | Media-focused assertion trees (EXIF, audio/video frame tracking), embedded soft-binding watermarking, consumer media player trust UI[cite: 1]. | Software-native concepts: Git trees, unified patch diffs, unit test exit codes, tool authorization gating, and software engineering segregation-of-duties. |

---

## 3. Identity Binding

Non-repudiation requires unforgeable cryptographic identity binding across three distinct tiers: the **requester**, the **approver**, and each **agent seat**.

### Comparison of Identity Systems

| Identity Scheme | Cryptographic Mechanism | Offline Capability | Enterprise Fit | Revocation & Auditability |
| :--- | :--- | :--- | :--- | :--- |
| **SPIFFE IDs** (`spiffe://trust-domain/...`) | X.509 SVIDs or JWT-SVIDs issued by SPIRE node agents. | Requires local SPIRE agent daemon; cannot operate air-gapped without infrastructure. | **High** for containerized/Kubernetes microservices; aligns cleanly with enterprise zero-trust infrastructure. | Short-lived certificates (e.g., 1 hour); automated CA-managed revocation. |
| **OIDC via Sigstore Fulcio** | Ephemeral X.509 certificates tied to email/sub via short-lived OIDC tokens. | **Fails completely offline.** Requires outbound connectivity to OIDC provider and Fulcio CA. | **High** for developer workstations with Okta/Entra ID, eliminating static key distribution. | Short-lived (10 minutes) with Rekor transparency log proof; no CRLs/OCSP needed. |
| **SSH Public Keys** (`ssh-keygen -t ed25519`) | Long-lived asymmetric Ed25519/ECDSA keypairs in standard `~/.ssh` or hardware security keys (FIDO2). | **Fully offline-capable.** Zero network dependencies; verification requires only an `allowed_signers` file. | **Moderate to High**; already standard for developer Git access and source commits. | Manual via distributed `allowed_signers` or SSH Certificate Authorities (`ssh-keygen -s`). |
| **Plain Email Address** (e.g. Git co-author) | String claims in JSON payload without cryptographic signatures. | Offline, but offers **zero non-repudiation**. | **Unacceptable** for compliance; fails SOC 2 CC8.1, SOX, and ISO 42001 integrity tests. | Ineffective; trivial to spoof or tamper with in transit. |

### Recommendations

*   **Open-Source / Zero-Runtime-Dependency CLI Default: SSH Public Keys (`ssh-ed25519` / `allowed_signers`).**
    Brigade enforces a strict zero-runtime-dependency rule[cite: 2]. SSH keys are already ubiquitous on developer workstations, require no Python C-extensions, operate 100% offline, and support hardware tokens (YubiKeys via FIDO2 `ed25519-sk`). Identities map to `ssh-ed25519 <key>` with signatures verified against Git-standard `.allowed_signers` files.
*   **Enterprise Deployment: OIDC Subjects via Sigstore Fulcio (Interactive) & SPIFFE IDs (Non-Interactive/CI).**
    In enterprise environments, human requesters and approvers authenticate via corporate IdP (Entra ID, Okta) through Sigstore Fulcio, embedding OIDC claims into short-lived X.509 certificates without long-lived private key risks. Agent seats in automated pipelines receive short-lived SPIFFE SVIDs issued by internal SPIRE servers, binding seat attestation signatures directly to the workload orchestrator's cryptographic identity.

---

## 4. Signing and Transparency

### Signing Options Comparison

1.  **DSSE with `ssh-keygen -Y sign`:** Signs canonical DSSE pre-authentication encoding (`PAE`) bytes via local OpenSSH binary (`ssh-keygen -Y sign -f ~/.ssh/id_ed25519 -n "in-toto"`). Zero third-party dependencies[cite: 2].
2.  **DSSE with `minisign`:** Lightweight public-key signatures using Ed25519. Highly auditable and offline, but requires installing `minisign` binary on PATH[cite: 2].
3.  **Cosign Keyless (Sigstore Bundle):** Embeds OIDC identity, short-lived X.509 certificate, DSSE envelope, and transparency log inclusion proof (Rekor) inside a single Sigstore Bundle JSON[cite: 1, 2]. Requires network access to public or private Sigstore services.
4.  **SCITT (IETF RFC 9943 / Published June 2026):** Supply Chain Integrity, Transparency, and Trust architecture using COSE Sign1 and verifiable transparency services[cite: 1]. Emits cryptographically notarized statements with Merkle inclusion receipts for private enterprise ledgers where public log exposure is unacceptable[cite: 1].

### Recommended Configurations

*   **Default Option (Open Source CLI):** **DSSE Envelope via `ssh-keygen -Y sign`**.
    Keeps Brigade's zero-runtime-dependency promise intact[cite: 2]. Signs the in-toto Statement over DSSE PAE[cite: 1, 2], storing output as `.attestation.json`.
*   **Enterprise Option:** **SCITT Transparency Statement (RFC 9943) or Sigstore Private Instance.**
    For organizations governed by strict audit regimes that forbid leaking internal commit/model signatures to public logs, an internal SCITT ledger notarizes the DSSE payload and returns a non-repudiable Merkle inclusion receipt.

### Verifier Command Line Examples

#### 1. Verifying Default SSH-Signed DSSE Attestation

```bash
# 1. Extract payload and verify the SSH signature over DSSE PAE using allowed_signers
ssh-keygen -Y verify \
  -f /etc/brigade/allowed_signers \
  -I "approver[at]corp.internal" \
  -n "in-toto" \
  -s attestation.dsse.sig \
  < <(brigade receipts dsse-pae attestation.json)

# 2. Check the payload with cosign (public key validation)
cosign verify-blob-attestation \
  --key ~/.ssh/id_ed25519.pub \
  --type https://brigade.dev/attestation/agent-change/v1 \
  --check-claims \
  attestation.json
```

#### 2. Verifying Enterprise Keyless Sigstore Bundle Attestation

```bash
# Verifies OIDC subject, issuer, and inclusion proof from Rekor transparency log
cosign verify-blob-attestation \
  --bundle brigade-run-20260902.sigstore.json \
  --certificate-identity "bob-security[at]corp.internal" \
  --certificate-oidc-issuer "https://login.microsoftonline.com/v2.0" \
  --type https://brigade.dev/attestation/agent-change/v1 \
  --check-claims \
  changes.patch
```

#### 3. Verifying Enterprise SCITT Notarized Statement (RFC 9943)

```bash
# Verify the SCITT Merkle tree proof against the enterprise transparency root CA
scitt-verifier verify \
  --transparency-service-root /etc/scitt/ts-root-ca.pem \
  --statement attestation.scitt.cose \
  --expected-issuer "spiffe://corp.internal/brigade/orchestrator" \
  --expected-tag "https://brigade.dev/attestation/agent-change/v1"
```

---

## 5. EU AI Act Roles and 2026 Digital Omnibus Adjustments

The EU Artificial Intelligence Act (Regulation 2024/1689)[cite: 1], as updated by the **EU AI Omnibus (Regulation EU 2026/1744, in force 27 July 2026)**, categorizes obligations strictly by deployment model and lifecycle integration.

### Classification of Roles

| Entity | EU AI Act Role | Justification |
| :--- | :--- | :--- |
| **Open-Source Tool Vendor** (Shipping Brigade CLI) | **None** (Subject to Open-Source Exclusion under Art. 2(12)), provided it is not monetized as a commercial managed service and not integrated into a high-risk system. | Brigade is general-purpose infrastructure / orchestration tooling without baked model weights. It is neither a General-Purpose AI (GPAI) model provider nor an AI system provider placing a high-risk system on the market. |
| **Enterprise Deploying Tool Internally** (Company running Brigade) | **Deployer** (under Article 3(4)), using an AI system under its authority for professional software engineering. | If the company incorporates the agentic output into a regulated product or uses it within operations that impact fundamental rights/safety, high-risk downstream obligations can attach. |
| **Enterprise Modifying/Fine-tuning Models** | **Downstream GPAI Provider** (Article 28(1)(a)-(c)) *only* if they fine-tune or make substantial modifications to an upstream foundational model under their own brand. | Standard dispatch to third-party endpoints (Anthropic/OpenAI) preserves ordinary **Deployer** status[cite: 1]. |

### Directly Attaching Articles for the Deployer

*   **Article 12 (Log Integrity & Record-Keeping):** Requires technical capabilities for automated recording of events throughout the system lifecycle[cite: 1]. Brigade's hash-chained `brigade.run_event.v1` lifecycle journal directly satisfies this requirement[cite: 1, 4].
*   **Article 14 (Human Oversight):** Demands systems be designed so natural persons can oversee operations, prevent risk, and remain capable of interrupting, overriding, or reversing runs[cite: 1, 3]. Issue #1405 (allowing/denying/holding runs) and the attestation's `authorization` block provide direct proof of compliance[cite: 1, 3].
*   **Article 26 (Deployer Obligations):** Deployers must retain logs automatically generated by high-risk AI systems under their control for a minimum of six months (Art. 26(5))[cite: 1].
*   **Article 50 (Transparency Duties for Synthetic Content):** Placed-on-the-market AI systems that generate synthetic text or code must mark outputs in a machine-readable format.

### Digital Omnibus (Reg. EU 2026/1744) Impacts

1.  **Extended Timelines for High-Risk Deadlines:**
    *   **Annex III Standalone High-Risk AI:** Enforcement moved from 2 August 2026 to **2 December 2027**.
    *   **Annex I / Product-Safety Embedded AI:** Enforcement moved to **2 August 2028**.
2.  **Safety Component Narrowing:**
    The Omnibus explicitly clarified that AI systems utilized for user assistance, performance optimization, service efficiency, automation, or convenience are **not** safety components unless their failure endangers the physical health or safety of persons or property. Internal enterprise software engineering agents fall squarely into non-safety automation, shielding companies from premature Annex I high-risk reclassification.
3.  **Synthetic Content Marking Grace Period:**
    Generative systems placed on the market before 2 August 2026 were granted a grace period until **2 December 2026** to comply with machine-readable marking and watermarking requirements (Art. 50). Brigade’s in-toto attestation provides the machine-readable provenance needed to meet this deadline[cite: 1].

---

## 6. Ten Questions from an Enterprise Security Reviewer

1.  **Can the agent forge or backdate its own human approval event?**
    No; the approval payload must be signed by an independent human SSH/OIDC key, and the verifier asserts that `authorization.approver.id` does not match any producing seat[cite: 3].
2.  **Does the attestation leak confidential source code or intellectual property outside our boundary?**
    No; the attestation binds only cryptographic hashes (Git tree, patch SHA-256) and scrubbed summaries, keeping full source files and raw logs out of the envelope[cite: 4].
3.  **What stops a malicious worker seat from altering the verification test scripts to force a pass?**
    Verification runs execute independently with read-only sandbox isolation against registered manifest hashes (`manifest_binding.payload_sha256`)[cite: 4], rejecting untracked runtime script mutations[cite: 4].
4.  **How do we prove to a SOC 2 auditor that code was verified before human sign-off occurred?**
    The attestation contains deterministic timestamps and causal chain hashes proving `verification.completed_at` preceded `authorization.approvedAt`[cite: 1, 4].
5.  **How does the verifier detect if a patch was modified between agent generation and final Git merge?**
    The verifier recalculates the exact SHA-256 of the patch and the Git tree hash; any alteration breaks the cryptographic binding in the in-toto `subject` array[cite: 1, 2, 4].
6.  **Can we run and verify these attestations in an isolated, air-gapped environment?**
    Yes; the default tier uses local Ed25519 SSH keypairs and an offline `.allowed_signers` file without requiring internet access or external CA services[cite: 2].
7.  **What happens if a developer uses their own key to both instruct the agent and approve the resulting PR?**
    The policy check fails closed with a `SOD-VIOLATION` status, barring identical requester and approver keys from satisfying separation-of-duties rules[cite: 3].
8.  **Does this attestation satisfy our 6-month log retention requirement under EU AI Act Article 26(5)?**
    Yes; when archived alongside the indexed `.brigade/work/verify-archive/` directory[cite: 4], the signed attestation serves as an immutable record of system operation[cite: 1].
9.  **Can we ingest these attestations into our enterprise vulnerability and supply chain graph (GUAC / DefectDojo)?**
    Yes; because the attestation adheres to the standard in-toto Statement v1 specification wrapped in DSSE[cite: 1, 2], standard supply chain consumers parse it natively[cite: 1, 2].
10. **How does this prevent prompt injection attacks from hijacking tool access?**
    Each worker assignment enforces pre-execution candidate tool filtering (`brigade.candidate-set.v1`)[cite: 4], cryptographically recording and constraining the admissible tools regardless of injected instructions[cite: 4].
