# Brigade Scanner Registry

`brigade work scanners` describes local scanner producers, plans safe run windows, and explicitly runs configured local producers when asked. `brigade work sweep` is the daily review action that runs configured scanner producers and writes one local report. These commands do not install cron jobs, start a daemon, mutate remotes, run scanners from `brief` or `doctor`, or promote imports automatically.

The local config is gitignored:

```text
.brigade/scanners.toml
```

Create it with:

```bash
brigade work scanners init
```

## Commands

```bash
brigade work scanners list
brigade work scanners list --json
brigade work scanners show chat-memory-sweep
brigade work scanners plan
brigade work scanners plan --json
brigade work scanners doctor
brigade work scanners doctor --import-issues
brigade work scanners run chat-memory-sweep
brigade work scanners run chat-memory-sweep --ingest-output
brigade work scanners run --all
brigade work scanners run --due
brigade work scanners runs
brigade work scanners run-show <run-id>
brigade work sweep
brigade work sweep --all
brigade work sweep --scanner security-scan
brigade work sweep --no-ingest
brigade work sweeps
brigade work sweep-show <sweep-id>
brigade work sweep-review latest
brigade work sweep-review <sweep-id>
```

`plan` calculates intended run windows from scanner cadence and timeout, detects overlapping or clustered scanner windows, and prints a suggested staggered schedule. `doctor` checks missing config, disabled required local producers, bad commands, missing or stale output paths, and schedule conflicts. With `--import-issues`, doctor writes scanner health warnings into the existing work import inbox as task imports.

`run` executes only configured scanner entries from `.brigade/scanners.toml`. It supports one scanner id, `--all`, or `--due`. Due runs compare each scanner cadence with the latest successful receipt. Disabled scanners are skipped unless `--include-disabled` is present. Existing running receipts block execution unless `--force` is present.

Execution is direct and foreground-only. Brigade splits command strings into argv, rejects high-risk shell-like commands, refuses shell metacharacters, and calls the process without a shell. Scanner commands may write their own import records, and Brigade stamps matching new imports with scanner run provenance when possible. It does not promote anything.

If a scanner declares `import_path` and `import_format = "jsonl"`, `brigade work scanners run ... --ingest-output` validates that JSONL output after a successful run and appends valid records to the work inbox with scanner provenance. Without `--ingest-output`, Brigade records the run receipt and leaves output ingestion explicit. Malformed configured import output fails before Brigade appends any records.

Scanner run receipts are gitignored under:

```text
.brigade/scanners/runs/
```

Each receipt includes the run id, scanner id, source, argv, cwd, started and completed timestamps, duration, exit code, timeout state, stdout/stderr summaries, full local log paths, and output path snapshots from before and after execution. Use `brigade work scanners runs` and `brigade work scanners run-show <run-id>` to review them. `doctor` also reports failed or timed-out runs, malformed receipts, missing logs, stale successful runs, and due scanners.

## Daily Sweeps

`brigade work sweep` runs due enabled scanners from `.brigade/scanners.toml` as one explicit foreground operator action. It uses the same scanner execution path as `brigade work scanners run --due`, but defaults to ingesting each successful scanner's configured `import_path` when `import_format = "jsonl"`. Use `--no-ingest` to leave outputs untouched, `--all` to run every configured scanner, or `--scanner <id>` to run one scanner. Disabled scanners still require `--include-disabled`, and overlapping running receipts still require `--force`.

Sweep reports are gitignored under:

```text
.brigade/scanners/sweeps/
```

Each report includes the sweep id, started and completed timestamps, scanner run ids, scanner receipt paths, created import ids, skipped source fingerprints, dismissed source fingerprints, import counts, an inbox hygiene summary, and suggested next commands. Use `brigade work sweeps` and `brigade work sweep-show <sweep-id>` to inspect reports.

Use `brigade work sweep-review <sweep-id>` or `brigade work sweep-review latest` to triage what a sweep produced. Review output groups created imports by source, kind, priority, acceptance coverage, provenance completeness, and pending/promoted/dismissed state. Pending imports include exact next commands for `import plan`, `import promote`, `import dismiss`, `import promote --run`, `import plan-handoff`, and `import promote-handoff` as appropriate.

`brigade work brief` shows the latest sweep, suggests `brigade work sweep` when scanner runs are due, and surfaces the top pending import from the latest unreviewed sweep. `brigade work doctor` warns on missing, stale, failed, or unreviewed sweep reports, missing import references, lost provenance, and noisy no-op sweeps. `brigade work inbox doctor` also reports broken sweep import references.

Sweeps may ingest reviewed JSONL output into the work inbox, but they never promote imports, edit memory, mutate GitHub, or run in the background.

Inbox hygiene commands help keep reviewed scanner output from becoming queue clutter:

```bash
brigade work inbox doctor
brigade work inbox archive
```

`inbox doctor` reports pending scanner imports missing provenance, stale pending imports, promoted imports whose ledger task is missing, dismissed imports whose source fingerprint changed, noisy sources, and scanner runs that produced no imports despite a configured `import_path`. `inbox archive` moves old promoted, dismissed, and superseded imports to `.brigade/work/imports/archive.jsonl` while preserving pending imports.

## Config Shape

Each scanner is a TOML table:

```toml
[[scanner]]
id = "chat-memory-sweep"
source = "chat-memory-sweep"
command = "brigade work import chat-sweep --json"
cadence = "daily@02:15"
enabled = true
timeout = 300
output_path = ".brigade/chat-memory-sweeps/latest.json"
import_path = ".brigade/work/imports/chat-memory-sweep.jsonl"
import_format = "jsonl"
conflict_window = "02:00-02:30"
```

Fields:

- `id`: stable scanner id.
- `command`: command the operator or wrapper may run explicitly.
- `source`: expected work import source.
- `cadence`: `daily@HH:MM` or `hourly@MM`.
- `enabled`: true or false.
- `timeout`: expected max runtime in seconds.
- `output_path`: local output or state file used for freshness checks.
- `import_path`: optional repo-relative JSONL import output for explicit `--ingest-output`.
- `import_format`: optional import format. Only `jsonl` is supported.
- `conflict_window`: `HH:MM-HH:MM` window that should not overlap related jobs.
- `cwd` or `target`: optional repo-relative working directory for execution.

Default local producers cover chat sweep imports, memory refresh imports, handoff ingest sync, security findings, and optional disabled memory-care, backup-health, and tool-catalog entries. Product-specific chat adapters and tool projection writers remain outside this registry phase.
