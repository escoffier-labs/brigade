# Issue #442 experiment artifacts

This directory contains the evidence for the grounded-deliberation experiment
specified in issue #442.

## Index

- `cases/`: evidence contexts with the held-out known-good answers
- `prompts/`: answer-free prompts supplied to the routes
- `config/`: the three Brigade roster configurations
- `raw/`: copied Brigade run artifacts and model outputs
- `command-logs/`: command boundaries, exit codes, latency, stdout, and stderr
- `scored-results.json`: machine-readable factual scoring
- `scored-comparison.md`: evidence table and recommendation
- `token-accounting.json`: provider token records matched to command intervals
- `account-tokens.py`: accounting script
- `run-case.sh` and `retry-sample.sh`: run harness

The known-good answers were not present in the answer-free prompts or sandbox
worktrees. They were added to `cases/` only after all routes finished.

## Apparatus note

The first two Route B groups attempted their three samples concurrently. The
Brigade run lock rejected two samples in each group before dispatch. The raw
failed receipts remain in place, and the rejected samples were rerun
sequentially. Cases 3 through 8 ran sequentially from the start.

Route C failed during planning in every case and made no model calls. Its raw
failure receipts and command logs are retained.

Tracked artifacts replace local absolute paths with readable placeholders.
