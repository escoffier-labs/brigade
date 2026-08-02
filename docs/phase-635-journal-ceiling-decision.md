# Issue #635: journal ceiling decision (measure, then design)

Status: implemented by issue #653.

## Measured numbers

Artifact: `docs/measurements/issue-635-journal-ceiling.json`, SHA-256 `db9a585b6c2b80ca5e5bfc2c1411d678754cecc2f8f6911685da5cc00a381c89`.

Current hard bounds: `MAX_JOURNAL_EVENTS = 2048`, `MAX_JOURNAL_BYTES = 8 MiB`.

| Scenario | Events | Bytes | % event ceiling | % byte ceiling | Hit ceiling? |
| --- | ---: | ---: | ---: | ---: | --- |
| Synthetic representative (1 seat, 1 attempt, 0 pauses) | 11 | 7,780 | 0.54% | 0.09% | no |
| Synthetic roster-sized (11 seats, 2 attempts, 3 pause/resume cycles) | 155 | 124,777 | 7.57% | 1.49% | no |
| Synthetic worst case (32 seats, 3 attempts, 8 pause/resume cycles) | 629 | 510,900 | 30.71% | 6.09% | no |

The captured synthetic scenarios completed without a stop reason. The artifact has no scanned real-run inputs; supplied scan roots now fail when missing, unreadable, or empty.

## Decision

Raise the event bound. Do not segment the journal yet.

Reasoning:

1. Event count remains the binding constraint. The worst synthetic run reaches 629 events while using 510,900 bytes.
2. The 11-seat roster scenario reaches 7.57% of the raised event ceiling.
3. The 32-seat scenario records all eight pause/resume cycles and reaches 30.71% of the raised event ceiling.
4. At the measured worst-case average event size, 2,048 events consume about one fifth of the byte bound, leaving about 5x byte headroom.
5. Segmentation remains the escape hatch if #593 budget events or larger fleets push roster-sized runs past about 50% of the raised ceiling.

Implemented production change:

- Raise `MAX_JOURNAL_EVENTS` from 512 to 2048 (4x).
- Leave `MAX_JOURNAL_BYTES` at 8 MiB for now (about 5x byte headroom at 2,048 measured-size events).
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
  --output docs/measurements/issue-635-journal-ceiling.json
```

Add `--scan /path/to/.brigade/runs` only for an existing readable root that contains lifecycle journals.
