# CRA actor and incident worksheet

This is a local process aid for recording facts, decisions, and reporting work.
It makes no legal applicability determination, certification, automatic notice,
or actual filing. Tools may draft; the responsible organizational owner, legal,
and security functions decide. Unknown facts are not an exemption. Do not wait
for a completed worksheet to escalate a possible reporting obligation.

## Source and review basis

Source basis supplied for this worksheet: the Commission's
[CRA reporting guidance](https://digital-strategy.ec.europa.eu/en/policies/cra-reporting)
and [open-source guidance](https://digital-strategy.ec.europa.eu/en/policies/cra-open-source)
were freshly retrieved on **2026-10-07 UTC**. This is a retrieval date, not a
publication date. Refresh guidance when using the worksheet for a real event.
The law is [Regulation (EU) 2024/2847](https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:32024R2847).
Exact statutory definitions were not independently verified for this worksheet;
the accountable legal review below must record them.

Accountable legal review must record the applicable provisions, exact text,
interpretation, reviewer, date, and source version for: product with digital
elements; manufacturer; distributor; open-source software steward; actively
exploited vulnerability; and severe incident having an impact on product
security. Record AI role definitions separately if relevant. These fields
remain open until reviewed; the questions below are not substitute definitions.

Verified Commission guidance supplies these planning anchors:

| Actor / trigger | Reporting start or deadline |
| --- | --- |
| Manufacturer, Article 14 | Reporting obligations apply from 2026-09-11 |
| Qualifying OSS steward, Article 24(3) | Reporting obligations apply from 2027-12-11; review that actor's scope separately |
| Reportable exploited vulnerability or severe incident | Early warning within 24 hours and notification within 72 hours of awareness |
| Exploited vulnerability final report | Within 14 days after a corrective measure is available |
| Severe incident final report | Within one calendar month after notification |

These are outer deadlines, not permission to delay. Confirm actor-specific
requirements and recipients with legal/security. Calculate calendar months as
calendar months, not 30 days. Record awareness evidence immediately; a later
classification meeting does not restart an established awareness clock.

## 1. Actor and applicability record

For every entry below, record **fact or decision; evidence reference; open
question; responsible organizational owner; decision/review date**. Use
`unknown` only when actual evidence is absent. Keep disputed evidence and its
limitations rather than replacing it with `unknown`.

| Record | Facts and questions to resolve |
| --- | --- |
| Case and accountability | Case ID, real event or DRILL, legal entity, jurisdiction, responsible owner, security/legal reviewers, authority to decide and submit, review date and next action |
| Product boundary | Exact product, releases, component/dependency versions, commit and artifact identifiers, affected deployments, intended use and users, EU market connection |
| Distribution / placement | Who supplies what, under whose name, where and when? Evidence of making available or placing on the market, licenses, packaging and distribution arrangements |
| Commercial facts | Commercial support commitments, paid services, monetization, contracts, funding and business relationships; record relevant facts even where software is free |
| Applicability decision | Actor(s), applicable provisions and start dates, evidence, reasoning, unresolved definitions, accountable decision and review date |

Assess each role separately; one entity may have several roles:

- **OSS contributor or maintainer:** What is contributed or maintained, by whom,
  under what governance, and with what distribution or commercial involvement?
  An OSS label alone does not decide applicability or exemption.
- **Commercial manufacturer or distributor:** Who develops or commissions the
  product, supplies it under a name or trademark, or distributes it? What do
  contracts and actual supply show? Legal review must decide the precise role
  and resulting duties rather than applying manufacturer clocks to every seller.
- **Qualifying OSS steward:** Is there a legal person and sustained support for
  relevant OSS development or viability? What governance and intended
  commercial-use evidence exists? Legal review must apply the exact statutory
  criteria and Article 24(3), including its later reporting start.
- **AI-system provider or deployer:** Does the actual product meet the applicable
  AI-system definition? Who develops, markets, or uses it, for what intended
  purpose and context? Record a separate legal decision; CRA status does not
  settle AI Act roles.
- **GPAI-model provider:** Is an entity providing a qualifying general-purpose AI
  model, or merely integrating or using another provider's model? Record model,
  supply and modification facts and the legal-review outcome separately.

This worksheet does not conclude that Brigade is regulated AI or that generated
code necessarily constitutes an AI system.

**Support question remains open:** [README](../README.md) describes stable 0.27
and main 0.28 beta, while [SECURITY](../SECURITY.md#supported-versions) says
alpha and latest-minor-on-main security fixes. Record the exact release and
applicable published statements; ask the owner to reconcile them. This worksheet
creates no support promise or support period. SECURITY's 72-hour acknowledgement
and 14-day fix targets are project targets with their own starting events,
separate from CRA awareness and reporting deadlines.

## 2. Incident trigger and response record

Record each item with evidence references and an accountable owner/date:

- Awareness: UTC timestamp, who became aware, source received, event time,
  discovery time and uncertainty; preserve earlier relevant signals.
- Affected product: exact releases/components and scope, including evidence
  supporting unaffected versions and remaining scope questions.
- Trigger: ordinary defect, possible/confirmed active exploitation, possible/
  confirmed severe product-security incident, or another classification;
  evidence of malicious exploitation, security impact and severity, contrary
  evidence, exact legal criteria reviewed and rationale.
- Decision: actor and trigger decisions separately, reviewer authority,
  escalation time, unresolved questions and next review. Unresolved actor status
  calls for immediate escalation when reliable exploitation is observed.
- Response: containment, corrective measures, availability timestamp and source,
  affected-user communications, owners, status, references and follow-up actions.
- Clocks: applicable start date, awareness basis, scheduled 24/72-hour deadlines,
  corrective-measure or notification anchor for the final deadline, calculation,
  legal confirmation and any provisional assumptions.

## 3. Actual filing state and evidence

For each early warning, notification, final report, or update, keep these fields
separate: **scheduled due date; draft status; authorized submitter; official
recipient/channel; actual submission time; official reference/receipt and its
source; acceptance/rejection/pending acknowledgement; next action and owner**.
Use `none`, `pending`, or `unknown` as supported by evidence. Never infer filing
from a draft, a scheduled date, a command exit code, or a local signed receipt.
Never invent an official reference. A hypothetical submission is not actual.

Existing repository mechanisms can support a local evidence index:

- [Verification receipt schemas](receipt-schemas.md) record command results,
  timestamps and available Git/patch bindings; these are execution evidence.
- [Receipt digest rules](attestation-receipt-digests.md) and
  [local signature limits](../SECURITY.md#receipt-digests-and-optional-local-signatures)
  explain integrity and optional single-machine HMAC evidence. They do not
  establish accountable organizational identity or official submission.
- [Portable evidence packages](evidence-package.md) copy a bounded verify-run
  bundle and check integrity. They do not prove retention, trusted provenance,
  or custody acknowledgement; command logs are not included in that package.

Link existing adopter-supplied entity/authority mappings, historical trust and
time evidence, custody records, and official receipts when available. Record
producer, scope, timestamps, verification method and limitations for each.
[#1619](https://github.com/escoffier-labs/brigade/issues/1619) tracks organizational
identity, historical trust and time prerequisites;
[#1407](https://github.com/escoffier-labs/brigade/issues/1407) tracks further
custody links. Neither must land before this worksheet can use existing evidence.
It requires no new identity, WORM/SIEM, retention, reporting or runtime service,
no universal separate-human rule, and no universal retention period. Record
organization-specific approval and retention decisions where applicable.

## Synthetic DRILLs

All names, products, releases, evidence labels and decisions below are synthetic.
All timestamps are UTC. **Actual filing state for every stage: none (drill).**
There are no official references or submission receipts.

### DRILL 1: ordinary defect

- Example Alder Cooperative; Alder Notes 1.2.3, formatter component 1.2.3.
  Awareness: **2027-01-12T09:00:00Z**, synthetic support report `D1-A`.
- Evidence: `D1-B` reproduces a heading spacing error; `D1-C` security triage
  finds no security impact or exploitation evidence in the assessed scope.
- Decision: Example Security Team, **2027-01-12T11:00:00Z**, ordinary defect
  with no reportable security trigger on these facts. Actor applicability is
  separately pending with Example Legal Team; no reporting deadlines scheduled.
- Action: formatter correction assigned to Example Release Team. Remaining
  question: whether other inputs change the security assessment; reopen on new
  evidence. Filing: **none (drill)**, not an official exemption determination.

### DRILL 2: malicious exploitation, actor unresolved

- Example Birch Association; Birch Pack 2.4.0, loader 2.4.0.
  Awareness: **2027-02-10T10:00:00Z**, synthetic response alert `D2-A`.
- Evidence: `D2-B` corroborates reliable malicious exploitation of the loader
  vulnerability. Entity supply contracts and commercial-support scope remain
  unresolved; OSS status does not resolve the actor question.
- Decision/action: Example Security Team escalates immediately at awareness
  to Example Organizational Owner and Legal Team; contain affected deployments
  and prepare drafts without waiting for actor resolution.
- Conditional manufacturer schedule: early warning **2027-02-11T10:00:00Z**;
  notification **2027-02-13T10:00:00Z**. If the corrective measure becomes
  available **2027-02-15T16:00:00Z** (`D2-C`, hypothetical release record),
  final report due **2027-03-01T16:00:00Z**, 14 days later.
- Remaining questions: exact actor/legal criteria, affected downstream releases,
  actual corrective availability and reporting channel. Example Legal Team owns
  the actor decision. These conditional dates are planning deadlines, not
  submissions. Filing: **none (drill)**.

### DRILL 3: severe incident, manufacturer established

- Example Cedar Products Ltd; Cedar Gate 3.1.0, security controller 3.1.0.
  Exercise evidence `D3-A` establishes manufacturer status and applicable
  Article 14 duties by an Example Legal Team decision dated **2027-02-25**.
- Awareness: **2027-03-01T08:00:00Z**, synthetic incident alert `D3-B`.
  `D3-C` records widespread loss of product security controls. Example Security
  and Legal Teams classify it as a severe product-security incident at
  **2027-03-01T09:00:00Z**, an established exercise assumption under reviewed
  criteria. The real-world definition still needs the source review above.
- Established schedule: early warning **2027-03-02T08:00:00Z**;
  notification **2027-03-04T08:00:00Z**. A hypothetical notification at
  **2027-03-04T08:00:00Z** would make the final report due
  **2027-04-04T08:00:00Z**, one calendar month later. With no actual notification,
  that final anchor remains hypothetical, not a receipt-backed filing time.
- Action: Example Response Team isolates affected controllers and prepares
  recovery and user communications. Remaining questions: downstream scope,
  restoration evidence and final corrective findings. Example Organizational
  Owner tracks response and submission readiness. Filing: **none (drill)**.
