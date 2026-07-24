Code grounding symbol: _pre_agent_notify_manifest
_pre_agent_notify_manifest _pre_agent_notify_manifest.

Experiment case 2. Decide from the evidence below.

Use only this prompt and the evidence attached by Brigade. Do not inspect other
files, call GitHub, use the network, or look for a known-good answer. Return one
JSON object with exactly these keys:

{"answer_label":"stations/notify|engines","answer":"brief rationale","contradiction_caught":null,"contradiction_explanation":"","minority_finding":"useful dissent or empty string","evidence":["specific fact","specific fact"]}

Use one allowed answer_label. Keep the response under 180 words. Longer or
repeated answers receive no extra credit.

Decision question: Should agent-notify live at stations/notify/ or under
engines/?

Evidence context from escoffier-labs/brigade issue #431:

- The component manifest, provenance checks, installer, and resolver key on
  component IDs, asset names, and exact asset counts, not source directories.
- Existing literal engines/ paths belong to current engine-specific CI and
  build jobs. A new source tree can receive its own path filter and working
  directory.
- A registered station under engines/ conflicts with the station registry and
  current station contract.
- Release-manifest membership is required under either directory, so engines/
  removes none of that release work.
- A stations/ placement needs a scoped Go job and publish working-directory
  override.

Grounding seed for Brigade Code Intelligence: agent notify registry station
component manifest.
