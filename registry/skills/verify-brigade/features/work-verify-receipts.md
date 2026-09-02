# work verification receipts

`brigade work verify run` is how a result counts. It executes the commands you
name, writes a receipt under `.brigade/work/verify-runs/<run-id>/`, and can
attribute the outcome to a skill or card in the same step. Everything in the
outcome ledger and the `brigade work brief` loop is downstream of this.

## Sub-features

- `--command "<shell command>"`, repeatable. Rejected when it contains shell
  metacharacters that the safety check does not allow.
- `--argv-json '["prog","arg"]'` for one command executed with `shell=False`,
  which is how you pass quotes, semicolons, and parentheses.
- `--manifest <id>` for a registered verifier manifest with scoreable
  subject/check metadata. Mutually exclusive with the other two.
- `--capture <artifact-id>` (with `--capture-kind skill|card`) closes the loop
  atomically: pass and failure both land in the outcome ledger, no separate
  `brigade outcome capture` step.
- `--timeout <s>` per command; `--no-reuse` to defeat the identical-tree
  passing-receipt reuse.
- Readers: `brigade work verify runs --limit N --json` and
  `brigade work verify show <run-id|latest> --json`.

## How to get to it (user POV)

An agent finishing a change in a Brigade-wired repo runs, per `AGENTS.md`:

```bash
brigade work verify run --target . \
  --argv-json '["./scripts/verify-focused","tests/test_thing.py"]' \
  --capture brigade-work
```

and reports the `run_id` and `status`. Later, someone reads the receipt back
with `brigade work verify show <run-id> --json`.

## Driving it with control-brigade

```bash
C=registry/skills/verify-brigade/control-brigade.py
$C work-verify --target "$TARGET" --capture verify-brigade
```

The default probe is `python3 --version`: no shell metacharacters, deterministic,
exit 0. Override with `--command`, and bound the inner command with
`--command-timeout`.

Proof, from the helper's normalized output:

- `"status": "completed"` and `"ok": true` (helper exit 0)
- `"run_id"` present, shaped `<YYYYMMDD>-<HHMMSS>-work-verify-<hex>`
- `"receipt_path"` points at a directory that exists
- `"outcome_capture": {"artifact_id": "verify-brigade", "artifact_kind": "skill"}`
  when `--capture` was passed

Then look at the receipt on disk - stdout is the claim, the receipt is the
evidence:

```bash
ls "$TARGET/.brigade/work/verify-runs/"
```

Observed on a fresh target: `run_id 20260901-024652-work-verify-757d99`,
`status completed`.

To prove a *failing* verification is recorded too (it must be - failures are
the signal the outcome ledger needs):

```bash
$C work-verify --target "$TARGET" --command "false" --capture verify-brigade
```

Observed: helper exit 3, `"status": "failed"`, and a receipt written anyway at
`20260901-024903-work-verify-ae8f0f`.

## Gotchas

- The `--command` shell-metacharacter check rejects things that look fine to a
  human. `python3 -c 'print(1)'` passes; anything with `;`, `&&`, `|`, or
  backticks does not. Use `--argv-json` for those.
- `--capture` is mandatory in this repo's hook policy. A raw
  `brigade work verify run` without `--capture brigade-work` is refused before
  it runs, and no receipt exists to point at.
- `--capture` does not make a receipt *scoreable*. `--command` and `--argv-json`
  receipts are audit-only: they carry no verifier-authored `subject_binding`, so
  `outcome rank` and the promotion ratchet never count them. Only
  `--manifest <id>`, against a manifest tracked under `verify/manifests/`, yields
  an eligible receipt. Each `--capture` run prints its verdict
  (`scoreable: yes` / `warning: scoreable: no (reason=...)`) and `--json` carries
  it as `outcome_scoreability`. Read `docs/outcome-scoring.md` before treating a
  green receipt as a fed loop.
- Passing receipts are reused when the tree fingerprint is unchanged, so a
  second identical run can return the earlier `run_id`. That is correct
  behavior, not a stale result. `--no-reuse` forces execution - but never pass
  it to the repo's full `./scripts/verify` gate.
- Status 75 from the full gate means another full verification already holds
  the lock for this checkout. Do not retry it with a bigger timeout; run
  `./scripts/verify-focused` or wait.
- The receipt captures a code-graph delta by shelling out to the `graphtrail`
  binary. On a host without it, `code_graph_delta` comes back
  `{"ok": false, "status": "unavailable", ...}` and the run still completes -
  do not read a missing binary as a failed verification.
- `run_id` is not a join key across targets. Two different targets can hold
  receipts with unrelated ids; always carry the target path alongside.
