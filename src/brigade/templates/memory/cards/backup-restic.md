---
topic: backup-restic
category: infrastructure
tags: [backup, restic, rclone, gdrive, nas, retention, recovery]
---

# Workspace Backup (Restic + rclone + NAS)

Twice-daily restic backups to two destinations: Google Drive (via rclone) and a local NAS mount. Encrypted, deduplicated, snapshot-pruned. The reference script ships at `scripts/backup-restic.sh`; this card explains *why* the shape is what it is.

## Why both destinations

| Failure mode | Local NAS only | gdrive only | Both |
|--------------|----------------|-------------|------|
| Workstation disk dies | Recover from NAS, fast | Recover from gdrive, slow | Either |
| NAS hardware failure | Lose everything | Recover from gdrive | gdrive saves you |
| Google account locked / quota | Lose everything that ran since last NAS run | Lose everything | NAS saves you |
| Ransomware hits workstation | Possibly hits NAS too | Off-site immutable copy | gdrive saves you |

Two destinations covers the "one of them is broken" case without raising the recovery time for the common case (NAS is faster).

## Cadence

```cron
0 3,15 * * * /path/to/backup-restic.sh
```

03:00 + 15:00. Twice a day means worst-case data loss window is ~12 hours. Adjust if your write velocity is higher.

## What gets backed up

Default paths:

- The agent workspace (`~/.brigade` or your equivalent)
- All repos under `~/repos`
- Local scripts and bin (`~/bin`)
- Dotfiles: `.bashrc`, `.profile`, `.gitconfig`, `.ssh`, `.claude`, `.codex`, `.npmrc`
- Notes (`~/notes`)
- Obsidian vault (`~/Obsidian`)

Excluded by default: `node_modules`, `.git/objects`, `__pycache__`, `*.pyc`, `.venv`, `dist`, `build`, `.next`, `.astro`, `coverage`, `.turbo`, `*.jsonl`, `.pm2/logs`, `.pm2/pids`, `.ollama`, `.obsidian/workspace*.json`, `.obsidian/cache`, `.trash`.

Edit the `BACKUP_PATHS` and `EXCLUDES` arrays in the script for your stack.

## Retention

```text
--keep-daily 7
--keep-weekly 4
--keep-monthly 3
```

About 14 snapshots overlapping over three months. Plenty for human-paced workflows.

## Why rclone is throttled hard

Google Drive can reject bursty restic-over-rclone writes when other rclone jobs (Obsidian bisync, cookbook sync, etc.) are running. The script sets:

```bash
RCLONE_TRANSFERS=1
RCLONE_CHECKERS=2
RCLONE_TPSLIMIT=4
RCLONE_TPSLIMIT_BURST=4
RCLONE_DRIVE_PACER_MIN_SLEEP=500ms
RCLONE_DRIVE_PACER_BURST=10
RCLONE_RETRIES=8
RCLONE_LOW_LEVEL_RETRIES=20
```

Conservative on purpose. A backup that takes 40 minutes and finishes beats one that races, hits Drive quota, and dies in a retry loop.

## NAS shape

The NAS mount is typically an SMB/NFS share at `/mnt/nas/backups`. The script:

1. Skips cleanly if the mount is not present.
2. Uses a separate restic repo path on the NAS (independent encryption + deduplication state).
3. Tags NAS snapshots distinctly (`scheduled-nas`) so summaries are easy to read.

NAS-side considerations:

- **Permissions:** the NAS share must allow write from the workstation user.
- **Lock files:** restic uses lockfiles inside the repo. A killed process can leave stale locks; the script runs `restic unlock --remove-all` after each successful backup.
- **Read-only by default:** if the NAS holds irreplaceable family photos or other "do not touch" data, keep that data on a separate path and treat the rest of the NAS as read-only for the agent. See `SAFETY_RULES.md`.

## Password

```bash
echo "<strong-password>" > ~/.brigade/.restic-password
chmod 600 ~/.brigade/.restic-password
```

Never commit this file. Never paste the password in a chat. If the password is lost, the encrypted snapshots are unrecoverable.

## Recovery

```bash
# list snapshots
restic snapshots

# restore a specific snapshot to /tmp/restore/
restic restore <id> --target /tmp/restore

# restore just one path
restic restore <id> --target /tmp/restore --include "$HOME/repos/<project>"
```

Practice this. A backup you have never restored is a hope, not a backup.

## Monitoring

Log file path:

```text
~/.brigade/logs/backup-YYYYMMDD.log
```

Worth wiring into the morning report (`memory/cards/pipeline-standups.md`): grep recent logs for `ERROR` and surface in the briefing. A silent backup that has been failing for three weeks is the second-worst kind of bug.

If chat is part of your operations loop, send backup summaries to the same private status surface you already use for agent reports. Keep the status compact:

- latest NAS snapshot age
- latest cloud snapshot age
- most recent `restic check` result
- prune result
- restore rehearsal date

Route stale or failed backup checks into the Brigade work inbox as incidents. Do not publish real repo names, hostnames, webhook URLs, cloud remote names, or channel ids in public docs.

## Anti-patterns

- **One destination only.** Single point of failure.
- **No retention policy.** Old snapshots accumulate, gdrive quota fills, new backups fail.
- **Backing up `node_modules`.** Wastes deduplication windows. Use the excludes.
- **Backing up secrets unencrypted.** `.env` files get backed up too; that is intentional because restic encrypts everything at rest. The password file is the keystone; protect it.
- **Skipping verification.** Run `restic check` periodically. Snapshots that exist but are corrupted are not snapshots.
