# Brigade Scanner Registry

`brigade work scanners` describes local scanner producers and plans safe run windows. It does not execute scanners, install cron jobs, start a daemon, mutate remotes, or promote imports.

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
```

`plan` calculates intended run windows from scanner cadence and timeout, detects overlapping or clustered scanner windows, and prints a suggested staggered schedule. `doctor` checks missing config, disabled required local producers, bad commands, missing or stale output paths, and schedule conflicts. With `--import-issues`, doctor writes scanner health warnings into the existing work import inbox as task imports.

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
- `conflict_window`: `HH:MM-HH:MM` window that should not overlap related jobs.

Default local producers cover chat sweep imports, memory refresh imports, handoff ingest sync, security findings, and optional disabled backup-health and tool-catalog entries. Product-specific chat adapters and tool projection writers remain outside this registry phase.
