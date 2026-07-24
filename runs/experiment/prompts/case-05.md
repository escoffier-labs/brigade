Code grounding symbol: resolve_capabilities resolve_capabilities
resolve_capabilities.

Experiment case 5. Decide from the evidence below.

Use only this prompt and the evidence attached by Brigade. Do not inspect other
files, call GitHub, use the network, or look for a known-good answer. Return one
JSON object with exactly these keys:

{"answer_label":"incremental-463|from-scratch-462","answer":"brief rationale","contradiction_caught":null,"contradiction_explanation":"","minority_finding":"useful dissent or empty string","evidence":["specific fact","specific fact"]}

Use one allowed answer_label. Keep the response under 180 words. Longer or
repeated answers receive no extra credit.

Decision question: Which roster change should land, the from-scratch work in
#462 or the incremental work in #463?

Evidence context from escoffier-labs/brigade issue #444 and pull requests #454,
#462, and #463:

- #454 already merged packaged presets and fallback resolution in roster.py.
- #462 started from an older base and re-implemented the same parts with a new
  roster_resolution.py module and another minimal.toml preset.
- #462 conflicts with the merged resolver.
- #463 extends the resolver already merged in #454 with roster suggestion,
  receipt-backed stats, and doctor warnings.
- #463 does not add a second resolver module and keeps existing presets.

Grounding seed for Brigade Code Intelligence: roster suggest
resolve_capabilities load_roster fallback.
