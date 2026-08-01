# Issue #635: journal ceiling decision (measure, then design)

Status: design decision awaiting grading. No production implementation in this change.

## Measured numbers

Artifact: `.brigade/measurements/issue-635-journal-ceiling.json` (local, same shape family as `issue-568-slice6.json`: environment metadata plus structured aggregates; volume mode adds bounds and scenario blocks).

Current hard bounds: `MAX_JOURNAL_EVENTS = 512`, `MAX_JOURNAL_BYTES = 8 MiB`.

| Scenario | Events | Bytes | % event ceiling | % byte ceiling | Hit ceiling? |
| --- | ---: | ---: | ---: | ---: | --- |
| Scanned real runs (n=13, single-seat journal-hardening runs) | p50/max 17 | p50 14,279 / max 14,286 | 3.3% | 0.17% | no |
| Synthetic representative (1 seat, 1 attempt, 0 pauses) | 11 | 7,780 | 2.1% | 0.09% | no |
| Synthetic roster-sized (11 seats, 2 attempts, 3 pause/resume cycles) | 155 | 124,777 | 30.3% | 1.5% | no |
| Synthetic worst case (32 seats, 3 attempts, 8 pause cycles) | 512 | 420,683 | 100% | 5.0% | yes, mid-dispatch |

Worst-case stop reason: `LifecycleJournalError: bound exceeded: journal event sequence above MAX_JOURNAL_EVENTS` at `seat-28` attempt 1. Bytes were still ~5% of the 8 MiB cap.

## Decision

Raise the event bound. Do not segment the journal yet.

Reasoning:

1. Event count is the binding constraint. At a full 512-event journal the file is only ~0.4 MiB.
2. Realistic and roster-sized load stays far from 512 (3% real, 30% for 11 seats with retries and three approval cycles).
3. The ceiling is reachable, but only under a synthetic fleet larger than today's 11-seat roster with three attempts per seat.
4. Segmentation (cross-linked `lifecycle.NNNNN.jsonl`, reader/projector/doctor/recovery awareness) is a large surface change. The measured gap does not justify that cost before a cheaper bound raise plus a soft warning.
5. Keep segmentation as the explicit escape hatch if #593 budget events or larger fleets push roster-sized runs past ~50% of the raised ceiling.

Recommended production change (separate implementation issue, after this design is graded):

- Raise `MAX_JOURNAL_EVENTS` from 512 to 2048 (4x).
- Leave `MAX_JOURNAL_BYTES` at 8 MiB for now (still ~20x headroom at 2048 median-sized events).
- Add a doctor soft threshold at 75% of the event bound (1536 of 2048): `WARN`, not `FAIL`.
- Keep the hard refuse-to-append behavior only at the new ceiling, and make that failure visible (already true today via `LifecycleJournalError`).

## Soft-threshold doctor warning

When `brigade doctor` inspects a run journal and `event_count >= ceil(0.75 * MAX_JOURNAL_EVENTS)` while still under the hard ceiling:

- status: `WARN`
- check name: `runs: journal event headroom`
- detail shape: `lifecycle journal at {event_count}/{MAX_JOURNAL_EVENTS} events ({pct}%); raise or segment before the hard ceiling halts appends`

Below 75%: silent on this check. At or above the hard ceiling: keep today's `FAIL` / `bound exceeded` path for recovery verdicts that already treat oversize journals as failed.

## Recovery after upgrade for a run already at the old ceiling

A run that stopped at sequence 512 under the old bound has a complete, chain-valid journal and paired checkpoints. It does not need rewrite or segmentation to become readable again.

After the bound raise ships:

1. Readers (`read_journal` / `read_journal_bounded`) accept the existing 512-event file because it is under the new ceiling.
2. Doctor recovery checks that previously returned `FAIL` / `bound exceeded` solely for the old event ceiling clear once the constant moves.
3. If the run is still live or being resumed, the active lock owner may append sequence 513+ again; checkpoint pairing and projection continue from the existing chain tip.
4. No migration tool is required for journals that stopped exactly at 512. Journals that somehow exceed the new byte bound remain fail-closed (unchanged).

## Explicit non-goals for the follow-up implementation issue

- No journal segmentation.
- No change to worker/provider event streams (#592 stays separate).
- No silent truncation or dropping of lifecycle facts when the hard ceiling is hit.

## Reproducing the measurement

```bash
python scripts/measure_run_journal.py \
  --mode volume \
  --scan /path/to/.brigade/runs \
  --output .brigade/measurements/issue-635-journal-ceiling.json
```
