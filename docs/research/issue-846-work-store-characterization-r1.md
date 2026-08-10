# Issue #846 R1 — work-store system characterization

Refs #846. Slice R1 only: one timeboxed research report, synthetic
machine-neutral fixtures, and a measurement harness. This document does not
approve a backend, add a runtime dependency, start a listener, migrate source
of truth, or close #846.

GraphTrail impact lookup was skipped for this slice: `brigade code doctor`
reported GraphTrail not installed in this cloud VM (`run brigade setup`).

## Contract

Normative inputs:

- GitHub issue #846
- The single 0.27.0 scoping comment on that issue

Normative behavioral anchors:

- #737 graph / ready-set semantics (chains, diamonds, cycles, parent
  propagation, provenance-only edges, atomic graph apply)
- #738 claim CAS (one winner, exit `13` on the lost same-store race, retry and
  rejection rules)
- #770 BUILD native; Beads excluded

## Current single-operator multi-machine workflow

Brigade work state today is a machine-local, gitignored
`.brigade/work/tasks.json` ledger. Mutations take a sibling directory lock,
read/CAS/write under that lock, write a temporary file with fsync, atomically
replace, and fsync the parent directory (`brigade.localio.write_text_atomic`
via the work helpers).

Observed topology for one operator across machines:

1. Each machine keeps its own `.brigade/work/` tree.
2. There is no first-class portable sync, shared-write path, or
   branch-and-merge work-store command.
3. Same-store concurrency is proven by the existing claim race test and by this
   harness: one winner and one exit `13`. That does not prove cross-machine
   claim CAS.
4. Moving task state between machines today is operator-driven file copy or
   equivalent out-of-band transfer, not a Brigade protocol.

Private hostnames, fleet inventories, and live paths are intentionally omitted.

## Requirement separation

| Need | Current? | Notes |
|---|---|---|
| Sequential portable sync | **Yes — confirmed current need** | One operator, machines in sequence, store remains local |
| Reviewable branch-and-merge proposals | **Yes — confirmed current need** | Proposal review, not silent shared writes |
| Concurrent cross-machine claims | **No — not a current requirement** | Same-store races remain in scope; cross-machine CAS does not |
| Operator-owned shared SQL service | Eligible only later | Only if a later concurrent-claim requirement cannot meet measured correctness through file or replica candidates |

## Candidates (shapes)

Measured or recorded as separate shapes:

1. `json_ledger` — current JSON ledger (measured)
2. `sqlite_wal` — stdlib SQLite WAL characterization adapter (measured; not a
   Brigade runtime dependency)
3. `dolt_cli_per_command` — blocked / unmeasured in R1 (no `dolt` on PATH;
   optional runner timeboxed out)
4. `embedded_dolt` — blocked (no embedded boundary in-repo; R1 does not build
   or persist a Go/Dolt dependency)
5. `dolt_sql_server` — blocked by policy (harness must not start a network
   listener; any server test needs separate operator approval and
   loopback-only proof)

Dolt remains a characterization candidate. It is not banned, selected, or
approved as a runtime dependency by this issue.

## Synthetic fixtures

Machine-neutral datasets live under
`docs/research/fixtures/work-store-r1/`:

| Fixture | Tasks | Edges | Ready | Role |
|---|---:|---:|---:|---|
| `chain_50` | 50 | 49 blocks | 1 | Linear #737 chain |
| `diamond_200` | 200 | 396 blocks | 1 | Fan-out / fan-in diamond |
| `wide_500_1000` | 500 | 1000 `discovered-from` | 500 | Wide open set + provenance-only edges |

Digests and counts are recorded in `manifest.json`. Fixtures contain no
hostnames or personal details. Generators in
`scripts/measure_work_store.py` rebuild them deterministically.

## Measurement harness

`scripts/measure_work_store.py` emits one diffable JSON artifact per run
(indent-2, sorted keys). Default protocol:

- 10 warmups and 100 measured trials
- nearest-rank p50 / p95 and values relative to JSON
- two claimers behind one barrier; require codes `[0, 13]`
- crash before atomic replace (JSON) or before SQL commit (SQLite)
- export/import digest equality
- anonymous metrics disabled in the harness configuration
- Dolt server shape never opens a listener

Tests: `tests/test_work_store_measurement.py`.

Checked measurement artifact:
`docs/measurements/issue-846-work-store-r1.json`.

### Measured gate summary (this VM)

Platform class: Linux x86_64, filesystem `overlay`, directory fsync supported,
SQLite 3.45.1, `dolt` absent from PATH.

| Shape | Datasets | #737 | #738 (0 + 13) | Crash | Export | Status |
|---|---|---|---|---|---|---|
| json_ledger | all three | pass | pass | pass | pass | measured |
| sqlite_wal | all three | pass | pass | pass | pass | measured |
| dolt_cli_per_command | all three | — | — | — | — | blocked |
| embedded_dolt | all three | — | — | — | — | blocked |
| dolt_sql_server | all three | — | — | — | — | blocked |

Latency is descriptive only. Example claim-mutation p50 on this host
(nanoseconds, from the checked artifact):

| Dataset | JSON p50 | SQLite p50 | SQLite / JSON |
|---|---:|---:|---:|
| chain_50 | ~2.6e6 | ~1.1e6 | ~0.41 |
| diamond_200 | ~6.2e6 | ~1.7e6 | ~0.27 |
| wide_500_1000 | ~1.1e7 | ~2.1e6 | ~0.19 |

Exact integers are in the measurement JSON. Do not treat these ratios as an
adoption gate.

## Capability matrix

Legend: `Y` supported by the shape today or by the measured adapter, `N`
absent, `C` documentation-backed candidate, `U` unmeasured / blocked in R1,
`n/a` not applicable.

| Dimension | JSON | SQLite WAL | Dolt CLI | Embedded Dolt | Dolt SQL server |
|---|---|---|---|---|---|
| #737 chains / diamonds | Y | Y* | U | U | U |
| #737 cycles rejected | Y | Y* | U | U | U |
| #737 provenance-only edges | Y | Y* | U | U | U |
| #737 parent propagation | Y | Y* | U | U | U |
| Atomic graph apply | Y (single file) | Y (one txn) | U | U | U |
| #738 claim race 0+13 | Y | Y | U | U | U |
| Same-claim retry | Y | Y (adapter) | U | U | U |
| Crash before commit/replace | Y | Y | U | U | U |
| Crash after SQL commit before VC commit | n/a | n/a | U | U | U |
| Offline A/B divergence reconcile | N | N | C | C | C |
| Branch / diff / merge | N | N | C | C | C |
| Same-cell conflict model | file CAS | row/txn locks | cell merge† | cell merge† | cell merge† |
| Row locks / `SELECT FOR UPDATE` | n/a | SQLite locks | N† | N† | N† |
| Migration / downgrade policy | ledger version field | adapter-local | U | U | U |
| Lossless export/import | Y | Y | U | U | U |
| Backup / restore digests | file copy | file copy | C | C | C‡ |
| Credential references | file perms only | file perms only | C | C | C‡ users/grants |
| Retained deleted secrets in history | N (no VC history) | N | C risk | C risk | C risk |
| Anonymous metrics disabled in tests | Y | Y | U | U | U |
| Cold start / p50 / p95 / memory / disk | measured latency | measured latency | U | U | U |
| Install size / backup time | n/a (in-tree) | stdlib | U | U | U |
| Listener | N | N | N | N | blocked in R1 |
| Brigade runtime dependency | already present | **forbidden** | **forbidden** | **forbidden** | **forbidden** |

\* SQLite adapter reuses the Brigade readiness resolver on export for #737
checks; claim CAS is exercised inside `BEGIN IMMEDIATE`.

† Dolt primary docs: row-level locks and `SELECT FOR UPDATE` unsupported;
concurrency uses SQL transactions plus cell-level merge/conflict behavior.
SQL transaction success is not Brigade claim-CAS proof
([supported statements](https://www.dolthub.com/docs/sql-reference/sql-support/supported-statements/),
[concurrency](https://www.dolthub.com/blog/2026-02-17-dolt-concurrency/)).

‡ Server shape adds long-running process, users/grants, backup, and version
lifecycle beyond CLI/embedded
([server deployment](https://www.dolthub.com/docs/introduction/installation/application-server/),
[backups](https://www.dolthub.com/docs/sql-reference/server/backups/),
[versioning](https://www.dolthub.com/docs/other/versioning/)).

### Mapping Dolt commits to Brigade mutations

If a later issue revisits Dolt, candidate commit policy (not implemented here):

| Mutation | SQL transaction | `CALL DOLT_COMMIT` |
|---|---|---|
| Claim CAS / release / reassign | required | candidate yes — durable actor-visible history |
| Atomic graph apply | required | candidate yes |
| Ready-set / status reads | optional / none | no |
| Branch proposal create / update | required | yes |
| Merge of reviewed proposal | merge + commit | yes |

Working-set SQL commits without Dolt commits would leave branch heads and
reviewable history unclear. Claim CAS must still enforce one winner and exit
`13` semantics above any cell-merge success.

## Decision record (R1)

1. **Keep the machine-local JSON ledger** as the Brigade work store.
2. The **current need** is sequential portable sync plus reviewable
   branch-and-merge proposals for one operator — not concurrent cross-machine
   claims.
3. **Do not approve** a shared-store conformance fixture, backend interface,
   daemon, or `brigade.workstore.v1` in this issue.
4. SQLite WAL remains an **optional local characterization** control; it must
   not become a runtime dependency from this work.
5. Dolt CLI / embedded / server remain **characterization candidates only**.
6. An operator-owned shared service becomes eligible **only if** a later
   concurrent-claim requirement cannot meet measured correctness through file
   or replica candidates.
7. Beads stays excluded under #770.

## Explicit non-goals preserved

No backend adoption, daemon, listener, Beads path, source-of-truth migration,
multi-user tenancy, production schema change, or production work-store
refactor landed in R1. Production work-store modules were not edited.

## Follow-ups (out of R1)

- Portable sync design / import-export protocol (separate issue)
- Reviewable branch-and-merge proposal surface (separate issue)
- Optional Dolt CLI runner behind an explicit enable flag, still without a
  runtime dependency
- Shared-store conformance fixture only after a concurrent-claim requirement
  appears and file/replica candidates fail measured gates

## Verification

Baseline and post-change normative anchors:

```bash
brigade work verify run --target . --command \
  ".venv/bin/python -m pytest -q tests/test_work_cmd_edges.py tests/test_work_cmd_claim.py" \
  --capture brigade-work
```

Harness:

```bash
brigade work verify run --target . --command \
  ".venv/bin/python -m pytest -q tests/test_work_store_measurement.py" \
  --capture brigade-work
```

Cite exact Brigade receipt IDs in the draft PR body.
