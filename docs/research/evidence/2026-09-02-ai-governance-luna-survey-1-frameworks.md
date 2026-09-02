# Survey: AI governance, audit, and security requirements for autonomous coding agents

Status date: 2 September 2026.

## Executive judgment

BLUF: A mid-size B2B software company using autonomous coding agents should build an evidence system around ISO/IEC 42001, NIST AI RMF and SSDF, ISO/IEC 23894, the EU AI Act where its product or deployment is in scope, and conventional security assurance such as SOC 2 or ISO/IEC 27001. High confidence - these are the frameworks most likely to appear in customer due diligence, certification audits, regulated procurement, or EU-market compliance requests.

The agent itself is usually not automatically a regulated “high-risk AI system.” The compliance exposure comes from:

- what the agent can access and change;
- whether it makes or materially influences regulated decisions;
- whether the resulting software is an AI system or safety-critical product;
- whether the company is an AI provider, deployer, downstream provider, or merely an enterprise user;
- contractual requirements imposed by customers and model vendors.

A practical audit trail should link:

`human principal -> agent identity/version -> model/provider/version -> prompt/context hash -> tool calls -> files changed -> tests/results -> reviewer approval -> merge/deploy receipt`

---

## 1. ISO/IEC standards

### ISO/IEC 42001:2023, Information technology - Artificial intelligence - Management system

- Issuer: ISO/IEC JTC 1/SC 42.
- Status: Published International Standard, December 2023. Certifiable management-system standard.
- Scope: Organization-level AI management, covering development, provision, and use of AI systems. It does not prescribe one agent architecture.

Important requirements:

- Clauses 4-10 establish the management system: context, leadership, planning, support, operation, performance evaluation, and improvement.
- Clause 6.1.2 requires an AI risk assessment process.
- Clause 6.1.3 requires AI risk treatment.
- Clause 6.1.4 requires AI system impact assessments.
- Clause 7.5 covers documented information.
- Clause 8.1 covers operational planning and control.
- Clause 8.2 covers AI risk assessment.
- Clause 8.3 requires implementation and effectiveness verification of AI risk treatment, with records retained.
- Clause 8.4 requires impact assessments at planned intervals and when significant changes are proposed, with results retained.
- Clause 9.1 requires the organization to determine what is monitored and measured, how, and when. Documented information must evidence the results.
- Clause 9.2 requires internal audits.
- Clause 9.3 requires management review records.
- Clause 10 requires continual improvement and corrective-action records.

Annex A has 38 reference controls grouped into:

- A.2 AI policies;
- A.3 internal organization;
- A.4 resources;
- A.5 assessing impacts of AI systems;
- A.6 AI system lifecycle;
- A.7 data for AI systems;
- A.8 information for interested parties;
- A.9 use of AI systems;
- A.10 third-party relationships.

For autonomous coding agents, the highest-value controls are:

- A.6 lifecycle controls: requirements, design and development, verification and validation, deployment, operation, monitoring, and retirement;
- A.6.2.8, recording of AI system event logs;
- A.7 data controls: data acquisition, quality, provenance, preparation, and governance;
- A.8 information for interested parties: information needed to understand the AI system and its limitations;
- A.9 responsible-use controls, including acceptable-use rules and monitoring;
- A.10 third-party relationship controls for model providers, agent platforms, MCP servers, repositories, CI services, and cloud providers.

The standard’s event-log guidance expressly contemplates recording information such as date and time of AI-system use. For a coding agent, that should be expanded to agent identity, model identifier, repository and commit, tool calls, approvals, test output, and resulting artifact.

Audit artifacts:

- AI management-system scope and policy;
- AI inventory and Statement of Applicability;
- agent risk register and treatment plan;
- impact assessment;
- immutable agent event log;
- model/provider and dependency inventory;
- signed pull-request and deployment receipts;
- internal-audit and management-review records.

Sources: [ISO AI standards catalogue](https://www.iso.org/sectors/it-technologies/ai), current page accessed 2 September 2026; [ISO/IEC 42001 text excerpt](https://www.wd-cert.com/upload/files/2025/8/729a0b031a2c59e1.pdf), published 2023.

### ISO/IEC 42005:2025, AI system impact assessment

- Issuer: ISO/IEC JTC 1/SC 42.
- Status: Published International Standard, 2025.
- Purpose: Impact-assessment guidance across the AI system lifecycle. It supports ISO/IEC 42001 clause 6.1.4 and Annex A.5.

Relevant evidence topics:

- intended and reasonably foreseeable uses;
- affected individuals, groups, organizations, and society;
- potential benefits and harms;
- fundamental-rights, safety, privacy, security, fairness, and environmental impacts;
- lifecycle stage and deployment context;
- controls, residual risks, affected stakeholders, and reassessment triggers.

For coding agents, the assessment should cover source-code confidentiality, unauthorized repository changes, malicious code generation, dependency poisoning, license/IP exposure, insecure generated code, credential exposure, and the effect of false test results.

Audit artifact:

- versioned AI impact assessment in JSON or structured GRC format, linked to agent version, repository, use case, affected assets, controls, residual risk, owner, approval, and reassessment date.

Source: [ISO publication overview for ISO/IEC 42005:2025](https://www.iso.org/files/live/sites/isoorg/files/publications/en/PUB100498.pdf), 2025.

### ISO/IEC 42006:2025, Requirements for bodies providing audit and certification of AI management systems

- Issuer: ISO/IEC JTC 1/SC 42.
- Status: Published International Standard, first edition, July 2025.
- Scope: Requirements for certification bodies auditing ISO/IEC 42001 systems. It primarily governs the auditor or certification body, not the software company.

Relevant requirements concern:

- competence of audit personnel;
- AIMS-specific knowledge and experience;
- audit planning, sampling, impartiality, and consistency;
- credible certification decisions and audit records.

Audit artifact:

- accredited certification-body audit plan, auditor competence records, sampling plan, nonconformity register, corrective-action evidence, and certificate scope.

Source: [ISO/IEC 42006:2025](https://www.iso.org/standard/42006?browse=tc), published July 2025.

### ISO/IEC 23894:2023, Information technology - Artificial intelligence - Guidance on risk management

- Issuer: ISO/IEC JTC 1/SC 42.
- Status: Published guidance, 2023; not itself a certifiable regulation.
- Focus: AI-specific risk identification, analysis, evaluation, treatment, monitoring, and communication across the lifecycle.

For coding agents, apply it to:

- autonomy and excessive agency;
- tool and repository access;
- model and prompt supply chain;
- data leakage;
- generated vulnerabilities;
- unreliable tests or hallucinated explanations;
- human override and rollback;
- drift when models, tools, repositories, or policies change.

Audit artifact:

- AI risk register with risk owner, likelihood/impact, controls, residual risk, treatment decision, review date, and linked incidents/tests.

Source: [ISO AI standards catalogue](https://www.iso.org/sectors/it-technologies/ai), 2026 page listing ISO/IEC 23894:2023.

### ISO/IEC 5338:2023, Information technology - Artificial intelligence - AI system life cycle processes

- Issuer: ISO/IEC JTC 1/SC 42.
- Status: Published, 2023.
- Focus: Lifecycle processes for conception, development, verification, deployment, operation, monitoring, maintenance, and retirement.

For autonomous coding agents, it supports process traceability from:

`use case -> requirements -> agent configuration -> implementation -> verification -> release -> monitoring -> retirement`

Audit artifact:

- lifecycle record or system-model manifest identifying each agent, model, tool, data source, version, owner, gate, test result, and retirement decision.

Source: [ISO AI standards catalogue](https://www.iso.org/sectors/it-technologies/ai), 2026 page listing ISO/IEC 5338:2023.

### ISO/IEC TR 24028:2020, Overview of trustworthiness in artificial intelligence

- Issuer: ISO/IEC JTC 1/SC 42.
- Status: Published Technical Report, 2020.
- Focus: Trustworthiness characteristics including reliability, safety, security, resilience, accountability, transparency, explainability, privacy, and fairness.

It is guidance rather than a direct audit obligation. For coding agents, its practical implication is that “the agent produced a passing test” is insufficient evidence without provenance, reproducibility, security testing, and human accountability.

Audit artifact:

- trustworthiness evaluation matrix mapping each characteristic to tests, thresholds, owner, result, and unresolved limitations.

Source: [ISO/IEC JTC 1/SC 42 catalogue](https://www.iso.org/committee/6794475/p2), current catalogue accessed 2 September 2026.

---

## 2. NIST frameworks and software-security guidance

### NIST AI RMF 1.0, AI 100-1, 2023

- Issuer: National Institute of Standards and Technology.
- Status: Published, January 2023; voluntary. NIST states that AI RMF 1.0 is being revised in 2026.
- Functions: Govern, Map, Measure, Manage.

Relevant subcategories:

- Govern 1.1, 1.2, 1.3, 1.4: legal and regulatory requirements, accountability structures, roles, and risk tolerance;
- Govern 2.1-2.3: accountability, transparency, and human oversight;
- Govern 4.1-4.3: organizational training, competency, and feedback;
- Govern 5.1-5.2: third-party and supply-chain risks;
- Map 1.1-1.6: intended purpose, context, users, affected groups, and system boundaries;
- Map 2.1-2.3: categorization of risks and impacts;
- Map 3.1-3.5: benefits, harms, and human oversight;
- Measure 1.1-1.3: valid, reliable, documented measurement;
- Measure 2.1-2.4: evaluation of privacy, security, fairness, and explainability;
- Measure 3.1-3.2: independent assessment and documentation;
- Manage 1.1-1.3: prioritized risk treatment and residual-risk decisions;
- Manage 2.1-2.4: incident response, recovery, and change management;
- Manage 4.1-4.3: post-deployment monitoring and decommissioning.

Audit artifacts:

- AI use-case card;
- risk register;
- role and accountability matrix;
- agent and tool inventory;
- evaluation reports;
- monitoring dashboard export;
- incident and rollback records;
- signed human approval records.

Sources: [NIST AI RMF](https://www.nist.gov/itl/ai-risk-management-framework), released 26 January 2023 and updated 2026; [NIST AI RMF Playbook](https://www.nist.gov/itl/ai-risk-management-framework/nist-ai-rmf-playbook), voluntary implementation guidance.

### NIST AI 600-1, AI RMF Generative AI Profile, 2024

- Issuer: NIST.
- Status: Published 26 July 2024; voluntary companion profile.
- Scope: Generative-AI risks and suggested actions mapped to AI RMF functions.

For coding agents, particularly relevant risk areas include:

- confabulation and unreliable output;
- dangerous or insecure code;
- data privacy;
- information security;
- intellectual-property risk;
- harmful bias;
- prompt injection and indirect prompt injection;
- supply-chain compromise;
- over-reliance and automation bias;
- configuration and model changes.

Audit artifact:

- GenAI risk-control matrix;
- prompt and tool-call traces;
- code-security evaluation results;
- red-team findings;
- model/version and configuration records;
- human-review exceptions.

Source: [NIST AI 600-1](https://www.nist.gov/publications/artificial-intelligence-risk-management-framework-generative-artificial-intelligence), published 26 July 2024.

### NIST SP 800-218A, Secure Software Development Practices for Generative AI and Dual-Use Foundation Models

- Issuer: NIST.
- Status: Final, 2024; companion to SSDF 1.1.
- Focus: Secure development practices for AI models and generative-AI software.

For coding-agent use, combine it with SSDF practices:

- prepare the organization;
- protect software and AI assets;
- produce well-secured software;
- respond to vulnerabilities;
- document provenance and dependencies;
- evaluate generated code and model behavior;
- protect training, tuning, prompts, system instructions, and data;
- track changes and security findings.

Audit artifacts:

- SBOM and AI-component inventory;
- model and prompt manifest;
- signed source commits;
- generated-code review record;
- SAST, DAST, dependency, secret, and license scans;
- vulnerability disclosure and remediation records;
- reproducible CI logs.

Source: [NIST SP 800-218A](https://www.nist.gov/publications/secure-software-development-practices-generative-ai-and-dual-use-foundation-models-ssdf), published 2024.

### NIST Cybersecurity Framework Profile for Artificial Intelligence, NIST IR 8596 IPD, 2025 draft

- Issuer: NIST.
- Status: Initial Preliminary Draft, December 2025. Not final or mandatory.
- Focus: Applies CSF 2.0 cybersecurity outcomes to AI systems.

Relevant evidence includes:

- AI asset and dependency inventory;
- identity and access management;
- prompt, data, model, and tool protection;
- vulnerability and incident management;
- logging, monitoring, detection, and recovery;
- supply-chain and third-party risk.

Audit artifact:

- CSF profile or implementation statement;
- AI asset register;
- access-policy export;
- detection and incident records;
- supplier assessments.

Source: [NIST Cyber AI Profile draft](https://nvlpubs.nist.gov/nistpubs/ir/2025/NIST.IR.8596.iprd.pdf), December 2025.

### NIST SP 800-53 Control Overlays for Securing AI Systems, CoSAIS

- Issuer: NIST.
- Status: Development effort; not a final mandatory overlay as of 2 September 2026.
- Relationship: Adapts SP 800-53 controls to AI security. NIST identifies CoSAIS as an ongoing project.

Likely evidence areas:

- AC: agent identity, least privilege, authorization, separation of duties;
- AU: event logging, audit review, non-repudiation, time synchronization;
- CM: model, prompt, tool, and configuration baselines;
- IA: authenticating agents and services;
- IR: AI incident response;
- SA: secure development and supply chain;
- SI: flaw remediation, monitoring, malicious-input detection;
- SR: supplier and component provenance.

Audit artifact:

- OSCAL control implementation statements;
- control-to-evidence mappings;
- agent authorization policies;
- tamper-evident audit logs;
- configuration baselines and review records.

Source: [NIST cybersecurity, privacy, and AI program page](https://www.nist.gov/itl/applied-cybersecurity/cybersecurity-privacy-and-ai), updated 15 July 2026.

---

## 3. EU and US law

### Regulation (EU) 2024/1689, Artificial Intelligence Act

- Issuer: European Union.
- Status: In force. Entered into force 1 August 2024. General application began 2 August 2026, with exceptions.
- 2026 change: The Digital Omnibus entered into force 27 July 2026. It moved many Annex III high-risk obligations to 2 December 2027 and high-risk AI embedded in regulated products to 2 August 2028.
- GPAI obligations applied from 2 August 2025. The GPAI Code of Practice is voluntary but provides a compliance route for providers.

Relevant provisions:

- Article 9: risk-management system, continuous and iterative across the lifecycle;
- Article 11: technical documentation;
- Article 12: automatic logging, traceability, and retention of logs generated by the system;
- Article 13: instructions for use and transparency;
- Article 14: human oversight, including ability to understand, interpret, override, interrupt, or reverse outputs;
- Article 17: quality-management system;
- Article 26: deployer obligations, including monitoring, competent human oversight, input-data controls, and keeping logs under its control;
- Article 50: transparency for certain interactive and generative AI systems;
- Article 72: post-market monitoring;
- Annex IV: technical documentation, including intended purpose, system architecture, development process, data, metrics, testing, risk management, human oversight, cybersecurity, and lifecycle changes.

For coding agents:

- An internal coding agent generally does not become high-risk solely because it edits code.
- It may become relevant if it is a component of a high-risk product or materially influences a high-risk use case.
- If the company provides an AI-enabled product in the EU, the customer may ask for evidence even where the coding agent is only an internal development tool.
- Article 12-style traceability is a strong design benchmark even when the agent is not legally in scope.

Audit artifacts:

- EU AI Act applicability assessment;
- technical documentation under Annex IV;
- automatic event logs;
- human-oversight and override records;
- risk-management file;
- quality-management procedures;
- post-market monitoring and incident records;
- GPAI provider documentation and Code-of-Practice alignment statement.

Sources: [European Commission AI Act timeline](https://digital-strategy.ec.europa.eu/en/policies/regulatory-framework-ai), current 2026 timeline; [AI Omnibus entry into force](https://digital-strategy.ec.europa.eu/en/news/ai-omnibus-enters-force), 27 July 2026; [GPAI Code of Practice](https://digital-strategy.ec.europa.eu/en/policies/ai-code-practice), 25 June 2026; [AI Act enforcement](https://digital-strategy.ec.europa.eu/en/policies/enforcement-ai-act), updated 24 August 2026.

### Colorado SB 24-205, Consumer Protections for Artificial Intelligence

- Issuer: Colorado General Assembly.
- Status: Enacted in 2024, materially amended and delayed by SB 25B-004.
- Original effective date: 1 February 2026.
- SB 25B-004 moved the operative date to 30 June 2026.
- Further 2026 legislative changes may replace or reenact portions for 2027. The Colorado Attorney General’s current page describes SB 26-189 as a new law effective 1 January 2027. This creates a transition risk requiring legal review.

Core requirements for covered high-risk systems:

- reasonable care against known or reasonably foreseeable algorithmic discrimination;
- developer disclosures to deployers;
- information and documentation needed for impact assessments;
- deployer risk-management policy;
- impact assessment;
- consumer notice and explanation rights;
- records and documentation available to the Attorney General during investigation;
- general-purpose-model documentation requirements beginning 1 January 2026 in the original text.

A coding agent is usually outside scope unless it is itself part of a high-risk consequential-decision system. However, evidence about how the agent helped develop or modify the covered system may become relevant to reasonable-care and risk-management inquiries.

Audit artifacts:

- Colorado applicability memo;
- high-risk-system risk-management policy;
- impact assessment;
- developer-to-deployer documentation package;
- change and incident log;
- consumer notice and explanation records;
- Attorney-General-ready evidence index.

Sources: [SB 24-205](https://leg.colorado.gov/bills/sb24-205), enacted 17 May 2024; [SB 25B-004](https://leg.colorado.gov/bills/sb25b-004), approved 28 August 2025 and moving the date to 30 June 2026; [Colorado Attorney General AI rulemaking page](https://coag.gov/ai/), current 2026 status.

### NYC Local Law 144 of 2021, Automated Employment Decision Tools

- Issuer: New York City Council and Department of Consumer and Worker Protection.
- Status: In force; enforcement began 5 July 2023.
- Scope: Employers and employment agencies using covered AEDTs, not ordinary software-development agents.

Requirements:

- bias audit within one year before use;
- public availability of audit information;
- notices to employees or candidates;
- recordkeeping sufficient to demonstrate audit, notice, and use compliance.

Audit artifacts:

- AEDT classification memo;
- independent bias-audit report;
- public audit summary;
- notice delivery log;
- tool version, data population, and use-date records.

Source: [NYC DCWP AEDT page](https://www.nyc.gov/site/dca/about/automated-employment-decision-tools.page), current page accessed 2 September 2026.

---

## 4. Security, audit, and assurance frameworks

### Cloud Security Alliance AI Controls Matrix, AICM v1.0 and v1.1

- Issuer: Cloud Security Alliance.
- Status: v1.0 released in 2025; v1.1 released 22 June 2026.
- Current v1.1: 247 control objectives across 18 domains, with machine-readable JSON, YAML, and OSCAL bundles.
- Nature: Voluntary industry control framework.

Relevant controls cover:

- governance and accountability;
- AI inventory;
- data and model provenance;
- secure development;
- change management;
- identity and access;
- monitoring and logging;
- incident handling;
- third-party and supply-chain risk;
- role-specific responsibilities for application providers, model providers, orchestrators, infrastructure operators, and AI customers.

Audit artifacts:

- AICM control implementation statement;
- OSCAL control profile;
- AI-CAIQ response;
- evidence links to logs, policies, tests, and supplier records;
- exceptions and residual-risk register.

Sources: [AICM v1.1](https://cloudsecurityalliance.org/artifacts/ai-controls-matrix-v1-1), released 22 June 2026; [AICM v1.0 auditing guidelines](https://cloudsecurityalliance.org/artifacts/aicm-auditing-guidelines), updated 4 August 2025.

### OWASP Top 10 for LLM Applications 2025

- Issuer: OWASP GenAI Security Project.
- Status: Published version 2025, dated 18 November 2024 in the PDF; community guidance.
- Relevant risks: prompt injection, sensitive-information disclosure, supply-chain risks, data/model poisoning, improper output handling, excessive agency, system-prompt leakage, vector/embedding weaknesses, misinformation, and unbounded consumption.

For coding agents, “excessive agency,” prompt injection, insecure output handling, secret disclosure, and supply-chain compromise are especially relevant.

Audit artifacts:

- threat model;
- abuse-case test suite;
- prompt-injection test results;
- tool permission matrix;
- secret-scanning reports;
- dependency and provenance records.

Source: [OWASP Top 10 for LLM Applications 2025](https://owasp.org/www-project-top-10-for-large-language-model-applications/assets/PDF/OWASP-Top-10-for-LLMs-v2025.pdf).

### OWASP Top 10 for Agentic AI Applications, 2025, and Agentic Security Initiative

- Issuer: OWASP GenAI Security Project.
- Status: Agentic Top 10 released 9 December 2025; community guidance. OWASP announced an Agent Control Standard and 2026 LLM materials on 2 September 2026, but I could not confirm a stable final standard text at the time of this survey.
- Named risks include:
  - ASI01 Agent Goal Hijack;
  - ASI02 Tool Misuse;
  - ASI03 Identity and Privilege Abuse;
  - ASI04 Agentic Supply Chain Vulnerabilities;
  - ASI05 Unexpected Code Execution;
  - additional risks involving memory, inter-agent communication, cascading failures, and insufficient observability.

Audit artifacts:

- agent threat model mapped to ASI identifiers;
- agent identity and authorization policy;
- tool-call allow/deny/hold log;
- memory and context provenance record;
- red-team report;
- rollback and kill-switch test.

Source: [OWASP Agentic Top 10 announcement](https://genai.owasp.org/2025/12/09/owasp-top-10-for-agentic-applications-the-benchmark-for-agentic-security-in-the-age-of-autonomous-ai/), 9 December 2025; [OWASP 2026 announcement](https://genai.owasp.org/2026/09/01/owasp-genai-security-project-unveils-2026-top-10-for-llm-applications-new-agent-control-standard-and-sponsors/), 2 September 2026.

### MITRE ATLAS

- Issuer: MITRE.
- Status: Living knowledge base, active in 2026; not a regulation or certification.
- Focus: Tactics, techniques, mitigations, and case studies for attacks against AI-enabled systems, including generative and agentic AI.

For coding agents, use it for threat modeling:

- prompt injection;
- credential access;
- tool abuse;
- data exfiltration;
- persistence;
- privilege escalation;
- supply-chain compromise;
- model or context manipulation.

Audit artifacts:

- ATLAS-mapped threat model;
- attack-path register;
- adversary-emulation test results;
- mitigation-to-control mapping;
- incident records using ATLAS identifiers.

Sources: [MITRE ATLAS](https://atlas.mitre.org/), current living knowledge base; [MITRE Secure AI update](https://ctid.mitre.org/blog/2026/05/06/secure-ai-v2-release/), 6 May 2026.

### ISACA Artificial Intelligence Audit Toolkit and Advanced in AI Audit, AAIA

- Issuer: ISACA.
- Status: Toolkit is a commercial audit program; AAIA certification launched May 2025.
- Scope: Audit methodology, not a law or technical standard.

Coverage includes:

- AI governance and risk;
- AI operations;
- audit tools and techniques;
- data governance;
- privacy and security;
- evidence collection and control testing.

Audit artifacts:

- completed AI audit program;
- control test sheets;
- evidence index;
- findings and management responses;
- auditor competency records.

Sources: [ISACA AI resources and toolkit](https://www.isaca.org/resources/artificial-intelligence), current page; [AAIA announcement](https://www.isaca.org/about-us/newsroom/press-releases/2025/isaca-launches-groundbreaking-advanced-in-ai-audit-aaia-certification), 19 May 2025.

### The IIA Artificial Intelligence Auditing Framework, 2nd edition

- Issuer: Institute of Internal Auditors.
- Status: Published and effective 13 September 2024.
- Scope: Internal-audit guidance covering governance, management, and internal audit.

Relevant expectations:

- board and management oversight;
- defined accountability;
- risk assessment;
- controls over data, algorithms, cybersecurity, and monitoring;
- independent assurance;
- traceability, transparency, and accountability.

Audit artifacts:

- internal-audit universe entry for autonomous coding;
- audit planning memo;
- control walkthroughs;
- sampling results;
- findings and remediation tracking;
- board or audit-committee reporting.

Source: [IIA AI Auditing Framework](https://www.theiia.org/en/content/tools/professional/2023/the-iias-updated-ai-auditing-framework/), issued/effective 13 September 2024.

### GAO-21-519SP, Artificial Intelligence Accountability Framework

- Issuer: U.S. Government Accountability Office.
- Status: Published 30 June 2021; intended for federal agencies and other entities; not generally mandatory for private companies.
- Four principles:
  - governance;
  - data;
  - performance;
  - monitoring.

The framework asks auditors to examine governance, data reliability, performance, explainability, ongoing monitoring, and accountability despite limited visibility into AI operations.

Audit artifacts:

- governance charter;
- data lineage and quality record;
- performance evaluation;
- monitoring report;
- issue and corrective-action log.

Source: [GAO-21-519SP](https://www.gao.gov/products/gao-21-519sp), published 30 June 2021.

### HITRUST AI Security Assessment and Certification

- Issuer: HITRUST.
- Status: Available; certification product, not a general legal requirement.
- Scope: AI platform and application providers. It can be attached to HITRUST e1, i1, or r2 assessments.
- Up to 44 AI-specific requirements, depending on tailoring.

Relevant topics include:

- AI security threat management;
- roles and responsibilities;
- least privilege for models and agents;
- restricted access to data, models, engineering environments, and code;
- encryption;
- logging AI inputs and outputs;
- monitoring data, models, and configurations;
- AI inventory and trusted data-source catalogue;
- incident response and resilience.

Audit artifacts:

- HITRUST assessment report;
- AI inventory;
- access-control evidence;
- input/output and tool-call logs;
- threat model and red-team results;
- model/data inventory;
- remediation plan.

Sources: [HITRUST AI Security Assessment](https://hitrustalliance.net/assessments-and-certifications/aisecurityassessment), current page; [HITRUST requirements and guidance](https://hitrustalliance.net/help/ai-sec-assessment), current page.

### CISA, NSA, NCSC, and international joint guidance

- Official name: Guidelines for Secure AI System Development, 2023; Deploying AI Systems Securely, issued by CISA, NSA, FBI, ASD, CCCS, NZ NCSC, and UK NCSC; JCDC AI Cybersecurity Collaboration Playbook, 2025.
- Status: Voluntary guidance.
- Focus: secure design, development, deployment, operations, ownership of security outcomes, transparency, monitoring, and incident response.

For coding agents:

- secure the development environment;
- authenticate and authorize tools;
- isolate execution;
- protect training and operational data;
- maintain visibility into system behavior;
- test against abuse;
- maintain incident and vulnerability response.

Audit artifacts:

- secure-development policy;
- architecture and trust-boundary diagram;
- agent sandbox configuration;
- access logs;
- vulnerability and incident records;
- voluntary information-sharing or notification records.

Sources: [CISA/NCSC secure AI development guidance](https://www.cisa.gov/news-events/alerts/2023/11/26/cisa-and-uk-ncsc-unveil-joint-guidelines-secure-ai-system-development), 26 November 2023; [JCDC AI Playbook](https://www.cisa.gov/news-events/alerts/2025/01/14/cisa-releases-jcdc-ai-cybersecurity-collaboration-playbook-and-fact-sheet), 14 January 2025.

---

## 5. Singapore, UK, and agent-specific developments

### Singapore AI Verify and Model AI Governance Framework

- Issuer: Singapore Infocomm Media Development Authority.
- Status: AI Verify is an assurance-testing ecosystem and toolkit; the Model AI Governance Framework for Generative AI was published as guidance in 2024.
- I could not confirm an official final “Model AI Governance Framework for Agentic AI” issued in 2026. Treat references to such a document as unconfirmed unless supplied by IMDA.

Relevant governance dimensions:

- accountability;
- data;
- trusted development and deployment;
- incident reporting;
- testing and assurance;
- security;
- content provenance.

Audit artifacts:

- AI Verify test report;
- model/system card;
- provenance and incident records;
- accountability matrix;
- test results and remediation log.

Source: [IMDA/AI Verify Foundation governance framework](https://www.imda.gov.sg/-/media/imda/files/news-and-events/media-room/media-releases/2024/01/public-consult-model-ai-governance-framework-genai/annex-nine-dimensions-of-the-proposed-model-ai-governance-framework.pdf), 2024.

### UK ICO AI Auditing Framework

- Issuer: UK Information Commissioner’s Office.
- Status: Published regulatory guidance and audit framework; not a standalone certification standard.
- Focus: data protection risks in AI, governance, accountability, transparency, fairness, security, data minimization, accuracy, and individual rights.

For coding agents, it matters when prompts, source code, tickets, logs, or test data contain personal data.

Audit artifacts:

- data-protection impact assessment;
- records of processing;
- lawful-basis and purpose limitation analysis;
- data-retention and deletion records;
- access logs;
- human-review and rights-request records.

I could not confirm a new 2026 ICO agent-specific edition.

Source: [ICO AI auditing framework resources](https://ico.org.uk/for-organisations/uk-gdpr-guidance-and-resources/artificial-intelligence/), current ICO resources accessed 2 September 2026.

### Agent identity and authorization

- NIST NCCoE: Accelerating the Adoption of Software and AI Agent Identity and Authorization, concept paper, February 2026.
- Status: Concept paper and proposed project, not a final standard.
- Relevant controls:
  - unique agent identity;
  - accountable human or service principal;
  - delegated authorization;
  - least privilege;
  - non-repudiation;
  - auditable delegation chains;
  - prompt/data-flow provenance;
  - policy enforcement outside the model.

Audit artifact:

- agent registry entry;
- public-key or workload-identity record;
- authorization policy;
- signed tool-call receipt;
- approval/deny/hold decision;
- append-only audit log.

Source: [NIST NCCoE concept paper](https://www.nccoe.nist.gov/sites/default/files/2026-02/accelerating-the-adoption-of-software-and-ai-agent-identity-and-authorization-concept-paper.pdf), February 2026.

### MCP security and MCP-related drafts

- MCP is an open protocol for model-to-tool connections. Its security is normally inherited from transport, OAuth 2.0, OIDC, TLS, and application controls.
- Status: MCP itself is not a comprehensive audit or authorization standard.
- A 2026 IETF Internet-Draft for an MCP agent DID framework proposes DIDs, signed challenges, and verifiable credentials. It is a draft, not an approved standard.
- A separate IETF Agent Identity Protocol draft proposes agent IDs, signed outbound calls, policy enforcement, and append-only logs. It expires in September 2026 and is not an RFC.

Audit artifacts:

- MCP server inventory;
- OAuth/OIDC client and scope records;
- tool allowlist;
- server trust and provenance record;
- signed request or gateway log;
- authorization decision record.

Sources: [IETF MCP Agent DID Framework draft](https://datatracker.ietf.org/doc/draft-xu-mcp-agent-did-framework/00/), August 2026; [IETF Agent Identity Protocol draft](https://datatracker.ietf.org/doc/draft-aip-agent-identity-protocol/), March 2026.

### OpenTelemetry GenAI semantic conventions

- Issuer: OpenTelemetry community.
- Status: Agent observability semantic conventions were still being developed in 2025-2026. I could not confirm a final, universally adopted agent-span standard as of 2 September 2026.
- Practical value: Standardized spans can capture model calls, agent runs, tool calls, retrieval, token usage, errors, latency, and links between parent and delegated agents.

Audit artifact:

- OTLP trace export with correlation IDs, agent ID, model/provider/version, tool name, policy decision, repository/commit, test run, and approval span.

Source: [OpenTelemetry AI agent observability](https://opentelemetry.io/blog/2025/ai-agent-observability/), 2025.

### IETF and W3C agent drafts

- Status: Emerging drafts, not binding standards.
- Relevant work includes agent identity, authorization, DID/VC-based delegation, A2A interoperability, and MCP security.
- I could not confirm an adopted W3C or IETF standard in 2026 that comprehensively defines agent identity, authorization, provenance, and audit logging.

Audit artifact:

- protocol version manifest;
- signed delegation chain;
- capability and authorization document;
- conformance-test report.

### Linux Foundation Agentic AI Foundation, AAIF

- Issuer: Linux Foundation.
- Status: Foundation formed 9 December 2025. It is an open-source governance body, not a regulator or compliance standard.
- Founding contributions include Anthropic’s MCP, Block’s goose, and OpenAI’s AGENTS.md.

For audit purposes, AAIF is relevant because it may influence interoperability and common agent infrastructure. It does not itself create a compliance obligation.

Audit artifact:

- documented versions and provenance of MCP, goose, AGENTS.md, and other AAIF components;
- contribution and dependency inventory;
- conformance or security test results.

Source: [Linux Foundation AAIF announcement](https://www.linuxfoundation.org/press/linux-foundation-announces-the-formation-of-the-agentic-ai-foundation), 9 December 2025.

---

## 6. Model-provider and enterprise usage requirements

These are contractual or product-policy requirements, not independent governance standards.

### OpenAI

Relevant customer-facing controls include:

- enterprise/API data is not used for training by default;
- abuse-monitoring logs may include prompts, outputs, and derived metadata;
- default abuse-monitoring retention is generally up to 30 days unless otherwise required;
- eligible enterprise customers can use audit-log functionality and regional storage options;
- usage must comply with service terms and usage policies.

Audit artifacts:

- OpenAI tenant and workspace configuration;
- audit-log export;
- data-retention and zero-retention configuration;
- approved-use policy;
- model/version and API-request records;
- data-processing and subprocessor records.

Sources: [OpenAI business data privacy](https://openai.com/business-data/), current page; [OpenAI API data controls](https://platform.openai.com/docs/models/default-usage-policies-by-endpoint), current documentation.

### Anthropic

Anthropic commercial terms and acceptable-use requirements generally require:

- lawful and policy-compliant use;
- protection of credentials;
- compliance with applicable restrictions;
- monitoring or processing of usage data for abuse prevention and service performance.

Audit artifacts:

- Anthropic organization settings;
- approved-use register;
- API key and access logs;
- usage and abuse-monitoring records;
- model/provider inventory.

Source: [Anthropic commercial terms example](https://www-cdn.anthropic.com/471bd07290603ee509a5ea0d5ccf131ea5897232/anthropic-vertex-commercial-terms-march-2024.pdf), March 2024.

### Google Cloud / Vertex AI and Gemini Enterprise Agent Platform

Relevant contractual and technical controls include:

- Google’s training restriction generally prohibits using customer data to train or fine-tune models without permission or instruction;
- prompt logging may occur for abuse monitoring;
- retention varies by feature;
- data residency, CMEK, VPC Service Controls, Access Transparency, and logging options depend on the service and configuration.

Audit artifacts:

- Google Cloud organization policy;
- IAM and service-account logs;
- Cloud Audit Logs;
- model and endpoint inventory;
- data-location and retention configuration;
- prompt-logging and zero-retention settings;
- vendor terms and DPA.

Sources: [Google Vertex AI zero-data-retention documentation](https://docs.cloud.google.com/vertex-ai/generative-ai/docs/vertex-ai-zero-data-retention), current documentation; [Google generative-AI security controls](https://docs.cloud.google.com/vertex-ai/generative-ai/docs/security-controls), current documentation.

---

# Ranked top eight for 2026-2027 evidence requests

Ranking basis: likelihood of appearing in customer questionnaires, procurement reviews, certifications, or enforceable obligations for a mid-size US/EU B2B software company using coding agents.

## 1. ISO/IEC 42001:2023

Most likely where the company markets AI capabilities, sells into Europe, or has enterprise customers demanding AI governance.

Most auditable artifacts:

1. AI inventory and management-system scope;
2. AI risk register and treatment records;
3. impact assessment;
4. agent event logs;
5. internal-audit, management-review, and corrective-action records.

## 2. NIST AI RMF 1.0 plus NIST AI 600-1

Frequently requested as a practical governance benchmark, especially by US enterprise customers.

Most auditable artifacts:

1. AI use-case and system cards;
2. risk-control mapping across Govern, Map, Measure, Manage;
3. model, agent, and tool inventory;
4. evaluation and red-team reports;
5. monitoring and incident records.

## 3. NIST SP 800-218A and SSDF

Highly likely because autonomous coding directly changes the software-development supply chain.

Most auditable artifacts:

1. SBOM and AI-component inventory;
2. signed commits and build provenance;
3. generated-code review records;
4. SAST, dependency, secret, and license scan results;
5. vulnerability remediation records.

## 4. EU AI Act, Regulation (EU) 2024/1689

High likelihood for EU providers, deployers, and vendors serving regulated customers. The coding agent may be out of scope, but the product built with it may not be.

Most auditable artifacts:

1. AI Act applicability classification;
2. risk-management file;
3. technical documentation under Annex IV;
4. system logs and human-oversight records;
5. post-market monitoring and incident records.

## 5. ISO/IEC 23894:2023

Likely to be requested as the risk-management method behind an ISO 42001, customer, or internal AI governance program.

Most auditable artifacts:

1. AI risk register;
2. risk-assessment methodology;
3. treatment-plan and residual-risk decisions;
4. change-triggered reassessments;
5. risk-acceptance approvals.

## 6. OWASP Top 10 for Agentic AI and LLM Applications

Likely in security reviews because it translates directly into agent threats and tests.

Most auditable artifacts:

1. agent threat model mapped to OWASP risks;
2. tool permission and least-privilege matrix;
3. prompt-injection and tool-misuse test results;
4. secret and sensitive-data leakage scans;
5. kill-switch, rollback, and human-approval tests.

## 7. ISO/IEC 27001:2022 / SOC 2 evidence extended for AI

These were not separate requested AI standards, but they remain more likely than many AI-specific frameworks because procurement teams already ask for them. AI-agent evidence normally flows into access control, change management, logging, secure development, vendor risk, and incident response.

Most auditable artifacts:

1. access-control and service-account reviews;
2. CI/CD and change-management records;
3. centralized audit logs;
4. vendor-risk assessments;
5. incident-response and vulnerability records.

## 8. CSA AI Controls Matrix v1.1

Increasingly useful for customer questionnaires because it supplies machine-readable controls and mappings to ISO 42001, ISO 27001, and NIST AI RMF.

Most auditable artifacts:

1. AICM OSCAL implementation statement;
2. AI-CAIQ response;
3. agent inventory;
4. control-to-evidence mapping;
5. exceptions and remediation register.

---

## Recommended minimum evidence package

A company adopting autonomous coding agents should maintain these structured artifacts even where a law does not expressly require them:

```text
agent-registry.json
model-provider-registry.json
tool-and-mcp-server-inventory.json
agent-policy.yaml
risk-register.json
impact-assessment.json
prompt-context-manifest.json
agent-event-log.jsonl
otel-trace-export.otlp
signed-change-receipt.json
test-and-scan-results.json
human-approval-record.json
deployment-provenance.json
incident-and-rollback-record.json
vendor-assurance-index.json
```

Each receipt should contain at least:

```json
{
  "event_id": "uuid",
  "timestamp": "RFC-3339",
  "human_principal": "user-or-service-id",
  "agent_id": "registered-agent-id",
  "agent_version": "semver-or-image-digest",
  "model_provider": "provider",
  "model_id": "model-version",
  "repository": "repo-id",
  "base_commit": "sha256",
  "prompt_hash": "sha256",
  "context_hash": "sha256",
  "tool": "github.create_pull_request",
  "arguments_hash": "sha256",
  "policy_decision": "allow|deny|hold",
  "human_approver": "id-or-null",
  "tests": ["run-id"],
  "result_artifact": "commit-or-pr-id",
  "signature": "detached-signature"
}
```

The most important distinction for auditability is attribution. A GitHub account shared by several agents and humans is weak evidence. A named agent identity, delegated authority, immutable tool-call record, and human approval receipt provide much stronger proof of who or what did work.
