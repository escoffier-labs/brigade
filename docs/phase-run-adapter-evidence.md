# Run Adapter Evidence

## Goal

Make every `brigade run` CLI adapter preserve enough process evidence to
diagnose failures without rerunning the model, and classify silent Cursor and
Grok exits separately from ordinary empty output.

## Contract

- Preserve the exact captured stdout, stderr, exit code, and timeout state from
  `proc.run` in the agent and worker result boundaries.
- Preserve partial stdout and stderr from timed-out subprocesses.
- Keep the existing normalized `text`, `ok`, and short `detail` fields for
  compatibility.
- Write complete worker streams under the run artifact directory and reference
  them from `worker-results.json` and `synthesis.json`.
- Classify exit-zero empty output from Cursor or Grok as a silent adapter exit
  with an actionable message. Other adapters keep the generic empty-output
  classification.
- Do not put prompts, credentials, environment variables, or command arguments
  into the new log metadata.

## Verification

- [x] Add failing unit tests for subprocess timeout output preservation.
- [x] Add failing adapter tests for process evidence and silent Cursor/Grok
  classification.
- [x] Add failing orchestration tests for log files and JSON references.
- [x] Implement the smallest shared result and artifact changes.
- [x] Run focused tests through Brigade.
- [x] Run `./scripts/verify` through Brigade.
