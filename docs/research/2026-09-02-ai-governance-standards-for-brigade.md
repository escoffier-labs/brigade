# AI governance, audit, and proof-of-work standards Brigade could evidence

Status: research, 2026-09-02. No code changes. Two Luna (gpt-5.6) survey reports
are archived beside this file under `evidence/` and were spot-checked by hand
against primary sources on the same day.

## BLUF

Brigade already produces most of the raw evidence these standards ask for:
append-only run journals, hash-chained verify receipts with exit codes and tree
fingerprints, a provenance envelope on every evidence item, per-run roster
snapshots, redaction-at-ingest, and an offline coordinator audit. What it lacks
is the packaging downstream verifiers and cross-company auditors recognise:
public-key signatures, in-toto/DSSE envelopes, an explicit human-approval
record with a distinct signer, and a control crosswalk that names the clause
each receipt satisfies. Unsigned system records are still evidence once an
auditor has tested their completeness and custody. Signatures make them
portable across trust domains, they do not make them true. Five additive slices close most of the gap. None of them make a
company compliant on their own. Brigade emits evidence, the company still runs
the management system.

## Which standards actually get asked for

Ranked by how likely a mid-size US or EU B2B software company is to be asked
for evidence in 2026-2027, with the clauses that touch an autonomous coding
agent. Status confirmed 2026-09-02 unless flagged.

| # | Standard | Status | Clauses that bind a coding agent | Artifact an auditor wants |
| --- | --- | --- | --- | --- |
| 1 | ISO/IEC 42001:2023 (AI management system) | Certifiable. ISO/IEC 42006:2025 (Jul 2025) now governs the auditors, so certificates are getting stricter | Annex A.6.2.8 AI system recording of event logs. A.6 lifecycle. A.9 responsible use. A.10 third parties (model providers, MCP servers). Clause 8.3 treatment records, 9.1 monitoring evidence | Immutable agent event log, AI inventory, risk register link, signed change receipts |
| 2 | NIST AI RMF 1.0 + AI 600-1 GenAI profile | Voluntary. AI RMF revision underway in 2026 | GOVERN 1-2 accountability and oversight. MAP 3. MEASURE 2. MANAGE 2 change management | Use-case card, agent and tool inventory, evaluation and monitoring exports, signed approvals |
| 3 | NIST SP 800-218 SSDF v1.1 + SP 800-218A | Final | PS (protect), PW (produce), RV (respond). 218A adds model/prompt provenance | SBOM, signed commits and provenance, review records, scan results |
| 4 | EU AI Act (Reg. 2024/1689) | In force. Digital Omnibus, Regulation (EU) 2026/1744 (OJ 24 Jul 2026, in force 27 Jul 2026), moved Annex III high-risk duties to 2 Dec 2027 and embedded-product duties to 2 Aug 2028 | Art. 12 automatic logging. Art. 14 human oversight (interrupt, override, reverse). Art. 26(6) deployers of high-risk systems keep the logs under their control six months minimum. Art. 17 QMS. Annex IV technical documentation | Event logs, oversight and override records, risk-management file |
| 5 | ISO/IEC 23894:2023 (AI risk) + ISO/IEC 42005:2025 (impact assessment) | Published | Risk register with change-triggered reassessment. Impact assessment tied to agent version | Versioned risk register and impact assessment (GRC-tool territory, not Brigade's) |
| 6 | OWASP Top 10 for Agentic Applications 2026 (ASI01-ASI10, released 9 Dec 2025) + LLM Top 10 2025 | Community. The OWASP LLM Top 10 2026 edition was published 3 Aug 2026. An "Agent Control Standard" was announced 1-2 Sep 2026 (text not yet confirmed) | ASI01 goal hijack, ASI02 tool misuse, ASI03 identity and privilege abuse, ASI04 agentic supply chain, ASI05 unexpected code execution, insufficient observability | Threat model keyed by ASI id, tool allow/deny/hold log, agent identity policy, rollback test |
| 7 | SOC 2 (CC6/CC7/CC8) and ISO/IEC 27001:2022 (A.8.15 logging, A.8.28 secure coding, A.8.32 change management) | Procurement already asks | CC8.1 authorised, tested, documented changes. Segregation of duties. Log protection | Ticket to commit to approval to deploy chain, tamper-evident logs |
| 8 | CSA AI Controls Matrix v1.1 (mid-2026, 247 objectives, 18 domains. JSON/YAML/OSCAL bundles. Mapped to ISO 42001, NIST AI RMF, EU AI Act) | Voluntary. STAR for AI on-ramp | Model security, logging, change management, supply chain | AI-CAIQ answers with links to evidence. OSCAL implementation statement |

Lower on the list but worth knowing: NIST COSAIS (SP 800-53 overlays for AI,
still drafts. A multi-agent overlay is in scope), NIST Cyber AI Profile (IR 8596
draft Dec 2025), NIST NCCoE agent identity concept paper (Feb 2026), IETF
drafts on agent auth (draft-klrc-aiagent-auth, WIMSE, SPIFFE for agents),
Colorado (SB 24-205 repealed and replaced by SB 26-189, effective 1 Jan 2027.
Narrower than the original but not disclosure-only: meaningful human review of
adverse decisions, three-year record retention, consumer data access and
correction rights), NYC LL 144 (hiring tools only), HITRUST AI, ISACA AAIA, IIA,
GAO-21-519SP, MITRE ATLAS, PCI DSS 6.5, DORA (EU) ICT change control.

## Proof-of-work formats worth adopting

The supply-chain world already has an interoperable stack. Brigade should emit
into it rather than invent a parallel one.

| Format | Identifier | What it would carry for Brigade |
| --- | --- | --- |
| in-toto Statement v1 (framework v1.2.0, Mar 2026) | `https://in-toto.io/Statement/v1` | Envelope for every exported receipt. `subject` = commit or tree digest |
| DSSE | `application/vnd.in-toto+json` payload type | Signature envelope over the Statement |
| in-toto Test Result | `https://in-toto.io/attestation/test-result/v0.1` | Direct mapping of a `brigade.work_verify_receipt` (PASSED/FAILED, configuration, per-command results) |
| SLSA Provenance v1 / Source track (SLSA v1.2) | `https://slsa.dev/provenance/v1`, `https://slsa.dev/source/v1` | Not Brigade's to emit. CI and the forge produce these. Brigade should reference them by digest |
| SLSA VSA | `https://slsa.dev/verification_summary/v1` | Optional: `brigade receipts verify` result as a verification summary |
| in-toto Reference | `https://in-toto.io/attestation/reference/v0.1` | Pointer to journals, logs, patches, OTel traces stored elsewhere, by digest |
| Custom agent-change predicate | project-controlled URI, e.g. `https://brigade.dev/attestation/agent-change/v1` | Human principal, approver, seat id, harness, model provider and version, plan and policy ids, allow/deny/hold decision, before/after file digests, links to test-result and provenance statements |
| Sigstore bundle + Rekor, or SCITT (RFC 9943, Jun 2026) | Sigstore bundle JSON. COSE signed statement + receipt | Public-key signature and third-party timestamp. Rekor for OSS interoperability. SCITT for enterprises that cannot publish to a public log |
| CycloneDX 1.7 (Oct 2025) ML-BOM / CDXA, or SPDX 3.0.1 AI profile | `https://cyclonedx.org/schema/bom-1.7.schema.json` | Roster snapshot as a model and tool inventory. CDXA for "claims with evidence" |
| OpenTelemetry GenAI semconv | `invoke_agent`, `execute_tool` span names (the dedicated GenAI conventions repo marks the whole set Development as of 2026-09-02) | High-volume trace export, referenced from signed receipts, never a substitute for them |

Platform table stakes in 2026, which Brigade is now measured against:

- GitHub Copilot coding agent adds an `Agent-Logs-Url` commit trailer linking
  each commit to its session log, marks the human as co-author, and exposes
  `actor_is_agent` and `agent_session_id` audit events with 180-day retention.
- GitLab Duo agent sessions emit AI audit events with inputs, model context, and
  outputs. GitLab provenance uses the SLSA predicate.
- Anthropic's Compliance API (Mar 2026, extended since) now returns Claude Code
  session transcripts for Claude Enterprise tenants, alongside the activity
  feed. Several API-key, cloud-platform, and zero-retention cases stay out.
- Vanta, Drata, and Credo AI sell "agent governance" as agent inventory,
  owner and identity, policy decisions, and tamper-evident evidence trails.
  None publish a portable attestation schema.

## What Brigade already has

Checked in this worktree on 2026-09-02.

| Standard need | Brigade surface today | Where |
| --- | --- | --- |
| Automatic, tamper-evident event log (AI Act 12, 42001 A.6.2.8, 27001 A.8.15, 800-53 AU) | Append-only `brigade.run_event.v1` lifecycle journal with digest chain. outcome ledger `prev_digest`/`digest`. `brigade receipts verify` digest checks | `run_journal`, `run_events`, `receipt_schema.py`, `receipts_cmd.py` |
| Verification evidence with real exit codes (SOC 2 CC8.1, PCI 6.5.1, SSDF PW.8) | `brigade.work_verify_receipt` v2: commands, exit codes, `baseline_commit`, `tree_fingerprint`, `changes_patch_sha256`, evidence snapshot | `docs/receipt-schemas.md` |
| Who and what did the work (ASI03, NIST GOVERN 2, 42001 A.9) | Per-run `roster.json` snapshot of seats, harness, model. `producer_run_id` on verify receipts. worker receipts | `run_receipts.py`, `route_receipts.py` |
| Provenance and trust of inputs (ASI01, 42001 A.7, NIST MAP) | `brigade.provenance-envelope.v1`: origin, modality, attribution, trust label, injection status, exact-byte sha256. trust policy | `provenance.py`, `docs/proposals/provenance-envelope.md` |
| Sensitive data never persisted (ICO, 27001, HITRUST) | `brigade.evidence-redaction.v1`: origin-scoped, counts only, fails closed | `evidence_redaction.py` |
| Signature on receipts | Optional local HMAC-SHA256 (`digests.signature`, `key_id`). single-machine, documented as not PKI | `receipt_signing.py`, technical guide |
| Decision audit after the fact (NIST MANAGE, IIA) | Offline `brigade.run_audit.v1` replays the journal, never writes | `run_audit.py` |
| Causal lineage plan -> run -> verify -> outcome -> handoff (42001 A.6, SSDF) | Causal receipt companion records | `causal_receipt.py`, issue #493 |
| Blast radius before change (27001 A.8.32 impact assessment) | `brigade code affected/impact/callers`. code-graph brief recorded in `run.json` | `docs/code-intelligence.md` |
| Reviewed-state closeouts (SOC 2 CC7, 42001 9.1) | Security, backup, handoff, memory-care, release-candidate closeouts | `docs/closeout-receipts.md` |
| Supply-chain pins (ASI04, SSDF PS) | Component manifest policy, pinned components, release attestations (issue #364) | `docs/component-manifest-policy.md` |
| Export bridge | Evidence-ledger JSONL export | `brigade receipts export miseledger` |

Prior tracker work found: #364 release attestations, #493 causal lineage, #498
redaction, #505 provenance envelope, #568 lifecycle journal, #595 run audit.
No issue mentions ISO 42001, SLSA, in-toto, SOC 2, or Sigstore. No Brigade
evidence for earlier compliance research (`brigade evidence search` has no
`--target` flag in this install. searched the tracker instead).

## Gaps, ranked by payoff

Tracker issues filed 2026-09-02: #1404 signing and attestation export, #1405
human approval event, #1406 control crosswalk, #1407 commit trailer and
retention, #1408 governance inventory.

1. **Public-key signing and an in-toto/DSSE export.** The HMAC tier is
   explicitly single-machine. Add `brigade receipts export attestation` that
   wraps a verify receipt as a Test Result statement and a run receipt as the
   custom agent-change predicate, signs with `ssh-keygen -Y sign` or minisign
   (both subprocess-only, keeps zero runtime dependencies), and optionally
   emits a Sigstore bundle or SCITT signed statement when the tools are
   present. The technical guide already reserves this "cross-machine trust
   tier". Reviewer caveat: `ssh-keygen -Y` emits SSHSIG, not a raw DSSE
   signature, so the SSH profile needs its own verifier and only the cosign
   profile is verifiable by `cosign`. `slsa-verifier` checks SLSA provenance
   and VSA only. GUAC collects DSSE and in-toto statements. GRC tools accept
   the files as attachments, nothing more.
2. **Human approval as a first-class, separately signed event.** EU AI Act
   Art. 14, SOC 2 CC8.1, SOX segregation of duties, and PCI 6.5.1 all need an
   approver who is not the author. Brigade has reviewer seats and `work
   acceptance`, but no receipt records a human identity with its own key
   deciding allow, deny, or hold. Add an `approval` lifecycle event carrying
   the approver id and signature, and a policy check that the approving
   identity differs from the producing seat. Reviewer caveat: compare
   controlling principals and key custody, not seat labels, and bind the
   approval to the exact tree or patch digest with an expiry.
3. **Control crosswalk plus `brigade evidence controls`.** A documented table
   from each receipt family to ISO 42001 Annex A ids, NIST AI RMF
   subcategories, SOC 2 CC ids, 27001 A.8.x, SSDF practice ids, AICM v1.1
   control ids, and OWASP ASI ids. Cheap, and it turns a questionnaire from a
   week of writing into a query. AICM ships OSCAL, so an OSCAL
   assessment-results export is a natural second step (also FedRAMP 20x).
4. **Commit trailer and retention/export.** A `Brigade-Receipt:` trailer with
   run id and receipt digest on every worker commit, matching GitHub's
   `Agent-Logs-Url`. A retention policy on journals and receipts (AI Act Art.
   26(6) wants six calendar months minimum for high-risk deployers, so use
   `P6M` semantics rather than a day count) and a JSONL or OTLP export so a
   SIEM can ingest the journal. Reviewer caveat: a retention floor inside the
   tool that writes the logs is a safety guard, not a control. The control is
   external WORM or SIEM storage with separate deletion rights.
5. **Governance inventory export.** `brigade governance inventory` emitting
   `agent-registry.json`, `model-provider-registry.json`, and
   `tool-and-mcp-server-inventory.json` from the roster, `tools.toml`, and MCP
   sync state, optionally as CycloneDX 1.7 ML-BOM. This is the "AI inventory"
   line item in ISO 42001, NIST AI RMF, and every vendor questionnaire, and
   Brigade already knows the answer.

Explicitly out of scope for Brigade: risk registers and impact assessments
(ISO 23894, 42005) belong in a GRC tool, SLSA build provenance belongs in CI,
and C2PA and TPM attestation have no coding-agent use case yet.

## Second-opinion review, 2026-09-02

Two review prompts were run against the doc, the five issues, and the receipt
schema contract: an auditor-style critique (ISO 42001 lead auditor, SOC 2
auditor, EU AI Act counsel) and an attestation-design brief. Reviewers were
Gemini 3.1 Pro through Oracle's cookie-based web client and GPT-5.6 Sol at
xhigh reasoning through Codex with web search. The ChatGPT browser lane
stalled four times and produced nothing. Raw reports sit under `evidence/` as
`2026-09-02-ai-governance-review-*.md`. Facts below were re-checked against
primary sources before being adopted.

Where both reviewers agree:

- **#1404.** `ssh-keygen -Y sign` produces an SSHSIG object, not the raw
  signature DSSE expects, so "cosign verifies it" is false unless the export
  uses `cosign attest-blob`. Ship one signer profile first. Use `gitCommit`
  and `gitTree` digest keys on subjects. The Test Result predicate has no
  per-command exit-code field, so map commands to pass, warn, and fail lists
  and reference the full receipt separately. Embedding a signature inside the
  receipt is self-referential. Sign a detached statement over the receipt
  digest. Pin cosign at or above 2.6.5 or 3.1.3 (GHSA-fx35-mq7g-6g98, Aug
  2026, legacy-bundle identity bypass).
- **#1405.** Comparing `approver_id` to a seat id is not segregation of duties.
  Compare controlling principals, credential custody, and access paths. The
  approval must bind the exact tree or patch digest, carry an expiry and a
  single-use nonce, and precede merge or deploy. `approver_kind: seat` should
  not exist inside an approval event. GPT-5.6 adds that Art. 14 and CC8.1 do
  not universally mandate a different approver. If company policy says so,
  the auditor tests the policy as written.
- **#1406.** An evidence index, not evidence. Add an evidentiary state per row
  (`evidenced_passed`, `evidenced_failed`, `untested`, `not_applicable`),
  framework edition and date, obligation bearer, and applicability. In OSCAL
  the crosswalk is a `mapping-collection` and Brigade's claims are a
  `component-definition`. `assessment-results` only follows a real assessment.
  Do not reproduce licensed ISO or AICPA clause text.
- **#1407.** A retention floor inside the writing tool is not a control.
  Retention belongs in external WORM or SIEM storage with separate deletion
  rights, legal hold, and restore tests. A commit trailer that names the
  receipt digest while the receipt's subject names the commit is a cycle. Put
  only `Brigade-Run` in the commit and issue the signed attestation after the
  commit. Six months is `P6M`, not 183 days. A prune receipt must be signed
  and exported before deletion.
- **#1408.** Point-in-time evidence of Brigade's own scope, never the
  company's full AI inventory. Add owner, purpose, environment, lifecycle
  status, privilege scope, and dated provenance for any provider retention
  claim. Add `--since` and `--until`. A document cannot carry its own digest
  without canonicalisation rules, so use a detached manifest. In CycloneDX,
  local tools are components and remote MCP endpoints are services, and
  `modelCard` is only valid on `machine-learning-model` components.

Where they disagree:

- **Build order.** Gemini puts #1405 first because unapproved changes fail
  every change-control regime. GPT-5.6 puts a narrowed #1404 first (detached
  DSSE statement, one signer profile, a trust-policy file, offline verify)
  because #1405 needs a trustworthy identity substrate, then #1405 immediately
  after, then #1408, then #1407 split into linkage and external retention.
  Both rank #1406 last. Adopted: GPT-5.6's order, with #1408 pulled up
  because it is cheap and scopes everything else.

GRC ingestion, as of 2026-09-02:

| Tool | Accepts Brigade output as-is | Form it needs |
| --- | --- | --- |
| Vanta, Drata, Secureframe | Attachments only | JSON or ZIP evidence files via their evidence APIs, mapped to controls by hand. None parse DSSE or in-toto |
| AuditBoard | No confirmed path | Flat CSV or XLSX for analytics import |
| Dependency-Track 5.1.0 | #1408 only | CycloneDX 1.7 via the BOM endpoint |
| GUAC | #1404 and #1408 | DSSE or in-toto statements and CycloneDX through its collectors. Custom predicate semantics are not interpreted |
| OSCAL importers | No | Valid OSCAL 1.2.2 `mapping-collection` or `component-definition` |

Predicate alignment: keep the project-owned URI. Borrow SCAI v0.3
(`https://in-toto.io/attestation/scai/v0.3`) for attribute and evidence
references, and the field set from `draft-munoz-wimse-authorization-evidence-01`
(individual draft, 18 Jul 2026: subject, delegated subject, resource, action,
decision, request hash, correlation) for the authorization block. ISO/IEC
24970 (AI system logging) reached FDIS in May 2026 and will define log content
expectations. OWASP's Agent Control Standard (1 Sep 2026) standardises runtime
hooks and policy enforcement, not an attestation format.

Attestation design guidance from the GPT-5.6 pass, adopted for #1404:

- Export a small bundle rather than one statement. The `agent-change`
  statement is an evidence index. Separately signed `agent-request`,
  `agent-execution` (one per seat workload), `human-approval`, and standard
  Test Result statements carry the claims each principal can actually make.
  An exporter-only signature cannot prove a human requested or approved.
- Subject is the result `gitTree`. Add `gitCommit` only when the commit
  resolves to that tree. Refuse to export without a tree and patch digest.
- Keep DSSE `payloadType` at `application/vnd.in-toto+json` so cosign and
  in-toto verifiers accept the envelope.
- The approval binds the final tree and the Test Result digests, so any later
  change invalidates it.
- Public projection drops task text and approval reason but keeps their
  digests. Env values, transcripts, stdout, stderr, and absolute paths never
  leave the machine.
- Identity is the OIDC `(issuer, subject)` pair or an SSH key mapped through a
  versioned `allowed_signers` file, never an email string. Enterprise
  deployments use Fulcio for humans and SPIFFE SVIDs for seats.
- Brigade never asserts a SLSA Source level about its own receipt. Only the
  forge enforcing protected-branch review can.
- EU AI Act roles: an open-source, model-agnostic CLI likely sits under the
  Art. 2(12) open-source exclusion. A company running it is a deployer, with
  Art. 26 duties only if the deployment is high-risk. Integrating a
  third-party model does not make anyone a GPAI provider. Whether Art. 50(2)
  machine-readable marking covers generated source code is unsettled, so do
  not claim the attestation satisfies it.

Reviewer claims discarded after checking: Gemini's "Agentic Top 10 v2.01,
June 2026" (the 2026 edition shipped 9 Dec 2025) and Gemini's SCAI
`attribute-report` URI (the real one is `scai/v0.3`).

## Caveats

- Brigade evidence proves which digest changed, which seat and model acted,
  what ran, and what a verifier saw. It cannot prove a model reasoned
  correctly or that a human read every line. Standards that ask for
  "meaningful oversight" still need a policy on top.
- Several items are moving: NIST AI RMF revision, COSAIS drafts, OWASP 2026
  LLM Top 10 and Agent Control Standard, IETF agent-identity drafts, OTel agent
  spans. Recheck before citing clause numbers in outward-facing material.
- The EU AI Act delay only moves Annex III high-risk deadlines. GPAI duties
  have applied since Aug 2025 and transparency duties since Aug 2026.

## Sources checked by hand on 2026-09-02

- [EU AI Act delays finalised](https://www.pinsentmasons.com/out-law/news/law-delaying-eu-high-risk-ai-rules-finalised) and [Article 12](https://artificialintelligenceact.eu/article/12/)
- [NIST COSAIS publications](https://csrc.nist.gov/Projects/cosais/publications)
- [ISO/IEC 42006:2025](https://www.iso.org/standard/42006), [ISO/IEC 42005:2025](https://www.iso.org/standard/42005)
- [ISO 42001 A.6.2.8](https://www.isms.online/iso-42001/annex-a-controls/a-6-ai-system-life-cycle/a-6-2-8-ai-system-recording-of-event-logs/)
- [OWASP Top 10 for Agentic Applications 2026](https://genai.owasp.org/resource/owasp-top-10-for-agentic-applications-for-2026/)
- [SLSA v1.2 source track](https://safeguard.sh/resources/blog/slsa-v1-2-source-track-deep-dive-2025), [in-toto provenance predicate](https://github.com/in-toto/attestation/blob/main/spec/predicates/provenance.md)
- [OpenTelemetry GenAI observability](https://opentelemetry.io/blog/2026/genai-observability/)
- [Colorado SB 189 replacement law](https://www.seyfarth.com/news-insights/colorado-enacts-artificial-intelligence-replacement-law.html)
- [CSA AICM v1.1](https://cloudsecurityalliance.org/blog/2026/07/14/ai-controls-matrix-v1-1-strengthening-the-foundation-for-trustworthy-ai)
- [IETF draft-klrc-aiagent-auth](https://datatracker.ietf.org/doc/draft-klrc-aiagent-auth/)
- [NIST SP 800-218A](https://csrc.nist.gov/pubs/sp/800/218/a/final)
- [GitHub: trace any Copilot coding agent commit to its session logs](https://github.blog/changelog/2026-03-20-trace-any-copilot-coding-agent-commit-to-its-session-logs/), [agent audit log events](https://docs.github.com/en/copilot/reference/agentic-audit-log-events)
- [Anthropic Compliance API](https://claude.com/blog/claude-platform-compliance-api)

Full clause-level surveys with their own source lists:
`evidence/2026-09-02-ai-governance-luna-survey-1-frameworks.md` and
`evidence/2026-09-02-ai-governance-luna-survey-2-attestations.md`.
