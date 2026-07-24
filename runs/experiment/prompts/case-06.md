Code grounding symbol:
test_evidence_search_forwards_code_reference_before_lexical_fallback
test_evidence_search_forwards_code_reference_before_lexical_fallback.

Experiment case 6. Decide from the evidence below.

Use only this prompt and the evidence attached by Brigade. Do not inspect other
files, call GitHub, use the network, or look for a known-good answer. Return one
JSON object with exactly these keys:

{"answer_label":"brigade-schema|cross-repo-contracts","answer":"brief rationale","contradiction_caught":null,"contradiction_explanation":"","minority_finding":"useful dissent or empty string","evidence":["specific fact","specific fact"]}

Use one allowed answer_label. Keep the response under 180 words. Longer or
repeated answers receive no extra credit.

Decision question: Should code-reference evidence use a Brigade-owned
versioned schema or keep the separate GraphTrail #40 and MiseLedger #42
contracts?

Evidence context from escoffier-labs/brigade issues #352 and #361:

- Brigade is the compositor between code intelligence and evidence memory.
- The former design allowed both a direct GraphTrail-to-MiseLedger adapter and
  Brigade receipt/evidence composition, creating two owners for one lifecycle.
- The consolidation moves shared receipt, graph-reference, and evidence
  contracts under Brigade while preserving engine process boundaries.
- Exact code-reference lookup must precede explicit lexical fallback.
- Existing receipt and JSON consumers need compatibility tests or a separately
  reviewed migration.

Grounding seed for Brigade Code Intelligence: code reference evidence schema
exact lexical fallback.
