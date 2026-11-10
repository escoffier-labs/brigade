# Rust hub cutover and rollback

This runbook moves an existing Fleet Hub from the Python engine to the Rust
hub, `brigade-hub`, and back (S10 in
[the v2 hub design](../phase-v2-hub-architecture.md#cutover)). It is written
for the production hub on the LXC hub host
([Fleet hub on a Proxmox LXC](fleet-hub-proxmox.md)). The dogfood Compose
recipe runs the Rust engine once its pinned Brigade has it
([Dogfood hub from the Compose recipe](fleet-hub-dogfood.md)).

Both engines serve the same `fleet-hub.db`. The Python hub stays the schema
owner and runs on as the Rust hub's managed loopback upstream, so a rollback
is a restart without `--engine rust`, on the same file.

## What `--engine rust` does

`brigade fleet serve --engine rust` keeps every flag `fleet serve` has. It:

1. migrates the database with the Python schema code, and refuses a file
   with a newer `user_version` before anything binds.
2. checks the `brigade-hub` binary's SHA-256 against the checksum recorded
   when it was installed, and refuses to start on a mismatch.
3. runs `brigade-hub`, which binds the port and starts
   `brigade fleet serve` (Python) as its child on an ephemeral loopback port,
   keyed by a value generated at each start.
4. forwards SIGTERM and SIGINT, logs `brigade fleet hub listening on` once
   `/health` answers and `brigade fleet hub stopped cleanly` after a clean
   stop.
5. watches `/health` every 10 s and exits 1 after 3 failures in a row, so the
   service manager restarts the unit.

If `brigade-hub` exits for any reason, `fleet serve` exits with its status.

Shutdown: `brigade-hub` stops accepting, answers native long polls with their
current page, drains for at most 25 s, then sends the Python hub SIGTERM and
waits up to 10 s. Give the unit or container a stop timeout above 35 s.

Upstream crashes: with at least one family native, `brigade-hub` restarts a
Python hub that died after 1 s, doubling to 30 s, and back to 1 s after a run
of 60 s. Native routes keep serving. Proxied routes answer
`502 {"error": "upstream unavailable", "reason": "upstream-down"}`, `/ready`
answers 503, and webhook delivery pauses with its outbox rows kept. With
every family proxied, `brigade-hub` exits instead, as before S10, and the
service manager restarts both.

## Settings

| Setting | Where | Meaning |
|---|---|---|
| `--engine rust` | `ExecStart` | Run `brigade-hub` in front. Without it, the Python hub serves alone. |
| `--route-mode FAMILY=MODE` | `ExecStart`, repeatable | `brigade-hub` route modes. Families `registration`, `heartbeat`, `claims`, `messages` and `memory` accept `native` or `proxy`. `cold` and `browser` accept `proxy` only until their slices land. Every family defaults to `proxy`. |
| `--hub-binary PATH` with `--hub-sha256 HEX` or `--hub-sha256-file PATH` | `ExecStart` | Run this binary instead of the one `brigade setup --component brigade-hub` installed. The checksum file may be `sha256sum` output. |
| `--port 0` | `ExecStart` | Any free port. The watchdog and the listening line use the address `brigade-hub` reports binding. |
| `XDG_DATA_HOME` | unit `Environment=` and the setup command | Where `brigade setup` records the installed binary. Use the same value for both. |
| `BRIGADE_HUB_PYTHON` | unit `Environment=` | The managed Python hub's interpreter. Defaults to the interpreter running `fleet serve`, so both engines come from one install. |
| `BRIGADE_HUB_WATCHDOG_INTERVAL`, `BRIGADE_HUB_WATCHDOG_FAILURES`, `BRIGADE_HUB_WATCHDOG_STARTUP` | unit `Environment=` | Seconds between `/health` checks (10), failures before a restart (3), and seconds allowed for the first answer (120). |
| `BRIGADE_HUB_WATCHDOG_NETNS=1` | container only | Also exit after 3 checks that find only loopback in the network namespace. |

`brigade-hub` ships for `linux-amd64` and `linux-arm64` only. The binary
must be a regular file, not a symlink, owned by root or the service user,
and writable by its owner alone. `fleet serve` opens it once, checks that
descriptor's SHA-256, and runs the same descriptor, so replacing the file
after the check has no effect on what runs.

## Install the binary

On a host that runs a tagged release, install it from the release manifest.
The production unit runs as root with `ProtectHome=true`, so keep the record
and the binary outside `/root`:

```bash
install -d -o root -g root -m 0755 /opt/brigade-components
XDG_DATA_HOME=/opt/brigade-components XDG_CACHE_HOME=/var/cache/brigade \
  brigade setup --component brigade-hub
```

A later plain `brigade setup`, or `brigade update`, with the same
`XDG_DATA_HOME` keeps `brigade-hub` installed and upgrades it with the rest.
`fleet serve --engine rust` refuses a `brigade-hub` recorded for another
Brigade version, so upgrade both together.

Before a release ships the binary, build it from the same commit as the
installed Brigade and name it explicitly:

```bash
# On a build host, at <BRIGADE_GIT_REF>:
cd engines/hub && cargo build --locked --release --bin brigade-hub
sha256sum target/release/brigade-hub > brigade-hub.sha256
# On the hub host:
install -d -m 0755 /opt/brigade-hub/<BRIGADE_GIT_REF>
install -m 0755 brigade-hub /opt/brigade-hub/<BRIGADE_GIT_REF>/brigade-hub
install -m 0644 brigade-hub.sha256 /opt/brigade-hub/<BRIGADE_GIT_REF>/brigade-hub.sha256
```

## Cutover, phase by phase

Each phase and its gate are in the design's
[Cutover](../phase-v2-hub-architecture.md#cutover) table. Take an online
backup (`brigade fleet backup`) before each step.

Phase 1, the front: every family proxied. Add the engine and the stop
timeout to the unit with a drop-in:

```bash
systemctl edit brigade-fleet.service
```

```ini
[Service]
Environment=XDG_DATA_HOME=/opt/brigade-components
TimeoutStopSec=45
ExecStart=
ExecStart=/usr/local/bin/brigade fleet serve --engine rust --host 127.0.0.1 --port 3774 --db /var/lib/brigade/fleet-hub.db --trust-forwarded-proto
```

For a binary built before the release, add
`--hub-binary /opt/brigade-hub/<BRIGADE_GIT_REF>/brigade-hub --hub-sha256-file /opt/brigade-hub/<BRIGADE_GIT_REF>/brigade-hub.sha256`
and leave out `XDG_DATA_HOME`. Then:

```bash
systemctl daemon-reload
systemctl restart brigade-fleet.service
journalctl -u brigade-fleet.service -n 50 --no-pager
curl --fail --silent --show-error http://127.0.0.1:3774/health
curl --fail --silent --show-error http://127.0.0.1:3774/ready
```

The log shows `brigade fleet hub listening on 127.0.0.1:3774 (brigade-hub front, ...)`.
Run the S29 read-only probe from the hub host against the loopback port, as
the gate requires.

Phase 3, one family at a time: add `--route-mode heartbeat=native` (S6, step 2a), then `claims=native` (S7, step 2b), `messages=native` and `registration=native`, each
after the previous family passed its gate. The 0.31.0 release gate requires phase 3 for at least the heartbeat and claims
families, which are steps 2a and 2b. Phase 4 adds
`--route-mode memory=native`. Each change is the same drop-in edit, then
`systemctl daemon-reload` and a restart.

## Rollback

One family: change its `--route-mode` back to `proxy` (or remove it), then
`systemctl daemon-reload` and restart. Seconds.

The whole Rust hub, on the same files:

```bash
systemctl revert brigade-fleet.service   # or remove --engine rust and every --route-mode from the drop-in
systemctl daemon-reload
systemctl restart brigade-fleet.service
curl --fail --silent --show-error http://127.0.0.1:3774/health
```

The Python hub opens the file the Rust hub left, live `-wal` and `-shm`
files included. During the rollback the Python hub answers 404 on
`/memory`. Clients with a replica watermark keep spooling memory writes and
flush when the Rust hub returns.

Rollback to a pinned backup, for a planned move between hosts or when the
running file must not be reused:

1. Fence: `systemctl stop brigade-fleet.service`. Nothing writes from here.
2. Take the final online backup:
   `brigade fleet backup --db /var/lib/brigade/fleet-hub.db --dest <BACKUP_DIR>`.
3. Start the pinned Python hub (no `--engine rust`) on that copy, check
   `/health` and the S29 probe on its loopback port, then move the name.

Never start a rollback hub on a scheduled backup: it misses every write
since it was taken. Losing the host is disaster recovery through
`brigade fleet restore`, in the Proxmox runbook.

## What the tests cover

- `engines/hub/tests/upstream_restart.py`: native claims commit while the
  Python hub is killed, proxied routes answer 502, `/ready` answers 503 and
  then 200 after the restart. Its negative case runs with every family
  proxied and confirms that hub exits and the probe reports failures.
- `engines/hub/tests/engine_rollback.py`: the drive through the Rust
  engine, then the Python engine on the same files and on a pinned backup,
  with every row still readable. Its negative case deletes one claim row
  from a copy and expects the comparison to report exactly that row.
- `engines/hub/tests/prepublish.py`: the release job's pre-publication
  acceptance on each Linux target.
