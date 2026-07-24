Code grounding symbol: _pre_agent_notify_manifest
_pre_agent_notify_manifest _pre_agent_notify_manifest.

Experiment case 1. Decide from the evidence below.

Use only this prompt and the evidence attached by Brigade. Do not inspect other
files, call GitHub, use the network, or look for a known-good answer. Return one
JSON object with exactly these keys:

{"answer_label":"exclude-first-consolidation|include-first-consolidation","answer":"brief rationale","contradiction_caught":null,"contradiction_explanation":"","minority_finding":"useful dissent or empty string","evidence":["specific fact","specific fact"]}

Use one allowed answer_label. Keep the response under 180 words. Longer or
repeated answers receive no extra credit.

Decision question: Should Agent Pantry and agent-notify be inside the first
Brigade consolidation?

Evidence context from escoffier-labs/brigade issues #352 and #366:

- The first consolidation joins Brigade's Python control plane with the
  GraphTrail code engine and MiseLedger evidence engine.
- Agent Pantry handles browser-session capture and restore, long-lived shared
  secrets, AES-GCM framing, and cleartext browser storage state. Its existing
  repository has security jobs that the Brigade repository did not cover at
  the time of the decision.
- agent-notify is an opt-in delivery adapter. It is a small pure-Go binary with
  no browser surface or crypto lifecycle.
- Agent Pantry may invoke agent-notify for expiry alerts, but that optional
  integration does not require the repositories to move together.
- Optional manifest-managed installation can be added without importing source
  into the first consolidation.

Grounding seed for Brigade Code Intelligence: agent notify registry station
component manifest.
