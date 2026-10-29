---
name: fleet-hub-upgrade
description: Use when upgrading a Brigade Fleet Hub and the node clients that publish to it - Hub-first ordering, pre-flight diff of node-to-Hub payloads and SCHEMA_VERSION, snapshot-and-switch deploy with rollback, and node verification. Never downgrade code against a newer schema or edit the Hub database directly.
allowed-tools: Read, Grep, Bash(git diff:*), Bash(git log:*), Bash(git show:*), Bash(brigade fleet sessions:*)
compatibility: Operators running a self-hosted Brigade Fleet Hub with one or more enrolled node clients. Host-specific deploy commands are the operator's own; this skill is the generic checklist.
---

# fleet-hub-upgrade

Upgrade a Fleet Hub and its nodes to a target Brigade commit without node writes being refused and without stranding the Hub database on a schema the old code cannot open. Brigade does not run the deploy; this skill is the checklist an operator or agent follows on their own hosts.

Placeholders used below: `<hub-host>`, `<node-host>`, `<old-sha>` (deployed Hub commit), `<target-sha>`, `/opt/brigade/releases/<sha>` (per-commit venv), `<hub-db>` (Hub SQLite path), `<hub-unit>` (service name).

## When this applies

- A release adds or changes anything a node sends to the Hub (client request bodies, session presence, telemetry, observers).
- The Hub and node installs are on different commits and you are bringing them level.
- A node reports `session request has unsupported field` or the Hub refuses to start with a schema-version error.

## 1. Order: Hub first, then nodes

The Hub validates node writes strictly. `_validate_session_request` in `src/brigade/fleet_hub_sessions.py` rejects unknown fields with `session request has unsupported field`. If a release adds a field to any node-to-Hub payload, a node upgraded before the Hub gets its writes refused until the Hub catches up.

1. Deploy and verify the Hub on `<target-sha>`.
2. Upgrade each node to the same `<target-sha>`.
3. Restart long-running units on each node that import its Brigade install (telemetry publishers, heartbeat units, daemons). A running process keeps the old code in memory until restarted.

## 2. Pre-flight diff

Compare `<old-sha>` to `<target-sha>` before touching anything (`git diff <old-sha>..<target-sha> -- src/brigade/`).

- **Payload keys.** Look for new keys in client request bodies: `src/brigade/fleet_client.py`, observers, session presence, telemetry publishers. For each, confirm the target Hub's validator accepts it and note whether the old Hub rejects it. A rejected key fixes the ordering in step 1; it is not optional.
- **Schema version.** Check `SCHEMA_VERSION` in `src/brigade/fleet_hub.py`. A bump means older code refuses the newer database (`_refuse_newer_schema`), so rollback needs a database restore plus the code revert, not the code revert alone. Say so in the plan before you start.
- **Disk headroom.** The Hub host needs room for a new venv, a wheel build, and a full database snapshot. Check free space against the database size, with margin.

## 3. Hub deploy shape

1. Build a wheel from a clean, detached checkout of `<target-sha>`. Do not build from a dirty working tree.
2. Install the wheel into a per-commit venv at `/opt/brigade/releases/<target-sha>`.
3. Stop `<hub-unit>`.
4. Snapshot `<hub-db>` with the SQLite backup API (`sqlite3` `.backup` or `Connection.backup`), not `cp` of a live file. Run `PRAGMA integrity_check` on the snapshot and require `ok`. Record when the snapshot was taken.
5. Switch the service to the new venv (unit `ExecStart` or a stable symlink), reload the manager, and start `<hub-unit>`.
6. Poll `/health` until it answers or a bounded deadline passes.
7. On failure: stop the unit, restore the snapshot, revert the venv switch, start, and re-check `/health`. Report the exact failure; do not retry blindly.

Verify the new code is what runs, not just that health is green:

- `systemctl show <hub-unit> -p ExecStart` names the new venv.
- The running process command line (`ps` or `/proc/<pid>/cmdline`) names the new venv.
- `PRAGMA user_version` on `<hub-db>` equals `SCHEMA_VERSION` from `<target-sha>`.

Keep the current venv plus one rollback venv; prune older release directories only after the new Hub has run cleanly.

## 4. Node upgrade

For each `<node-host>`:

1. Upgrade the node's Brigade install to `<target-sha>`.
2. Restart every unit that imports that install.
3. Trigger one telemetry publish through the node's usual publisher, or `brigade fleet policy observe --file <snapshot.json>`. Require exit 0: a refused write fails the command.
4. Confirm the write landed on the telemetry surface: no `unsupported field` errors in the unit journal since the restart, and the node's telemetry age on the Hub's machine views (Overview or Capacity) is newer than the restart. If the release changed session publishing, `brigade fleet sessions` should list the node's open sessions with fresh heartbeat times.

Do not use `brigade fleet status` for this. It lists run events, not telemetry, so an idle node can be missing from it and an unrelated recent run can look fresh after a failed publish.

Do not mark a node done on install success alone. Freshness on the Hub is the evidence.

## 5. Never

- Never run older code against a newer schema. If `user_version` is ahead of the code, upgrade the code; do not edit the version.
- Never replace the Hub database with a snapshot without accounting for writes made since the snapshot was taken. Node writes between snapshot and restore are lost; say how many minutes that is and whether nodes will republish.
- Never edit the Hub database directly (no ad hoc `UPDATE`, no `PRAGMA user_version=` surgery). Use Brigade commands or a restore.
- Never upgrade nodes first when the pre-flight diff found a new payload key.

## Failure paths

- Pre-flight diff cannot be produced (shallow clone, unknown `<old-sha>`): stop and report what is missing. Do not assume no payload change.
- Snapshot integrity check fails: stop. Leave the old venv in place and do not switch.
- `/health` never comes up: restore and revert as in step 3, then report the journal tail.
- `user_version` mismatch after start: treat as a failed deploy, not a cosmetic warning.

## Out of scope

- Provisioning or rotating node credentials and enrollment (follow your operator credential process).
- Choosing a process manager or reverse proxy; the shapes above assume a service with a venv-based `ExecStart`.
- Fleet Hub configuration changes unrelated to the version bump.
