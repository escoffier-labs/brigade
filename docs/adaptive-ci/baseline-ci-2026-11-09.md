# CI timing: ci.yml

- Window (run created_at, UTC, half-open): 2026-11-08T22:00:00Z up to but not including 2026-11-09T12:00:00Z
- Runs: 123 completed
- Label: strict baseline
- Generated: 2026-11-09T16:56:02Z

## merge_group

Conclusions: failure 9, success 20
Runs with a re-run (metrics are first attempts): 0

| runs | n | wall min median | p75 | p90 | max | runner min median | p90 | Windows runner min median | p90 |
|---|---|---|---|---|---|---|---|---|---|
| successful | 20 | 14.9 | 15.2 | 15.4 | 15.8 | 123.6 | 130.3 | 12.2 | 13.5 |
| all | 29 | 14.9 | 15.2 | 15.4 | 15.8 | 126.7 | 133.9 | 12.4 | 13.6 |

Job families in successful runs, top 12 by p90 seconds (over job instances, not per run):

| family | runner | ran in | median s | p90 s | max s |
|---|---|---|---|---|---|
| hub-rust / hub (Rust 1.85.0) | ubuntu-latest | 20/20 | 859.5 | 892.1 | 907 |
| hub-rust / hub (Rust stable) | ubuntu-latest | 20/20 | 734.0 | 838.2 | 860 |
| test shard 3.12 | ubuntu-latest | 20/20 | 500.5 | 581.9 | 640 |
| test shard 3.11 | ubuntu-latest | 20/20 | 412.0 | 486.1 | 527 |
| windows-native-acceptance | windows-latest | 20/20 | 279.5 | 331.4 | 337 |
| Windows (stable, default features) | windows-latest | 20/20 | 202.0 | 223.8 | 236 |
| Fleet Hub container (linux/arm64) | ubuntu-latest | 20/20 | 156.5 | 184.0 | 185 |
| engine-rename-installer | ubuntu-latest | 20/20 | 94.0 | 112.2 | 129 |
| build-and-test | ubuntu-latest | 20/20 | 89.5 | 100.2 | 109 |
| evidence-ledger-test | ubuntu-latest | 20/20 | 89.0 | 98.1 | 99 |
| mesh core without extra | ubuntu-latest | 20/20 | 79.0 | 85.2 | 89 |
| content-guard | ubuntu-latest | 20/20 | 68.5 | 81.1 | 82 |

## pull_request

Conclusions: cancelled 43, failure 12, success 19
Runs with a re-run (metrics are first attempts): 5

| runs | n | wall min median | p75 | p90 | max | runner min median | p90 | Windows runner min median | p90 |
|---|---|---|---|---|---|---|---|---|---|
| successful | 19 | 14.7 | 17.2 | 26.6 | 33.9 | 75.7 | 83.8 | 9.8 | 31.9 |
| all | 74 | 10.4 | 14.6 | 18.0 | 33.9 | 55.1 | 78.9 | 10.8 | 22.3 |

Job families in successful runs, top 12 by p90 seconds (over job instances, not per run):

| family | runner | ran in | median s | p90 s | max s |
|---|---|---|---|---|---|
| windows-affected-tests | windows-latest | 19/19 | 143.0 | 1566.8 | 1986 |
| hub-rust / hub (Rust 1.85.0) | ubuntu-latest | 9/19 | 856.0 | 894.4 | 896 |
| hub-rust / hub (Rust stable) | ubuntu-latest | 9/19 | 761.0 | 836.2 | 845 |
| test shard 3.12 | ubuntu-latest | 19/19 | 472.5 | 580.5 | 617 |
| windows-native-acceptance | windows-latest | 19/19 | 307.0 | 335.8 | 355 |
| engine-rename-installer | ubuntu-latest | 19/19 | 95.0 | 107.4 | 138 |
| content-guard | ubuntu-latest | 19/19 | 68.0 | 81.0 | 82 |
| mesh core without extra | ubuntu-latest | 3/19 | 59.0 | 79.0 | 84 |
| hub-rust / hub (linux-arm64) | ubuntu-24.04-arm | 9/19 | 62.0 | 67.2 | 68 |
| mesh (windows-latest, floor) | windows-latest | 3/19 | 63.0 | 65.4 | 66 |
| mesh (ubuntu-latest, newest) | ubuntu-latest | 3/19 | 61.0 | 63.4 | 64 |
| mesh (windows-latest, newest) | windows-latest | 3/19 | 50.0 | 62.0 | 65 |

## push

Conclusions: cancelled 20
Runs with a re-run (metrics are first attempts): 0

| runs | n | wall min median | p75 | p90 | max | runner min median | p90 | Windows runner min median | p90 |
|---|---|---|---|---|---|---|---|---|---|
| successful | 0 | - | - | - | - | - | - | - | - |
| all | 20 | 11.0 | 32.3 | 134.3 | 509.5 | 79.5 | 110.8 | 7.7 | 8.9 |

Job families in successful runs, top 0 by p90 seconds (over job instances, not per run):

| family | runner | ran in | median s | p90 s | max s |
|---|---|---|---|---|---|
