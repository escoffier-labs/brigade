# Fleet Hub on a generic Linux host

This runbook installs the Fleet Hub directly on an existing Linux computer,
bare-metal server, VM, or VPS. Proxmox is not a runtime dependency: the hub is
a Python standard-library HTTP service backed by SQLite. This recipe does not
install worker engines, model CLIs, or model-seat credentials. Those belong on
machines that execute work; the hub only receives fleet events and serves the
dashboard.

## Support and sizing

**Tested support:** Brigade supports Python 3.10 or newer. The repository tests
the hub, authentication, ingestion, dashboard, migrations, and SQLite recovery
on its CI platforms. The pinned release used below is `brigade-cli==0.28.0`.

**Proposed hosting recipe:** the systemd deployment and sizing here are an
operator recipe, not a claim that every Linux distribution, reverse proxy, or
filesystem has passed live acceptance. Record the acceptance evidence in the
last section before calling a particular host tested.

The documented starting target is 1 CPU, 512 MiB RAM, and 4 GiB local disk.
Those are operational targets with room for the OS, not measured minimums.
Measure memory, event volume, and database growth on the intended fleet before
reducing them or setting production capacity.

## Safety and storage contract

- Bind to numeric loopback (`127.0.0.1`) by default. Use a same-host HTTPS
  reverse proxy or a private overlay network; do not expose port 3774 directly
  to the public Internet.
- Keep the admin token in a root-controlled file. Do not put it in command
  arguments, shell history, unit text, URLs, or logs. Give each worker its own
  node token; workers never need the admin token.
- Persist the **entire** `/var/lib/brigade` directory on local storage. The
  database, `fleet-hub.db-wal`, and `fleet-hub.db-shm` must remain together.
  Do not place it on NFS, SMB, a cloud-synced directory, or storage that copies
  only the main database file.
- Local worker journals remain authoritative. A hub outage queues client
  events; it does not move execution dependencies onto the hub.

## 1. Record the platform and install a pinned release

Record the exact inputs rather than only the distribution marketing name:

```bash
cat /etc/os-release
uname -a
python3 --version
python3 - <<'PY'
import sqlite3, sys
print("python:", sys.version)
print("sqlite runtime:", sqlite3.sqlite_version)
print("sqlite module:", sqlite3.version)
PY
```

The `sqlite runtime` line is the SQLite library actually linked to Python and
is the relevant value for the hub. The `sqlite3` executable may link another
version; record `sqlite3 --version` too if it will perform backups.

Choose **one** foreground installation below. Each is pinned to the released
version and ends by running the hub on loopback. Press Ctrl-C to stop it.

### pipx (recommended for an interactive trial)

Install pipx using the distribution package or the [official pipx installation
guide](https://pipx.pypa.io/stable/installation/), then:

```bash
pipx install 'brigade-cli==0.28.0'
pipx runpip brigade-cli show brigade-cli
umask 077
python3 - <<'PY' > "$HOME/.brigade-fleet.env"
import secrets
print("BRIGADE_FLEET_TOKEN=" + secrets.token_urlsafe(32))
PY
set -a; . "$HOME/.brigade-fleet.env"; set +a
brigade fleet serve --host 127.0.0.1 --port 3774 --db "$HOME/.local/share/brigade/fleet-hub.db"
```

### uv tool

After installing uv through its reviewed distribution channel:

```bash
uv tool install 'brigade-cli==0.28.0'
uv tool run --from 'brigade-cli==0.28.0' brigade --version
set -a; . "$HOME/.brigade-fleet.env"; set +a
uv tool run --from 'brigade-cli==0.28.0' brigade fleet serve \
  --host 127.0.0.1 --port 3774 --db "$HOME/.local/share/brigade/fleet-hub.db"
```

### Standard-library venv

```bash
python3 -m venv "$HOME/.local/share/brigade/venv"
"$HOME/.local/share/brigade/venv/bin/python" -m pip install 'brigade-cli==0.28.0'
set -a; . "$HOME/.brigade-fleet.env"; set +a
"$HOME/.local/share/brigade/venv/bin/brigade" fleet serve \
  --host 127.0.0.1 --port 3774 --db "$HOME/.local/share/brigade/fleet-hub.db"
```

Do not mix installers for one deployment. The service below uses a dedicated
venv so its interpreter and package location are explicit.

## 2. Install the least-privilege system service

Run this section as root. Create a locked service account, local data
directory, application venv, and an environment file readable by the service:

```bash
useradd --system --home-dir /var/lib/brigade --shell /usr/sbin/nologin brigade-hub
install -d -o brigade-hub -g brigade-hub -m 0750 /var/lib/brigade
python3 -m venv /opt/brigade-fleet
/opt/brigade-fleet/bin/python -m pip install 'brigade-cli==0.28.0'
install -d -o root -g brigade-hub -m 0750 /etc/brigade
umask 077
python3 - <<'PY' > /etc/brigade/fleet-hub.env
import secrets
print("BRIGADE_FLEET_TOKEN=" + secrets.token_urlsafe(32))
PY
chown root:brigade-hub /etc/brigade/fleet-hub.env
chmod 0640 /etc/brigade/fleet-hub.env
```

The heredoc generates the secret directly into the protected file; it is not
an argument or command-history value. A secret manager may write the same file
instead. Inspect only metadata with
`stat -c '%U:%G %a %n' /etc/brigade/fleet-hub.env`.

Create `/etc/systemd/system/brigade-fleet.service`:

```ini
[Unit]
Description=Brigade Fleet Hub
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=brigade-hub
Group=brigade-hub
WorkingDirectory=/var/lib/brigade
EnvironmentFile=/etc/brigade/fleet-hub.env
ExecStart=/opt/brigade-fleet/bin/brigade fleet serve --host 127.0.0.1 --port 3774 --db /var/lib/brigade/fleet-hub.db
Restart=on-failure
RestartSec=5
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
PrivateDevices=true
ProtectHome=true
ProtectSystem=strict
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictSUIDSGID=true
ReadWritePaths=/var/lib/brigade

[Install]
WantedBy=multi-user.target
```

Activate the service and use these lifecycle commands:

```bash
systemctl daemon-reload
systemctl enable --now brigade-fleet.service
systemctl start brigade-fleet.service
systemctl stop brigade-fleet.service
systemctl status --no-pager brigade-fleet.service
journalctl -u brigade-fleet.service -n 100 --no-pager
```

Start it again after exercising stop. `enable` supplies reboot recovery.
Validate it explicitly during acceptance:

```bash
systemctl start brigade-fleet.service
systemctl reboot
# After reconnecting:
systemctl is-enabled brigade-fleet.service
systemctl is-active brigade-fleet.service
curl --fail --silent --show-error http://127.0.0.1:3774/health
```

## 3. Verify authentication, ingestion, and dashboard enrollment

Health is deliberately unauthenticated:

```bash
curl --fail --silent --show-error http://127.0.0.1:3774/health
```

On an administrator workstation, copy the admin token into a mode `0600`
token file through the approved secret channel and configure `hub_url` and
`token_file`. Never paste the token into a command. Obtain a worker's identity
with `brigade node --machine --json`, then enroll it:

```bash
brigade fleet nodes add <NODE_ID> --label <HOST_LABEL>
brigade fleet nodes list
```

The returned node token is shown once. Deliver it securely to the worker as a
mode `0600` file and set `[fleet] node_token_file`; do not copy the admin token
to the worker. `brigade fleet status` proves node authentication. Start a
disposable run on that worker (or use the endpoint smoke procedure in the
[Proxmox runbook](fleet-hub-proxmox.md#6-verify-health-and-authenticated-status)),
then run `brigade fleet flush` and confirm the run appears in
`brigade fleet status --all`; this proves ingestion.

Dashboard enrollment must use an HTTPS origin, or numeric loopback HTTP for a
browser on this host. With the admin token file configured:

```bash
brigade fleet enroll --label <DEVICE_LABEL> --base-url http://127.0.0.1:3774
```

Open the printed five-minute URL, submit the confirmation form, and confirm
the enrolled browser reaches `/deck`. The URL contains a one-time enrollment
code, not the admin token; do not log or share it. For remote browsers, put an
HTTPS reverse proxy on the same host, keep the backend on loopback, and follow
the proxy contract in the Proxmox runbook.

## 4. Prove crash and restart retention

After ingesting the disposable event, record its visible run ID. Simulate a
process crash and let systemd restart it:

```bash
systemctl kill --signal=KILL brigade-fleet.service
systemctl is-active brigade-fleet.service
journalctl -u brigade-fleet.service -n 50 --no-pager
brigade fleet status --all
```

Confirm the recorded run remains visible. Also inspect SQLite through Python,
which uses the same runtime library as the service:

```bash
sudo -u brigade-hub /opt/brigade-fleet/bin/python - <<'PY'
import sqlite3
db = sqlite3.connect('/var/lib/brigade/fleet-hub.db')
print('sqlite runtime:', sqlite3.sqlite_version)
print('journal:', db.execute('PRAGMA journal_mode').fetchone()[0])
print('schema:', db.execute('PRAGMA user_version').fetchone()[0])
print('integrity:', db.execute('PRAGMA integrity_check').fetchone()[0])
PY
```

Expect `journal: wal` and `integrity: ok`. Current WAL-reset guidance is not a
routine repair step: never delete, rename, or copy `fleet-hub.db-wal` or
`fleet-hub.db-shm` independently while the hub is running. A clean shutdown
may remove or checkpoint sidecars; their absence then is normal. If recovery
is required, stop the service, preserve the complete directory, and restore a
verified online backup rather than forcing a WAL reset.

## 5. Online backup and clean restore

Back up a live hub using Python's online backup API, which is equivalent in
safety to the SQLite shell `.backup` command and uses the hub's linked SQLite:

```bash
install -d -o brigade-hub -g brigade-hub -m 0750 /var/backups/brigade
sudo -u brigade-hub /opt/brigade-fleet/bin/python - <<'PY'
import sqlite3
source = sqlite3.connect('/var/lib/brigade/fleet-hub.db')
backup = sqlite3.connect('/var/backups/brigade/fleet-hub-backup.db')
with backup:
    source.backup(backup)
print(backup.execute('PRAGMA integrity_check').fetchone()[0])
backup.close()
source.close()
PY
```

Expect `ok`. Copy the completed backup off-host using the approved backup
system. To prove a clean restore without risking production, restore into a
new local directory and run a temporary loopback instance on another port:

```bash
systemctl stop brigade-fleet.service
install -d -o brigade-hub -g brigade-hub -m 0750 /var/lib/brigade-restore-test
install -o brigade-hub -g brigade-hub -m 0600 \
  /var/backups/brigade/fleet-hub-backup.db /var/lib/brigade-restore-test/fleet-hub.db
sudo -u brigade-hub sh -c \
  '. /etc/brigade/fleet-hub.env; export BRIGADE_FLEET_TOKEN; exec timeout 20 /opt/brigade-fleet/bin/brigade fleet serve --host 127.0.0.1 --port 3775 --db /var/lib/brigade-restore-test/fleet-hub.db'
systemctl start brigade-fleet.service
```

In a second terminal, check `http://127.0.0.1:3775/health` and authenticated
status before the timeout. Then remove the restore-test directory. The shell
reads the protected file without displaying its value; do not enable shell
tracing. For an actual restore, stop the service, move the complete old
data directory aside, install the verified backup as `fleet-hub.db`, fix
ownership, and start the service.

## 6. Schema-aware upgrades and rollback

Before an upgrade, make and verify an online backup, record the installed
version and `PRAGMA user_version`, and retain the old venv. Install the new
released pin into a separate path, stop the service, change `ExecStart`, run
`systemctl daemon-reload`, and start it. Re-run health, authentication,
ingestion, dashboard, and backup checks.

The hub migrates schemas forward and refuses schemas newer than the running
binary understands. An application-only rollback is safe only when the old
binary supports the resulting `PRAGMA user_version`. Otherwise stop the hub
and restore the pre-upgrade backup. That discards all post-backup writes,
including events, enrollment changes, and policy changes; workers may resend
only events still present in their local spools. Announce and account for that
loss before restoring. Never point old and new binaries at the database at the
same time.

## 7. Removal while preserving data

This removes the service and executable but deliberately retains the complete
database directory and backups:

```bash
systemctl disable --now brigade-fleet.service
rm -f /etc/systemd/system/brigade-fleet.service
systemctl daemon-reload
rm -rf /opt/brigade-fleet
userdel brigade-hub
```

Do **not** remove `/var/lib/brigade`, `/var/backups/brigade`, or the protected
token file until retention and secret-destruction policies authorize it. Note
that the preserved files retain the deleted account's numeric UID; assign
ownership to the replacement service account during recovery.

## Acceptance record

For each tested host, retain a dated record containing:

1. OS release, kernel, CPU/RAM/disk, Python version, `sqlite3.sqlite_version`,
   optional SQLite CLI version, Brigade pin, and database `user_version`.
2. Loopback/private reachability and token-file owner/mode; confirm secrets are
   absent from history, service arguments, and journals.
3. Health, node authentication, ingestion, and dashboard enrollment results.
4. Crash/restart retention and an actual reboot recovery result.
5. Online backup integrity and clean restore results, including retained WAL
   sidecars and confirmation that the storage is local.
6. Upgrade and rollback rehearsal results, including the schema boundary and
   the stated potential loss of post-backup writes.

Until that record exists, describe the host as **proposed**, not tested.
