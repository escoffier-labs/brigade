# Brigade Backup Health

`brigade work backup` reads local backup summary files and routes backup risk into the daily work loop. It is read-only: Brigade does not run `restic`, mount storage, prune, restore, notify chat, or mutate remote backup state.

The local config is gitignored:

```text
.brigade/backups.toml
```

Create it with:

```bash
brigade work backup init
```

## Commands

```bash
brigade work backup status
brigade work backup status --json
brigade work backup doctor
brigade work backup doctor --json
brigade work backup import-issues
brigade work backup import-issues --json
```

`status` summarizes configured destinations. `doctor` checks local backup summary files for stale snapshots, failed or stale checks, failed or stale prunes, missing summaries, overdue or failed restore rehearsals, and unsafe private fields. `import-issues` writes those risks into the local work inbox with source `backup-health`.

## Config Shape

Each destination is a TOML table:

```toml
[[destination]]
id = "nas"
kind = "nas"
command_label = "local backup summary producer"
summary_path = ".brigade/backups/nas-summary.json"
snapshot_stale_hours = 36
check_stale_hours = 168
prune_stale_hours = 168
restore_rehearsal_stale_days = 90
enabled = true
```

Fields:

- `id`: stable destination id such as `nas` or `cloud`.
- `kind`: destination family label.
- `command_label`: safe label for the external producer. Do not include secrets or real remote paths.
- `summary_path`: local JSON summary path.
- `snapshot_stale_hours`: warn when the latest snapshot is older than this threshold.
- `check_stale_hours`: warn when the latest check is older than this threshold.
- `prune_stale_hours`: warn when the latest prune is older than this threshold.
- `restore_rehearsal_stale_days`: warn when the latest restore rehearsal is older than this threshold.
- `enabled`: true or false.

## Summary JSON

Minimal safe summary:

```json
{
  "destination_label": "NAS backup",
  "latest_snapshot_at": "2026-05-30T06:00:00+00:00",
  "latest_check_at": "2026-05-29T12:00:00+00:00",
  "latest_check_result": "ok",
  "latest_prune_at": "2026-05-29T12:30:00+00:00",
  "latest_prune_result": "ok",
  "latest_restore_rehearsal_at": "2026-05-01T12:00:00+00:00",
  "latest_restore_rehearsal_result": "ok",
  "summary": "NAS backup is current.",
  "evidence_path": ".brigade/backups/nas-evidence.json"
}
```

Accepted successful results include `ok`, `success`, `passed`, and `pass`.

## Privacy Boundary

Do not put real hostnames, remote names, mount paths, repository paths, webhook URLs, channel ids, tokens, passwords, or backup secrets into public templates or backup summary JSON. Brigade warns on unsafe field names and reports only the field names, not their values.
