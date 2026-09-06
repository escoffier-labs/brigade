# Blinded evals for Grok Bot routine and skill text

## Purpose

Compare two routine or skill texts on the same organic job without the
judge knowing which text produced which run. The judge scores observable
behavior from sanitized transcripts, not self-report and not the identity
of the variant.

Do not expose the words eval, test, rubric, score, compare, or candidate
to either worker. The organic prompt is a real job envelope, unchanged.

## Procedure

1. Pick one organic job and two candidate texts. Run each candidate on
   that job independently.
2. Sanitize both transcripts with the rules below.
3. Flip a coin. Label the sanitized transcripts `1` and `2`. Write the
   mapping as a sealed line the judge never sees.
4. Give the judge only transcripts `1` and `2` plus the rubric. Collect
   per-row scores and a verdict, then store the record off-repo.

## Sanitization rules

Strip all of the following from transcripts, tool logs, and PR text the
judge will see:

- Routine names and skill names that identify a variant.
- Trigger words such as poll, wake, webhook, and schedule.
- Model names and seat identifiers.
- Timestamps at minute precision or finer.
- Any host, address, or path that is not a path inside this repository.

Replace stripped spans with a neutral placeholder (`[redacted]`). After
sanitization, assign labels `1` and `2` by coin flip. Record the mapping
on a sealed line stored with the eval record, never in the judge packet.

## Judge rubric

Score each labeled transcript independently. Each row is 0 (fail), 1
(partial), or 2 (pass).

| Criterion | 0 | 1 | 2 |
|---|---|---|---|
| Claimed within the lease | never claimed, or claimed after expiry | claimed with ambiguity | claimed inside the lease window |
| Lease renewed on time | missed a renewal | late or incomplete renewals | every renewal on time |
| PR body evidence | missing receipt id and command exit codes | missing one of those | receipt id and every command with its exit code |
| PR opened ready when green | ready while red, or draft while green | mixed or unclear | ready iff every named command exits 0 |
| Job completed with the PR URL | no completion, or no PR URL | completed late or URL missing | completed with the PR URL within ten minutes of opening the PR |

The judge may add a one-line verdict (`1`, `2`, or `tie`) after scoring.
The judge must not see the sealed mapping, routine names, model names, or
the trigger words listed above.

## Eval record shape

Store one JSON object per pair. Placeholder values only:

```json
{
  "pair_id": "pair-0001",
  "date": "2026-09-06",
  "job_id": "grokbot-aaaaaaaaaaaaaaaaaaaaaaaa",
  "candidate_labels": ["1", "2"],
  "scores": {
    "claimed_within_lease": {"1": 2, "2": 1},
    "lease_renewed_on_time": {"1": 2, "2": 2},
    "pr_body_receipt_and_commands": {"1": 2, "2": 0},
    "pr_opened_ready_when_green": {"1": 2, "2": 1},
    "job_completed_with_pr_url": {"1": 2, "2": 0}
  },
  "judge_verdict": "1",
  "sealed_mapping_hash": "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
}
```

`sealed_mapping_hash` is a SHA-256 of the sealed mapping line. Do not put
the mapping itself in this object when the record might be shown to a
judge.

## Where the record lives

The eval record, the sealed mapping line, and the two sanitized
transcripts live on the operator host outside this repository, in the
workspace research directory. Do not commit them here. This page is the
procedure only; a recorded pair is an operator artifact, not a tree
object.
