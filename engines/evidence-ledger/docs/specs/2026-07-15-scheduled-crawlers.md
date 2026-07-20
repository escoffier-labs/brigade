# Scheduled crawlers

## Goal

MiseLedger should run crawler ingestions on a repeatable local schedule, using the same crawl commands users already trust manually.

## Design

Add a `miseledger schedule` command with two modes:

- `miseledger schedule run <config> [--json]` runs each enabled job once and returns a per-job summary.
- `miseledger schedule daemon <config> [--interval DURATION] [--max-runs N] [--json]` repeats `schedule run` on an interval until interrupted or until `--max-runs` is reached.

The config file is a small TOML subset:

```toml
interval = "15m"

[[jobs]]
name = "discord"
command = "crawl"
args = ["discord", "--limit", "100", "--json"]

[[jobs]]
name = "sessions"
command = "crawl"
args = ["sessions", "--json"]
```

`command` is the top-level MiseLedger command and `args` are the remaining CLI args. Jobs run in order. A failed job is recorded in the summary and does not prevent later jobs from running. The overall command exits non-zero if any enabled job failed.

## Boundaries

- MiseLedger does not install systemd timers or cron entries in this change.
- Jobs execute in-process via the existing command dispatcher rather than shelling out, so schedule configs cannot inject shell syntax.
- The daemon owns only timing and signal handling. Each job still uses the existing crawler/import implementation and dedupe behavior.

## Verification

- Unit tests cover config parsing, one-shot schedule execution, continuing after failures, and daemon `--max-runs`.
- Full repo verification still requires `go vet ./...` and `go test ./...`.
