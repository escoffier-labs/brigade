# CI timing: ci.yml

- Window (run created_at, UTC, half-open): 2026-11-05T15:00:00Z up to but not including 2026-11-09T12:00:00Z
- Runs: 495 completed
- Label: extended window
- Generated: 2026-11-09T16:40:41Z

## merge_group

Conclusions: failure 18, success 70
Runs with a re-run (metrics are first attempts): 0

| runs | n | wall min median | p75 | p90 | max | runner min median | p90 | Windows runner min median | p90 |
|---|---|---|---|---|---|---|---|---|---|
| successful | 70 | 11.5 | 13.4 | 15.1 | 15.8 | 120.5 | 125.3 | 12.6 | 13.9 |
| all | 88 | 11.6 | 13.9 | 15.1 | 15.8 | 120.5 | 129.1 | 12.6 | 13.8 |

Job families in successful runs, top 12 by p90 seconds (over job instances, not per run):

| family | runner | ran in | median s | p90 s | max s |
|---|---|---|---|---|---|
| hub-rust / hub (Rust 1.85.0) | ubuntu-latest | 70/70 | 659.0 | 886.1 | 907 |
| hub-rust / hub (Rust stable) | ubuntu-latest | 70/70 | 628.0 | 775.8 | 860 |
| test shard 3.12 | ubuntu-latest | 70/70 | 505.5 | 589.1 | 640 |
| test shard 3.11 | ubuntu-latest | 70/70 | 408.5 | 483.1 | 527 |
| windows-native-acceptance | windows-latest | 70/70 | 313.5 | 337.2 | 411 |
| Windows (stable, default features) | windows-latest | 70/70 | 199.5 | 236.0 | 290 |
| Fleet Hub container (linux/arm64) | ubuntu-latest | 70/70 | 171.0 | 186.0 | 193 |
| build-and-test | ubuntu-latest | 70/70 | 91.5 | 113.3 | 190 |
| engine-rename-installer | ubuntu-latest | 70/70 | 96.5 | 110.1 | 129 |
| evidence-ledger-test | ubuntu-latest | 70/70 | 89.0 | 99.0 | 104 |
| mesh core without extra | ubuntu-latest | 70/70 | 72.0 | 83.1 | 89 |
| notify-build-and-test (windows-latest) | windows-latest | 70/70 | 72.5 | 81.0 | 129 |

## pull_request

Conclusions: cancelled 190, failure 40, success 110
Runs with a re-run (metrics are first attempts): 12

| runs | n | wall min median | p75 | p90 | max | runner min median | p90 | Windows runner min median | p90 |
|---|---|---|---|---|---|---|---|---|---|
| successful | 110 | 11.7 | 14.3 | 19.3 | 71.7 | 70.6 | 87.4 | 9.2 | 24.8 |
| all | 340 | 11.1 | 13.4 | 19.1 | 71.7 | 61.7 | 86.2 | 9.2 | 24.0 |

Job families in successful runs, top 12 by p90 seconds (over job instances, not per run):

| family | runner | ran in | median s | p90 s | max s |
|---|---|---|---|---|---|
| windows-affected-tests | windows-latest | 109/110 | 143.0 | 1124.0 | 1986 |
| hub-rust / hub (Rust 1.85.0) | ubuntu-latest | 80/110 | 636.5 | 761.0 | 896 |
| hub-rust / hub (Rust stable) | ubuntu-latest | 80/110 | 612.0 | 707.2 | 845 |
| test shard 3.12 | ubuntu-latest | 109/110 | 479.5 | 583.5 | 619 |
| windows-native-acceptance | windows-latest | 109/110 | 316.0 | 351.6 | 457 |
| Windows (stable, default features) | windows-latest | 4/110 | 227.5 | 299.0 | 329 |
| Fleet Hub container (linux/arm64) | ubuntu-latest | 6/110 | 156.0 | 171.0 | 180 |
| engine-rename-installer | ubuntu-latest | 109/110 | 93.0 | 106.2 | 139 |
| build-and-test | ubuntu-latest | 4/110 | 92.5 | 100.3 | 103 |
| evidence-ledger-test | ubuntu-latest | 5/110 | 89.0 | 96.2 | 97 |
| mesh core without extra | ubuntu-latest | 10/110 | 68.5 | 82.2 | 84 |
| content-guard | ubuntu-latest | 110/110 | 70.0 | 79.0 | 82 |

## push

Conclusions: cancelled 67
Runs with a re-run (metrics are first attempts): 0

| runs | n | wall min median | p75 | p90 | max | runner min median | p90 | Windows runner min median | p90 |
|---|---|---|---|---|---|---|---|---|---|
| successful | 0 | - | - | - | - | - | - | - | - |
| all | 67 | 35.4 | 88.9 | 254.7 | 638.5 | 126.9 | 358.0 | 8.0 | 9.3 |

Job families in successful runs, top 0 by p90 seconds (over job instances, not per run):

| family | runner | ran in | median s | p90 s | max s |
|---|---|---|---|---|---|
