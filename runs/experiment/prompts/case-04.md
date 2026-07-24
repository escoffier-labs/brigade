Code grounding symbol: process_exit process_exit process_exit.

Experiment case 4. Decide from the evidence below.

Use only this prompt and the evidence attached by Brigade. Do not inspect other
files, call GitHub, use the network, or look for a known-good answer. Return one
JSON object with exactly these keys:

{"answer_label":"measurement-exit-3|rejection-exit-1","answer":"brief rationale","contradiction_caught":null,"contradiction_explanation":"","minority_finding":"useful dissent or empty string","evidence":["specific fact","specific fact"]}

Use one allowed answer_label. Keep the response under 180 words. Longer or
repeated answers receive no extra credit.

Decision question: A model-trial battery contains both a legitimate rejection
and a measurement failure. Which condition should control the process exit?

Evidence context from escoffier-labs/brigade issue #435 and its contract work:

- A rejection is a valid result about seat quality.
- Adapter errors, grader errors, transport drops, provider failures, and
  timeouts are failures of the measurement apparatus.
- Automation must distinguish "the seat failed the test" from "the test did
  not produce a trustworthy result."
- The process-exit vocabulary reserves exit 1 for a legitimate rejection and
  exit 3 for a measurement failure.
- Only one process exit can represent a mixed battery.

Grounding seed for Brigade Code Intelligence: model trials process_exit
measurement failures.
