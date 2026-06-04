# Brigade Backup Health

`brigade work backup` reads local backup summary files and routes backup risk into the daily work loop. It is read-only: Brigade does not run `restic`, mount storage, prune, restore, notify chat, or mutate remote backup state.

It monitors snapshot-history backups - the kind that have a latest snapshot, a check, a prune, and a restore rehearsal (restic, borg, and similar). It is not designed to monitor a bidirectional last-writer-wins file sync (for example a KeePass database mirrored between a NAS and cloud on a short timer). Those have no snapshot or prune lifecycle to track. If you want such a sync watched, emit your own summary JSON with a `latest_snapshot_at` standing in for the last successful sync, but treat it as a coarse freshness check, not full backup health.

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
brigade work backup contract
brigade work backup contract --destination nas --json
brigade work backup doctor
brigade work backup doctor --json
brigade work backup import-issues
brigade work backup import-issues --json
brigade work backup closeout
brigade work backup closeout --json
```

`status` summarizes configured destinations. It reports active issue count, raw issue count, quieted reviewed or deferred issue count, restore rehearsal issue count, and a compact safe operator summary. `contract` prints the JSON producer contract for all configured destinations, or one destination with `--destination`. `doctor` checks local backup summary files for stale snapshots, failed or stale checks, failed or stale prunes, missing summaries, overdue or failed restore rehearsals, and unsafe private fields. `import-issues` writes active risks into the local work inbox with source `backup-health`. `closeout` writes a local review receipt keyed by backup issue fingerprints, so unchanged reviewed risks stop making the daily brief noisy while changed fingerprints resurface.

## Config Shape

Each destination is a TOML table. A common topology is a frequently written
local NAS plus a slower off-site cloud copy, so the staleness thresholds differ
per destination:

```toml
# Local NAS, backed up twice daily.
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

# Off-site cloud, backed up weekly. Thresholds widened so a once-a-week repo
# does not report stale every day. `brigade work backup init` writes these
# wider defaults for the `cloud` destination automatically.
[[destination]]
id = "cloud"
kind = "cloud"
command_label = "cloud backup summary producer"
summary_path = ".brigade/backups/cloud-summary.json"
snapshot_stale_hours = 192
check_stale_hours = 336
prune_stale_hours = 336
restore_rehearsal_stale_days = 90
enabled = true
```

Match the thresholds to each destination's real cadence. A twice-daily NAS
should warn within a day or so of a missed snapshot, while a weekly cloud copy
needs more than seven days of slack before stale is meaningful. Setting the
cloud threshold as tight as the NAS produces a false stale alarm on every day
the weekly backup did not run.

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

External jobs write one local JSON object per destination. Use the contract command when wiring a producer:

```bash
brigade work backup contract --destination nas --json
```

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

Producer rules:

- Write to the destination's configured `summary_path` after each backup run or scheduled health check.
- Use ISO-8601 timestamps with timezone offsets for all timestamp fields.
- Keep `destination_label`, `summary`, `evidence_path`, and `command_label` safe for logs and public docs.
- Store raw backup logs elsewhere. The summary should point to safe evidence, not copy private command output.
- Do not include hostnames, mount paths, repository paths, webhook URLs, channel ids, tokens, passwords, or backup secrets.

## Closeout And Release Evidence

Backup closeout receipts live under:

```text
.brigade/backups/closeouts/
```

The receipt stores only safe counts, the closeout reason, status, source fingerprints, and restore rehearsal issue count. It does not copy destination hostnames, remotes, mount paths, repository paths, webhooks, passwords, or raw evidence values.

Release readiness and release candidate evidence include backup health counts, latest closeout metadata, changed fingerprint count, restore rehearsal issue count, and the safe operator summary. Reviewed backup risks can be quieted in the daily loop, but raw counts and restore rehearsal evidence remain visible for release review.

## Privacy Boundary

Do not put real hostnames, remote names, mount paths, repository paths, webhook URLs, channel ids, tokens, passwords, or backup secrets into public templates or backup summary JSON. Brigade warns on unsafe field names and reports only the field names, not their values.
