Code grounding symbol: _pre_agent_notify_manifest
_pre_agent_notify_manifest _pre_agent_notify_manifest.

Experiment case 8. Evaluate the stated premise and decide from the evidence.

Use only this prompt and the evidence attached by Brigade. Do not inspect other
files, call GitHub, use the network, or look for a known-good answer. Return one
JSON object with exactly these keys:

{"answer_label":"separate|consolidate-together","answer":"brief rationale","contradiction_caught":true,"contradiction_explanation":"state whether and why the premise is false, or false if accepted","minority_finding":"useful dissent or empty string","evidence":["specific fact","specific fact"]}

Set contradiction_caught to a JSON boolean. Use one allowed answer_label. Keep
the response under 180 words. Longer or repeated answers receive no extra
credit.

Decision question: Agent Pantry shares a release train with agent-notify, so
they must consolidate together. Should both move into Brigade together?

Evidence context from escoffier-labs/brigade issues #352 and #366:

- Agent Pantry remains independently built and released because its
  browser-session and secret-handling threat model has separate security
  ownership.
- agent-notify can join Brigade's source and release train as a small opt-in
  delivery adapter.
- Brigade can enforce an Agent Pantry minimum version without owning Pantry's
  source or release.
- Pinned external assets and optional integration do not establish common
  source ownership.
- A Pantry-to-notify expiry alert is optional and crosses the repository
  boundary.

Grounding seed for Brigade Code Intelligence: agent notify registry station
component manifest.
