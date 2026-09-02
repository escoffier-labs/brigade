# Fleet hub on a Proxmox LXC

This runbook provisions the Phase 2 fleet hub as an unprivileged Debian LXC on a Proxmox VE 9.x host. The hub accepts run events over Tailscale and serves the live aggregate from one SQLite database. Local journals remain authoritative. A hub outage only delays the aggregate view and causes clients to spool events locally.

The examples use the Tailscale MagicDNS name `brigade-hub`. Replace every other angle-bracket value before running a command.

## Target layout

- Proxmox VE 9.x host
- Debian 12 or Debian 13 CT template
- 1 vCPU, 512 MB RAM, 4 GB root disk
- Unprivileged CT with nesting disabled
- Tailscale inside the CT, hostname `brigade-hub`
- Brigade bound to the CT's Tailscale IPv4 address on TCP port 3774
- SQLite database at `/var/lib/brigade/fleet-hub.db`
- Root-owned, mode `0600` systemd environment file at `/etc/brigade/fleet-hub.env`

The hub exposes these endpoints:

- `GET /health`, unauthenticated liveness check
- `POST /events`, node-token-authenticated event append (the admin token only with `--allow-admin-writes`)
- `GET /status`, admin- or node-token-authenticated latest non-terminal state per node and run
- `GET /status?all=1`, the same including terminal runs
- `POST /claims` (node token) and `GET /claims` (admin or node token), repo claims
- `GET /nodes` and `POST /nodes`, admin-token-only node enrollment
- `GET /` and `GET /view/{machines,repos}`, the Fleet dashboard (admin token or the dashboard cookie; see below)

Events are deduplicated by `(node_id, run_id, sequence, digest)`. Reposting after a lost response is safe.

Two credentials exist (see the trust model in `docs/fleet-sync.md`). The **admin token** in `/etc/brigade/fleet-hub.env` is the control plane: it enrolls nodes, reads status and claims, and sets the dashboard cookie. Each machine's **node token** (`brigade fleet nodes add`) is that machine's identity: the hub derives `node_id` from it and refuses an event or claim for any other node with HTTP 403. The hub stores only a SHA-256 of each node token.

## 1. Create the CT on the Proxmox host

Download a Debian 12 or 13 standard CT template into the template storage first. Confirm the exact filename and storage names before creating the CT.

Run as root on the Proxmox host:

```bash
pct create <CTID> <TEMPLATE_STORAGE>:vztmpl/<DEBIAN_TEMPLATE>.tar.zst \
  --hostname brigade-hub \
  --ostype debian \
  --cores 1 \
  --memory 512 \
  --swap 512 \
  --rootfs <ROOT_STORAGE>:4 \
  --unprivileged 1 \
  --features nesting=0 \
  --net0 name=eth0,bridge=<PROXMOX_BRIDGE>,ip=<CT_IPV4/CIDR>,gw=<GATEWAY_IPV4>,type=veth \
  --onboot 1 \
  --start 0 \
  --ssh-public-keys <PUBLIC_KEY_FILE>

pct start <CTID>
pct enter <CTID>
```

The CT must have outbound access to the Tailscale package repository and the tailnet. Do not enable nesting for this service.

Tailscale normally needs `/dev/net/tun`. Check from inside the CT:

```bash
test -c /dev/net/tun && echo "TUN device present" || echo "TUN device missing"
```

If the device is missing, leave nesting disabled and add only the TUN device permission on the Proxmox host:

```bash
printf '%s\n' \
  'lxc.cgroup2.devices.allow: c 10:200 rwm' \
  'lxc.mount.entry: /dev/net/tun dev/net/tun none bind,create=file' \
  >> /etc/pve/lxc/<CTID>.conf
pct restart <CTID>
pct enter <CTID>
```

## 2. Install Tailscale in the CT

Run as root inside the CT. The repository path is selected from Debian's codename, so the same commands work on Debian 12 and Debian 13.

```bash
apt-get update
apt-get install -y ca-certificates curl gnupg

. /etc/os-release
curl -fsSL "https://pkgs.tailscale.com/stable/debian/${VERSION_CODENAME}.noarmor.gpg" \
  -o /usr/share/keyrings/tailscale-archive-keyring.gpg
curl -fsSL "https://pkgs.tailscale.com/stable/debian/${VERSION_CODENAME}.tailscale-keyring.list" \
  -o /etc/apt/sources.list.d/tailscale.list

apt-get update
apt-get install -y tailscale
systemctl enable --now tailscaled
tailscale up --authkey=tskey-auth-<PREAUTH_KEY> --hostname=brigade-hub --accept-dns=true
tailscale status
tailscale ip -4
```

The pre-auth key must be reusable or have enough uses for the intended rebuilds. Do not put the key in this repository or in a systemd unit. Record the IPv4 printed by `tailscale ip -4` as `<TAILSCALE_IPV4>` for the service unit below.

Confirm the MagicDNS name resolves from the CT and from one client machine:

```bash
getent hosts brigade-hub
tailscale ping brigade-hub
```

## 3. Install Python and Brigade

Debian 12 supplies Python 3.11. Debian 13 supplies a newer Python. Confirm the interpreter before installing the CLI.

```bash
python3 --version
apt-get install -y python3 python3-venv pipx sqlite3 curl
install -d -o root -g root -m 0755 /opt/pipx /usr/local/bin
python3 -c 'import sys; print(sys.version); raise SystemExit("Python 3.11+ required") if sys.version_info < (3, 11) else None'

PIPX_HOME=/opt/pipx PIPX_BIN_DIR=/usr/local/bin \
  pipx install --force \
  "git+https://github.com/escoffier-labs/brigade.git@<BRIGADE_GIT_REF>"

/usr/local/bin/brigade --version
```

Keep `<BRIGADE_GIT_REF>` identical on the hub and clients while rolling out a Phase 2 build. The service uses `/usr/local/bin/brigade`, the pipx entry point created above.

## 4. Create the hub admin token environment file

The 1Password item is named `brigade-fleet-hub`. The command below expects `op` to be authenticated for the vault containing that item and for the item field to be `password`. Run it in the CT as root, or populate the file through an approved root-only provisioning session.

```bash
install -d -o root -g root -m 0750 /etc/brigade /var/lib/brigade
umask 077
fleet_token="$(op read 'op://<VAULT>/brigade-fleet-hub/password')" && \
  printf 'BRIGADE_FLEET_TOKEN=%s\n' "$fleet_token" > /etc/brigade/fleet-hub.env
unset fleet_token
chown root:root /etc/brigade/fleet-hub.env
chmod 0600 /etc/brigade/fleet-hub.env
```

Check only the file metadata, not its contents:

```bash
stat -c '%U:%G %a %n' /etc/brigade/fleet-hub.env
```

Expected output includes `root:root 600`. The service reads `BRIGADE_FLEET_TOKEN` from this file as the admin token. Brigade does not persist the admin token in SQLite, and node tokens only as SHA-256 digests.

## 5. Install and start the systemd service

Write `/etc/systemd/system/brigade-fleet.service` with the Tailscale IPv4 captured in step 2:

```ini
[Unit]
Description=Brigade fleet hub
After=network-online.target tailscaled.service
Wants=network-online.target
Requires=tailscaled.service

[Service]
Type=simple
User=root
WorkingDirectory=/var/lib/brigade
EnvironmentFile=/etc/brigade/fleet-hub.env
ExecStart=/usr/local/bin/brigade fleet serve --host <TAILSCALE_IPV4> --port 3774 --db /var/lib/brigade/fleet-hub.db
Restart=on-failure
RestartSec=5
PrivateTmp=true
NoNewPrivileges=true
ProtectHome=true
ProtectSystem=strict
ReadWritePaths=/var/lib/brigade

[Install]
WantedBy=multi-user.target
```

The `--host` value is required. Do not bind to all interfaces; the hub is intended to listen on the Tailscale interface only. The explicit `--db` path keeps the database under `/var/lib/brigade`, independent of the service user's home directory.

A new hub starts with node tokens required for writes. Only a hub that must keep serving clients still on the shared token should add `--allow-admin-writes` to `ExecStart`; see *Migration from the shared token* below for when to add it and when to take it away.

Activate it:

```bash
systemctl daemon-reload
systemctl enable --now brigade-fleet.service
systemctl status --no-pager brigade-fleet.service
journalctl -u brigade-fleet.service -n 50 --no-pager
```

The startup log should name `<TAILSCALE_IPV4>:3774` and `/var/lib/brigade/fleet-hub.db`.

## 6. Verify health and authenticated status

Health does not require the bearer token:

```bash
curl --fail --silent --show-error http://brigade-hub:3774/health
```

Expected response:

```json
{"ok": true, "service": "brigade-fleet-hub"}
```

From a client that has the same token, check the protected status endpoint:

```bash
curl --fail --silent --show-error \
  -H "Authorization: Bearer $(<~/.brigade/fleet.token)" \
  http://brigade-hub:3774/status
```

An empty result is valid before any client reports an event. An HTTP 401 means the request reached the hub but the token did not match.

For a direct endpoint smoke test, enroll a throwaway node and post one synthetic non-terminal event with its token. Use a node and run id that cannot be confused with a real run, and revoke the node afterwards:

```bash
smoke_token="$(curl --fail --silent --show-error \
  -X POST \
  -H "Authorization: Bearer $(<~/.brigade/fleet.token)" \
  -H 'Content-Type: application/json' \
  --data '{"action":"add","node_id":"smoke-<NODE_ID>","label":"smoke test"}' \
  http://brigade-hub:3774/nodes | python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])')"

curl --fail --silent --show-error \
  -X POST \
  -H "Authorization: Bearer $smoke_token" \
  -H 'Content-Type: application/json' \
  --data '{"node_id":"smoke-<NODE_ID>","run_id":"smoke-<RUN_ID>","repo":"<REPO_NAME>","seat":"<SEAT>","harness":"<HARNESS>","state":"run.started","ts":"<ISO8601_UTC>","sequence":1,"digest":"<EVENT_DIGEST>"}' \
  http://brigade-hub:3774/events

curl --fail --silent --show-error \
  -H "Authorization: Bearer $(<~/.brigade/fleet.token)" \
  http://brigade-hub:3774/status

curl --fail --silent --show-error \
  -X POST \
  -H "Authorization: Bearer $(<~/.brigade/fleet.token)" \
  -H 'Content-Type: application/json' \
  --data '{"action":"revoke","node_id":"smoke-<NODE_ID>"}' \
  http://brigade-hub:3774/nodes
unset smoke_token
```

The response from `/events` reports `accepted: 1` for the first post. Reposting the same body reports it as a duplicate, which confirms the idempotency key. Posting the same event with the admin token instead answers HTTP 403 on a hub without `--allow-admin-writes`, which confirms that node tokens are required.

## 7. Enroll and configure each Brigade client

Every machine needs its own node token. On the machine, print its machine identity (`brigade node --machine` creates it once under `BRIGADE_HOME`, `~/.brigade/node.toml` by default; this is the identity every event and claim is stamped with, never a workspace-local one):

```bash
brigade node --machine --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["node_id"])'
```

From the operator machine that holds the admin token (`[fleet] token_file` in its own `~/.brigade/fleet.toml`, or `BRIGADE_FLEET_TOKEN`), enroll that identity. The token is printed once and never again; the hub keeps only its hash:

```bash
brigade fleet nodes add <NODE_ID> --label <HOSTNAME>
brigade fleet nodes list
```

Deliver the printed token to the machine through its existing secret-distribution process and install it as a user-readable file with mode `0600`. The admin token stays on the operator machine only; a node does not need it.

```bash
mkdir -p ~/.brigade
install -m 0600 <NODE_TOKEN_FILE> ~/.brigade/fleet-node.token
chmod 0700 ~/.brigade

cat > ~/.brigade/fleet.toml <<'EOF'
[fleet]
hub_url = "http://brigade-hub:3774"
node_token_file = "~/.brigade/fleet-node.token"
EOF

chmod 0600 ~/.brigade/fleet.toml
```

The config keys are `[fleet] hub_url`, `[fleet] node_token_file` (this machine's identity), and optionally `[fleet] token_file` (the admin token, only where `brigade fleet nodes` runs). `BRIGADE_FLEET_HUB_URL` overrides `hub_url`, `BRIGADE_FLEET_NODE_TOKEN` overrides the node token file, and `BRIGADE_FLEET_TOKEN` overrides the admin token file when set. Do not add a `host`, `port`, or `token` key to this file.

To rotate a machine's credential, revoke and re-enroll it, then replace the file on that machine:

```bash
brigade fleet nodes revoke <NODE_ID>
brigade fleet nodes add <NODE_ID> --label <HOSTNAME>
```

A revoked token is refused with HTTP 401 immediately; events the machine produces in between stay in its local spool and are delivered by the next report or `brigade fleet flush` once the new token is in place.

### Windows client

Use the same `[fleet]` keys on the Windows machine. The client expands `~` in `node_token_file`, so the same config shape works for a native Windows user profile:

```powershell
New-Item -ItemType Directory -Force "$env:USERPROFILE\.brigade" | Out-Null
Copy-Item <NODE_TOKEN_FILE> "$env:USERPROFILE\.brigade\fleet-node.token"
@'
[fleet]
hub_url = "http://brigade-hub:3774"
node_token_file = "~/.brigade/fleet-node.token"
'@ | Set-Content -Encoding utf8 "$env:USERPROFILE\.brigade\fleet.toml"
icacls "$env:USERPROFILE\.brigade\fleet-node.token" /inheritance:r /grant:r "$env:USERNAME:(R)" | Out-Null
brigade fleet status
```

Keep the token file readable only by the Windows user running Brigade. `brigade fleet flush` is the manual retry command on Windows too.

Once the client has a node identity and an active run, inspect the hub from that machine:

```bash
brigade fleet status
brigade fleet status --json
```

`brigade fleet status` queries `GET /status` and shows node, repo, run id, seat/harness, state, and age. Terminal runs are hidden by default. Use `brigade fleet status --all` to include them. `brigade fleet flush` retries the local spool immediately:

```bash
brigade fleet flush
```

Event reporting is best effort. A failed POST is appended under the Brigade home directory at `fleet-spool/<node_id>.jsonl`; it does not block the journal writer or fail the local run. A later successful report or `brigade fleet flush` sends queued events in order.

### Fleet dashboard from a laptop or phone

The hub serves a read-only web view of the same data at `http://brigade-hub:3774/` (machine cards) and `http://brigade-hub:3774/view/repos` (repo board). The dashboard accepts the admin token (not a node token). From a machine that holds it, a bearer header works as for `/status`:

```bash
curl --silent --show-error \
  -H "Authorization: Bearer $(<~/.brigade/fleet.token)" \
  http://brigade-hub:3774/ | head
```

A phone browser cannot send a bearer header, so open the page once with the token in the query string:

```text
http://brigade-hub:3774/?token=<FLEET_TOKEN>
```

The hub answers a redirect to `/` without the token and sets a `brigade_fleet_view` cookie (HttpOnly, SameSite=Strict, 30 days). The cookie is an HMAC of the token, not the token: it opens only the dashboard pages, never `/status`, `/claims`, or `/events`, and rotating the hub token invalidates it. Tradeoffs to accept before using it: the token passes once through that device's browser history, and the cookie is a 30-day read-only view of the fleet on that device. Only do this on a device you would enrol in the tailnet, and rotate the token (section 4) if the device is lost. The cookie is not marked `Secure` because the hub is plain HTTP inside Tailscale's encrypted link; do not expose the hub outside the tailnet.

Sort and filter with query parameters, for example `/?attention=1` (only failed, awaiting-approval, or stale runs), `/view/repos?sort=repo`, `/?node=<prefix>`, `/?all=1` (include finished runs). The page refreshes every 10 seconds and works with JavaScript disabled.

### Tailscale Serve identity authentication (optional)

The hub can be started with `--trust-tailscale-identity` so that dashboard routes (`/`, `/deck`, `/deck/repos`, and the legacy `/view/*` boards) authorize a request from the `Tailscale-User-Login` header injected by a Tailscale Serve reverse proxy. This removes the need to pass the admin token or set the `brigade_fleet_view` cookie for dashboard-only access when the proxy is in place.

Safe deployment contract: do not enable this unless all of the following are true:

1. The hub is bound to a loopback interface only (`127.0.0.1` or `::1`). Never bind it to all interfaces or to a routable address when this flag is on.
2. The dashboard request reaches the hub only through a Tailscale Serve reverse proxy that terminates Tailscale identity and strips any spoofed incoming `Tailscale-User-Login` header. The proxy must run on the same host as the hub, so the immediate TCP peer is loopback.
3. The backend is never exposed directly to the tailnet or to any other network without the proxy in front.

This mode treats every process inside the hub host or container as trusted because any local process can connect to the loopback backend and supply the identity header. Keep the hub in a dedicated host or container, and do not enable the flag when untrusted workloads share that network namespace.

When the flag is set, the hub authorizes a dashboard request only when the immediate TCP peer is loopback and the header is present, non-empty, at most 256 characters, and contains no control characters. The identity value is never logged or rendered in any response. JSON and API routes (`/status`, `/claims`, `/nodes`, `/cloud`, `/models`, and all `POST` endpoints) still require the bearer/node/admin token checks; the Tailscale identity header does not grant access to them.

Example service unit that binds loopback and trusts the proxy header:

```ini
[Service]
ExecStart=/usr/local/bin/brigade fleet serve --host 127.0.0.1 --port 3774 --db /var/lib/brigade/fleet-hub.db --trust-tailscale-identity
```

Pair this with a Tailscale Serve rule on the same host that forwards `https://brigade-hub` (or a funnel you control) to `http://127.0.0.1:3774`. Do not configure a Serve rule that points at a routable or remote backend address; the whole point is that the proxy is the only thing that can legitimately present the header, and it must speak to the hub over loopback.

## Migration from the shared token

A fleet deployed before per-node credentials has one token in `/etc/brigade/fleet-hub.env` and in every client's `token_file`. After the upgrade that token is the admin token, and the hub refuses event and claim posts made with it unless `--allow-admin-writes` is set. Migrate in this order so no machine loses reporting:

1. Back up the database (section 8). The upgrade adds the `nodes` table in place (schema v4); a hub built from an older ref refuses a v4 database, so keep the backup until the rollout is done.
2. Upgrade the hub (see *Upgrade and rollback*) and add `--allow-admin-writes` to `ExecStart` for the transition. Existing clients keep working exactly as before; each one logs a single `WARNING` per process naming the deprecated shared-token mode.
3. Upgrade the clients to the same ref.
4. For each machine, run `brigade fleet nodes add <NODE_ID> --label <HOSTNAME>` from the operator machine, deliver the token, and set `[fleet] node_token_file` on that machine (section 7). Leave `token_file` in place only where `brigade fleet nodes` will run. Check with `brigade fleet nodes list` and, on the machine, `brigade fleet status` followed by a run: the shared-token warning stops once the node token is in use.
5. When every machine in `brigade fleet nodes list` has posted with its own token (the `WARNING` no longer appears in any client log), remove `--allow-admin-writes` from `ExecStart`, `systemctl daemon-reload`, and restart the service. From then on a machine still on the shared token gets HTTP 403, keeps its events in the local spool, and delivers them once enrolled.
6. Rotate the admin token (section 4): it was on every machine, and it still enrolls nodes and sets the dashboard cookie.

## 8. Back up the SQLite database

Use SQLite's online backup command. Do not copy only the main database file while the service is running, because WAL data may not have been checkpointed into it.

Create `/usr/local/sbin/brigade-fleet-backup`:

```sh
#!/bin/sh
set -eu

backup_dir=/var/backups/brigade
stamp=$(date -u +%Y%m%dT%H%M%SZ)
install -d -o root -g root -m 0750 "$backup_dir"
sqlite3 /var/lib/brigade/fleet-hub.db ".backup '$backup_dir/fleet-hub-$stamp.db'"
find "$backup_dir" -type f -name 'fleet-hub-*.db' -mtime +14 -delete
```

After writing the script to a temporary file, install it:

```bash
install -o root -g root -m 0750 <BACKUP_SCRIPT_FILE> /usr/local/sbin/brigade-fleet-backup
```

Write `/etc/systemd/system/brigade-fleet-backup.service`:

```ini
[Unit]
Description=Back up the Brigade fleet hub SQLite database
After=brigade-fleet.service

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/brigade-fleet-backup
```

Write `/etc/systemd/system/brigade-fleet-backup.timer`:

```ini
[Unit]
Description=Daily Brigade fleet hub SQLite backup

[Timer]
OnCalendar=*-*-* 03:15:00
Persistent=true
Unit=brigade-fleet-backup.service

[Install]
WantedBy=timers.target
```

Enable and test the timer:

```bash
systemctl daemon-reload
systemctl enable --now brigade-fleet-backup.timer
systemctl start brigade-fleet-backup.service
systemctl list-timers brigade-fleet-backup.timer --no-pager
ls -lh /var/backups/brigade/
```

Copy backups off the CT through the operator's existing backup system. The local timer is not a replacement for an off-host copy.

## Upgrade and rollback

Before changing the Brigade ref, run a backup and save the current ref:

```bash
systemctl start brigade-fleet-backup.service
pipx list
systemctl status --no-pager brigade-fleet.service
```

Upgrade to a reviewed ref:

```bash
systemctl stop brigade-fleet.service
PIPX_HOME=/opt/pipx PIPX_BIN_DIR=/usr/local/bin \
  pipx install --force \
  "git+https://github.com/escoffier-labs/brigade.git@<NEW_BRIGADE_GIT_REF>"
systemctl start brigade-fleet.service
systemctl status --no-pager brigade-fleet.service
curl --fail --silent --show-error http://brigade-hub:3774/health
```

Roll back the application to a known-good ref with the same sequence:

```bash
systemctl stop brigade-fleet.service
PIPX_HOME=/opt/pipx PIPX_BIN_DIR=/usr/local/bin \
  pipx install --force \
  "git+https://github.com/escoffier-labs/brigade.git@<KNOWN_GOOD_BRIGADE_GIT_REF>"
systemctl start brigade-fleet.service
journalctl -u brigade-fleet.service -n 50 --no-pager
```

Keep the SQLite file during an application rollback. A hub refuses a database with a newer `PRAGMA user_version` instead of silently interpreting it: a database touched by a per-node-credentials hub (schema v4) is refused by a ref before #1150, so a rollback across that boundary restores the pre-upgrade backup from section 8 (losing events and claims received since) or stays on the newer ref with `--allow-admin-writes`.

## Troubleshooting

### `POST /events` or `POST /claims` returns HTTP 403

The token was accepted but may not act as the `node_id` in the body. Either the client sends the admin token (no `node_token_file` configured; the client also logged the shared-token `WARNING`) to a hub without `--allow-admin-writes`, or the node token installed on the machine was minted for a different `node_id` than the one `brigade node --machine --json` reports there. Compare `brigade node --machine --json` on the machine with `brigade fleet nodes list` on the operator machine, then re-enroll (section 7). Events produced in the meantime stay in the client spool and go out on the next `brigade fleet flush`.

### `GET /status` or `POST /events` returns HTTP 401

The request reached the hub, but the bearer token matched neither the admin token nor an enrolled, unrevoked node token. A revoked node token answers `unauthorized: node token revoked`; re-enroll it (section 7). Otherwise verify that the client points to the intended token file and that the hub environment file was refreshed from the same 1Password item. After changing `/etc/brigade/fleet-hub.env`, restart the service:

```bash
systemctl restart brigade-fleet.service
systemctl show brigade-fleet.service --property=EnvironmentFiles --no-pager
```

Do not print the token to logs or compare it in shell history. A queued event can remain in the client spool while the token is corrected, then be retried with:

```bash
brigade fleet flush
```

### Tailscale is down or the hub name does not resolve

Check the daemon, address, route, and MagicDNS path:

```bash
systemctl status --no-pager tailscaled.service
tailscale status
tailscale ip -4
getent hosts brigade-hub
tailscale ping brigade-hub
```

If the daemon is stopped, restore it and check the hub service again:

```bash
systemctl restart tailscaled.service
systemctl restart brigade-fleet.service
```

If the CT has no `/dev/net/tun`, apply the host-side TUN configuration in step 1. Do not enable nesting as a workaround.

### SQLite reports `database is locked`

The hub uses WAL mode, a 5-second SQLite busy timeout, and a 10-second connection timeout. Find competing writers and inspect the service log:

```bash
journalctl -u brigade-fleet.service --since '-15 min' --no-pager
fuser -v /var/lib/brigade/fleet-hub.db
sqlite3 /var/lib/brigade/fleet-hub.db 'PRAGMA integrity_check;'
```

Only the hub should write the database. Stop the service before any offline repair or manual replacement, and never delete the `-wal` or `-shm` files while the service is running:

```bash
systemctl stop brigade-fleet.service
sqlite3 /var/lib/brigade/fleet-hub.db 'PRAGMA integrity_check;'
systemctl start brigade-fleet.service
```

If lock errors continue, check that the backup script is using `.backup` and not a second long-running writer, then review the CT disk and memory pressure.
