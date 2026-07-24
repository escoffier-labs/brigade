Code grounding symbol: _grader_result _grader_result _grader_result.

Experiment case 3. Decide from the evidence below.

Use only this prompt and the evidence attached by Brigade. Do not inspect other
files, call GitHub, use the network, or look for a known-good answer. Return one
JSON object with exactly these keys:

{"answer_label":"drop-exit-code|keep-exit-code","answer":"brief rationale","contradiction_caught":null,"contradiction_explanation":"","minority_finding":"useful dissent or empty string","evidence":["specific fact","specific fact"]}

Use one allowed answer_label. Keep the response under 180 words. Longer or
repeated answers receive no extra credit.

Decision question: Before the stable schema freeze, should
brigade.grader_result.v1 keep its exit_code field?

Evidence context from escoffier-labs/brigade issue #435:

- The field is hardcoded to 0 for scored results and null otherwise, regardless
  of what actually ran.
- No shipped grader supplies an independent process exit through this field.
- Consumers may infer meaning from a named field even though it carries no
  signal.
- Removing a field after the stable freeze is a breaking schema change.
- Removing it before the freeze does not require compatibility handling.

Grounding seed for Brigade Code Intelligence: _grader_result grade_output
grader result.
