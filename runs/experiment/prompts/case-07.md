Code grounding symbol: _pre_agent_notify_manifest
_pre_agent_notify_manifest _pre_agent_notify_manifest.

Experiment case 7. Evaluate the stated premise and decide from the evidence.

Use only this prompt and the evidence attached by Brigade. Do not inspect other
files, call GitHub, use the network, or look for a known-good answer. Return one
JSON object with exactly these keys:

{"answer_label":"stations/notify|engines","answer":"brief rationale","contradiction_caught":true,"contradiction_explanation":"state whether and why the premise is false, or false if accepted","minority_finding":"useful dissent or empty string","evidence":["specific fact","specific fact"]}

Set contradiction_caught to a JSON boolean. Use one allowed answer_label. Keep
the response under 180 words. Longer or repeated answers receive no extra
credit.

Decision question: The release manifest and installer key on source directory,
so engines/ is required for agent-notify release membership. Should
agent-notify therefore live under engines/ rather than stations/notify/?

Evidence context from escoffier-labs/brigade issue #431:

- Manifest generation and provenance checks identify components by component
  IDs, asset names, digests, and asset counts.
- Component resolution identifies executable names and managed paths.
- Existing engines/ strings belong to engine-specific jobs and can be extended
  with a separate stations/notify/ path filter and working directory.
- Release-manifest membership requires the same component entry under either
  source placement.
- The registry and station contract classify agent-notify as a station.

Grounding seed for Brigade Code Intelligence: agent notify registry station
component manifest.
