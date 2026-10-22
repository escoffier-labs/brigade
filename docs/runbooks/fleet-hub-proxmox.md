# Fleet hub on a Proxmox LXC

This runbook provisions the Phase 2 fleet hub as an unprivileged Debian LXC on a Proxmox VE 9.x host. Tailscale Serve exposes the hub over HTTPS and forwards requests to a loopback-only Brigade service. The hub serves the live aggregate from one SQLite database. Local journals remain authoritative. A hub outage delays the aggregate view and causes clients to spool events locally. Hub-based model admission can still refuse launches.

The Tailscale Serve URL is printed by `tailscale serve status`. Use its HTTPS tailnet DNS name as `<HUB_TAILNET_FQDN>` wherever the client URL appears below. Replace every other angle-bracket value before running a command.

## Target layout

- Proxmox VE 9.x host
- Debian 12 or Debian 13 CT template
- 1 vCPU, 512 MB RAM, 4 GB root disk
- Unprivileged CT with nesting disabled
- Tailscale inside the CT, hostname `brigade-hub`
- Brigade bound to `127.0.0.1:3774`, behind Tailscale Serve HTTPS
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
- `GET /deck/roster` and `POST /deck/roster`, the roster page (admin Authorization bearer header required to save, see `docs/fleet-sync.md`)

Events are deduplicated by `(node_id, run_id, sequence, digest)`. Reposting after a lost response is safe.

Two bearer credential classes exist (see the trust model in `docs/fleet-sync.md`). The **admin token** in `/etc/brigade/fleet-hub.env` is the control plane: it enrolls nodes, reads status and claims, and mints dashboard enrollment codes. Each machine's **node token** (`brigade fleet nodes add`) is that machine's identity: the hub derives `node_id` from it and refuses an event or claim for any other node with HTTP 403. The hub stores only a SHA-256 of each node token.

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

The pre-auth key must be reusable or have enough uses for the intended rebuilds. Do not put the key in this repository or in a systemd unit. The IPv4 is useful for checking that Tailscale is connected; the Brigade service below binds to loopback.

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
install -d -o root -g root -m 0750 /etc/brigade
install -d -o root -g root -m 0700 /var/lib/brigade
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

Write `/etc/systemd/system/brigade-fleet.service`:

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
ExecStart=/usr/local/bin/brigade fleet serve --host 127.0.0.1 --port 3774 --db /var/lib/brigade/fleet-hub.db --trust-forwarded-proto
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

The `--host` value is required. Do not bind to all interfaces or a routable address; the hub must listen on loopback so Tailscale Serve is its only network frontend. The explicit `--db` path keeps the database under `/var/lib/brigade`, independent of the service user's home directory. The database directory must be owned by root and mode `0700`.

A new hub starts with node tokens required for writes. Only a hub that must keep serving clients still on the shared token should add `--allow-admin-writes` to `ExecStart`; see *Migration from the shared token* below for when to add it and when to take it away.

Activate it:

```bash
systemctl daemon-reload
systemctl enable --now brigade-fleet.service
systemctl status --no-pager brigade-fleet.service
journalctl -u brigade-fleet.service -n 50 --no-pager
tailscale serve --bg 3774
tailscale serve status
```

Tailscale Serve provides the HTTPS MagicDNS origin `https://<HUB_TAILNET_FQDN>` and proxies it to the local service. The startup log should name `127.0.0.1:3774` and `/var/lib/brigade/fleet-hub.db`. Keep the Serve proxy on this same CT; do not forward to the Tailscale interface or another host.

## 6. Verify health and authenticated status

Health does not require the bearer token:

```bash
curl --fail --silent --show-error https://<HUB_TAILNET_FQDN>/health
```

Expected response:

```json
{"ok": true, "service": "brigade-fleet-hub"}
```

From a client that has the same token, check the protected status endpoint:

```bash
curl --fail --silent --show-error \
  -H "Authorization: Bearer $(<~/.brigade/fleet.token)" \
  https://<HUB_TAILNET_FQDN>/status
```

An empty result is valid before any client reports an event. An HTTP 401 means the request reached the hub but the token did not match.

For a direct endpoint smoke test, enroll a throwaway node and post one synthetic non-terminal event with its token. Use a node and run id that cannot be confused with a real run, and revoke the node afterwards:

```bash
smoke_token="$(curl --fail --silent --show-error \
  -X POST \
  -H "Authorization: Bearer $(<~/.brigade/fleet.token)" \
  -H 'Content-Type: application/json' \
  --data '{"action":"add","node_id":"smoke-<NODE_ID>","label":"smoke test"}' \
  https://<HUB_TAILNET_FQDN>/nodes | python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])')"

curl --fail --silent --show-error \
  -X POST \
  -H "Authorization: Bearer $smoke_token" \
  -H 'Content-Type: application/json' \
  --data '{"node_id":"smoke-<NODE_ID>","run_id":"smoke-<RUN_ID>","repo":"<REPO_NAME>","seat":"<SEAT>","harness":"<HARNESS>","state":"run.started","ts":"<ISO8601_UTC>","sequence":1,"digest":"<EVENT_DIGEST>"}' \
  https://<HUB_TAILNET_FQDN>/events

curl --fail --silent --show-error \
  -H "Authorization: Bearer $(<~/.brigade/fleet.token)" \
  https://<HUB_TAILNET_FQDN>/status

curl --fail --silent --show-error \
  -X POST \
  -H "Authorization: Bearer $(<~/.brigade/fleet.token)" \
  -H 'Content-Type: application/json' \
  --data '{"action":"revoke","node_id":"smoke-<NODE_ID>"}' \
  https://<HUB_TAILNET_FQDN>/nodes
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
hub_url = "https://<HUB_TAILNET_FQDN>"
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
hub_url = "https://<HUB_TAILNET_FQDN>"
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

The hub serves a read-only web view of the same data at `https://<HUB_TAILNET_FQDN>/` (machine cards) and `https://<HUB_TAILNET_FQDN>/view/repos` (repo board). The dashboard accepts the admin token (not a node token). From a machine that holds it, a bearer header works as for `/status`:

```bash
curl --silent --show-error \
  -H "Authorization: Bearer $(<~/.brigade/fleet.token)" \
  https://<HUB_TAILNET_FQDN>/ | head
```

Before enrolling a phone, configure an HTTPS proxy that forwards to a hub
bound to numeric loopback (`127.0.0.1`) with
`--trust-forwarded-proto`. The proxy must preserve the browser-facing Host
and overwrite `X-Forwarded-Proto` with exactly one `https` value. The hub
recognizes this scheme only from an immediate loopback peer and sets Secure
on the cookie. Tailscale Serve HTTPS needs this configuration and explicit
opt-in. Tailscale identity trust is optional for this enrollment flow.

The HTTPS Tailscale Serve origin supports node, event, messaging, and
dashboard operations. Do not configure clients with the direct backend URL
`http://<TAILSCALE_IPV4>:3774`; clients reject remote cleartext hub URLs before
attaching bearer credentials.
`--base-url` changes only the printed browser origin, never the admin request
endpoint. It cannot make the direct HTTP backend acceptable.
Both URLs must contain only an origin. Set each client's `hub_url` to
`https://<HUB_TAILNET_FQDN>` and preserve its existing node credentials.

Replace the HTTPS origin in these examples with `https://<HUB_TAILNET_FQDN>`.
On the admin client with its already configured admin credential,
override the configured `hub_url` to mint a private five-minute read-only
enrollment URL:

```bash
BRIGADE_FLEET_HUB_URL=https://hub.example.test brigade fleet enroll --label "phone" --base-url https://hub.example.test
```

Open the printed URL on the device and confirm the form. GET does not consume
the code. Same-origin POST consumes it once and redirects to `/deck` with an
independent, host-only 30-day cookie. Old bearer query URLs and derived HMAC
cookies are refused. Each device must enroll once after upgrade. The cookie
reads HTML only, without roster/policy writes or JSON access.

For a lost device, list browser sessions, identify its label and revoke each
unwanted session id:

```bash
BRIGADE_FLEET_HUB_URL=https://hub.example.test brigade fleet sessions --dashboard
BRIGADE_FLEET_HUB_URL=https://hub.example.test brigade fleet sessions --dashboard --revoke SESSION_ID
```

Follow the next-page cursor when listings exceed 100 sessions. Repeat listing
from page one during incident response to catch concurrent inserts before an
existing cursor. `--all` includes metadata records retained after access expires
or a session is revoked. Bearer rotation does not revoke browser sessions.
Use individual session revocation instead.
These dashboard operations require the current admin token, never a node token.
Unadorned `fleet sessions` still reports interactive editor presence.

When administering a local hub through numeric loopback HTTP, `--base-url`
can name its browser-accessible HTTPS proxy. Ordinary HTTP without
`--trust-forwarded-proto` ignores forwarding headers.

Only an injected admin Authorization header enables HTML editors. For routine
changes use `fleet models set`, `fleet preference set` or the `fleet policy` CLI. Schema 23 is additive at startup. An older binary
refuses that newer database schema on rollback.

Sort and filter with query parameters, for example `/?attention=1` (only failed, awaiting-approval, or stale runs), `/view/repos?sort=repo`, `/?node=<prefix>`, `/?all=1` (include finished runs). The page refreshes every 10 seconds and works with JavaScript disabled.

### Tailscale Serve identity authentication (optional)

The hub can be started with `--trust-tailscale-identity` so that dashboard routes (`/`, `/deck`, `/deck/repos`, and the legacy `/view/*` boards) authorize a request from the `Tailscale-User-Login` header injected by a Tailscale Serve reverse proxy. This removes the need to pass the admin token or set the `brigade_fleet_view` cookie for dashboard-only access when the proxy is in place.

Safe deployment contract: do not enable this unless all of the following are true:

1. The hub is bound to a loopback interface only (`127.0.0.1`). Never bind it to all interfaces or to a routable address when this flag is on.
2. The dashboard request reaches the hub only through a Tailscale Serve reverse proxy that terminates Tailscale identity and strips any spoofed incoming `Tailscale-User-Login` header. The proxy must run on the same host as the hub, so the immediate TCP peer is loopback.
3. The backend is never exposed directly to the tailnet or to any other network without the proxy in front.

This mode treats every process inside the hub host or container as trusted because any local process can connect to the loopback backend and supply the identity header. Keep the hub in a dedicated host or container, and do not enable the flag when untrusted workloads share that network namespace.

When the flag is set, the hub authorizes a dashboard request only when the immediate TCP peer is loopback and the header is present, non-empty, at most 256 characters, and contains no control characters. The identity value is never logged or rendered in any response. JSON and API routes (`/status`, `/claims`, `/nodes`, `/cloud`, `/models`, and all `POST` endpoints) still require the bearer/node/admin token checks; the Tailscale identity header does not grant access to them.

Example service unit that binds loopback and trusts the proxy header:

```ini
[Service]
ExecStart=/usr/local/bin/brigade fleet serve --host 127.0.0.1 --port 3774 --db /var/lib/brigade/fleet-hub.db --trust-tailscale-identity
```

Pair this with a Tailscale Serve rule on the same host that forwards `https://<HUB_TAILNET_FQDN>` (or a funnel you control) to `http://127.0.0.1:3774`. Do not configure a Serve rule that points at a routable or remote backend address; the whole point is that the proxy is the only thing that can legitimately present the header, and it must speak to the hub over loopback.

## Migration from the shared token

A fleet deployed before per-node credentials has one token in `/etc/brigade/fleet-hub.env` and in every client's `token_file`. After the upgrade that token is the admin token, and the hub refuses event and claim posts made with it unless `--allow-admin-writes` is set. Migrate in this order so no machine loses reporting:

1. Back up the database (section 8). The upgrade adds the `nodes` table in place (schema v4); a hub built from an older ref refuses a v4 database, so keep the backup until the rollout is done.
2. Upgrade the hub (see *Upgrade and rollback*) and add `--allow-admin-writes` to `ExecStart` for the transition. Existing clients keep working exactly as before; each one logs a single `WARNING` per process naming the deprecated shared-token mode.
3. Upgrade the clients to the same ref.
4. For each machine, run `brigade fleet nodes add <NODE_ID> --label <HOSTNAME>` from the operator machine, deliver the token, and set `[fleet] node_token_file` on that machine (section 7). Leave `token_file` in place only where `brigade fleet nodes` will run. Check with `brigade fleet nodes list` and, on the machine, `brigade fleet status` followed by a run: the shared-token warning stops once the node token is in use.
5. When every machine in `brigade fleet nodes list` has posted with its own token (the `WARNING` no longer appears in any client log), remove `--allow-admin-writes` from `ExecStart`, `systemctl daemon-reload`, and restart the service. From then on a machine still on the shared token gets HTTP 403, keeps its events in the local spool, and delivers them once enrolled.
6. Rotate the admin token (section 4): it was on every machine, and it still enrolls nodes and mints dashboard enrollment codes.

## 8. Back up the SQLite database

Use SQLite's online backup command. Do not copy only the main database file while the service is running, because WAL data may not have been checkpointed into it.

Create `/usr/local/sbin/brigade-fleet-backup`:

```sh
#!/bin/sh
set -eu

backup_dir=/var/backups/brigade
stamp=$(date -u +%Y%m%dT%H%M%SZ)
umask 077
install -d -o root -g root -m 0700 "$backup_dir"
sqlite3 /var/lib/brigade/fleet-hub.db ".backup '$backup_dir/fleet-hub-$stamp.db'"
chmod 0600 "$backup_dir/fleet-hub-$stamp.db"
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
UMask=0077
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

For an existing backup directory, stop the timer, confirm the path is the
dedicated backup directory, then tighten it and existing database backups:

```bash
systemctl stop brigade-fleet-backup.timer
test -d /var/backups/brigade && test ! -L /var/backups/brigade
chown root:root /var/backups/brigade
chmod 0700 /var/backups/brigade
find /var/backups/brigade -type f -name 'fleet-hub-*.db' -exec chmod 0600 {} +
systemctl start brigade-fleet-backup.timer
```

## 9. Compact the event journal (optional)

The classic boards (`/view/machines`, `/view/repos`) refresh about every 10s and now read a latest-state-per-run page: terminal history is windowed to 24h unless `all=1`, and each page is LIMITed. That keeps render cost flat as the journal grows. The events table itself is still an append-only log. Dashboard GET handlers never delete rows.

If the SQLite file is still larger than you want, prune events older than N days as a planned maintenance step after a backup (section 8). Stop or briefly idle writers if you can; WAL mode allows a live delete, but take the backup first.

```bash
systemctl start brigade-fleet-backup.service
cutoff=$(date -u -d '14 days ago' +%Y-%m-%dT%H:%M:%SZ)
sqlite3 /var/lib/brigade/fleet-hub.db \
  "DELETE FROM events WHERE received_at < '$cutoff';"
sqlite3 /var/lib/brigade/fleet-hub.db "PRAGMA wal_checkpoint(TRUNCATE);"
sqlite3 /var/lib/brigade/fleet-hub.db "VACUUM;"
```

Fourteen days is a starting horizon, not a required policy. Keep at least the deck history window (default 24h, configurable as `stale_history_after_seconds`). Do not run this from a read-only dashboard request. Confirm `/health` and a board refresh afterward.

## Upgrade and rollback

Before changing the Brigade ref, run a backup and save the current ref. The current hub uses schema 32, which adds `events.context` and `model_policy.t3_model`. Startup also repairs either missing additive column on a database already marked schema 32. Upgrade the hub before enabling T3 lifecycle reporting or native T3 launch models. See [T3 lifecycle reporting](../fleet-t3-events.md) for rollback constraints. The section 8 backup precedes the rollout:

```bash
systemctl start brigade-fleet-backup.service
pipx list
systemctl status --no-pager brigade-fleet.service
```

For installations created from an earlier version of this runbook, migrate
the direct Tailnet HTTP endpoint before upgrading clients. Stop the hub,
confirm `/var/lib/brigade` is the dedicated data directory (not a symlink),
and tighten its ownership and mode. Replace the service unit with the
loopback-only `ExecStart` from section 5, then restart the hub and enable
Tailscale Serve. Update each client to `https://<HUB_TAILNET_FQDN>`; offline
events and messages remain in local spools until the new URL is configured.

```bash
systemctl stop brigade-fleet.service
test -d /var/lib/brigade && test ! -L /var/lib/brigade
chown root:root /var/lib/brigade
chmod 0700 /var/lib/brigade
stat -c '%U:%G %a %n' /var/lib/brigade
```

After editing the service unit, activate and verify the proxy:

```bash
systemctl daemon-reload
systemctl start brigade-fleet.service
tailscale serve --bg 3774
tailscale serve status
curl --fail --silent --show-error https://<HUB_TAILNET_FQDN>/health
```

Upgrade to a reviewed ref:

```bash
systemctl stop brigade-fleet.service
PIPX_HOME=/opt/pipx PIPX_BIN_DIR=/usr/local/bin \
  pipx install --force \
  "git+https://github.com/escoffier-labs/brigade.git@<NEW_BRIGADE_GIT_REF>"
systemctl start brigade-fleet.service
systemctl status --no-pager brigade-fleet.service
curl --fail --silent --show-error https://<HUB_TAILNET_FQDN>/health
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


## External agent principals

Upgrade the hub to schema 31 before enabling agent sends. Agents start with no
send grants. A node may supply a display-only `--sender-label`. Agents cannot.
Every new outbound agent message, including a reply, requires a matching send grant.

Enroll using the administrator credential and stream the token directly into a
private file. The command prints it once, but it is derived from the
administrator credential, so treat it as a secret rather than a single-use code. Substitute an enrolled node ID for `example-node`. The token
file must be owned by the invoking effective user where the platform exposes an
effective user ID, have no group or other access, and contain only the token,
with no trailing newline.

```bash
set -euo pipefail
umask 077
brigade fleet msg principals add example-agent --may-send-to example-node --json |
  jq -j '.principal.token' > example-agent.token
brigade fleet msg principals show example-agent --json
brigade fleet msg list --token-file example-agent.token --json
```

`--token-file PATH` takes precedence over `BRIGADE_FLEET_MESSAGE_TOKEN_FILE`, then
the node credential. Set the path only for message subprocesses. A token file does
not require an enrolled node identity. Do not pass a token value in argv or an
agent environment variable. Administrator credentials are refused for messaging.
Use a stable `--client-id` when retrying a send or reply. Check acknowledgements
with `brigade fleet msg sent --id MESSAGE_ID --token-file example-agent.token
--json`, and drain that credential's queue with `brigade fleet msg flush
--token-file example-agent.token --json`.

Replace grants, clear send and reply permission, or revoke the credential:

```bash
brigade fleet msg principals grants example-agent --may-send-to example-node
brigade fleet msg principals grants example-agent --clear
brigade fleet msg principals revoke example-agent
```

Grants accept exact node IDs, exact `agent:NAME`, `node:*`, `agent:*`, or `*`.
Agents cannot send to themselves. Each agent may have 200 unacknowledged
messages per recipient, counting both initial sends and replies. The existing
daily sender quota also applies. A grant change applies to new sends and replies.
Replaying an accepted client ID returns its original message. Re-enrollment without grants clears previous grants.

Without grants, `principals add` omits `may_send_to` so it can enroll against a
schema 30 hub. If grants change after an enrollment whose local finalization
failed, replay returns `principal_enrollment_grants_changed` (409) and clears the
stale local retry record. Use `principals show` to inspect the current principal.
Revoke before re-enrolling if a replacement credential is needed.

While a message remains queued, retrying its client ID with the same payload
returns the existing queued result. A different payload fails locally with
`spool_conflict` and leaves the original message queued. Use `msg flush` to retry
delivery. JSON errors have a non-null local code for token-file validation,
admin-token refusal, client-ID validation, spool conflicts, and other invalid
arguments. Transport failures use `hub_unavailable`.

## Fleet MCP station

`brigade fleet mcp` exposes `fleet_who`, `fleet_send`, `fleet_inbox`, `fleet_ack`,
`fleet_reply`, `fleet_sent`, and `fleet_claims` through the shared MCP harness.
Stdio is the default. Message bodies are fenced as `peer-message` data, with
sender attribution outside the body and injection scan findings in `scan`.
Findings remain visible alongside the message.

The station uses the same credential resolution as `fleet msg`: an explicit
`--principal-token-file PATH`, then `BRIGADE_FLEET_MESSAGE_TOKEN_FILE`, then the
configured node token. Tool arguments cannot select a sender or credential.
Admin credentials are refused. Agent recipient discovery follows send grants.
The hub must support the message API's `who` and `claims` read actions. Directory
results omit local checkout paths and credential material. Claims are a read-only
fleet snapshot available to authenticated message principals.

For Claude Code:

```bash
claude mcp add --transport stdio fleet -- brigade fleet mcp
```

For Codex, add this to `config.toml`:

```toml
[mcp_servers.fleet]
command = "brigade"
args = ["fleet", "mcp"]
```

For Cursor, add this server to the MCP configuration:

```json
{
  "mcpServers": {
    "fleet": {
      "command": "brigade",
      "args": ["fleet", "mcp"]
    }
  }
}
```

To use loopback Streamable HTTP, set a separate MCP bearer token with at least
32 characters. The Fleet Hub credential stays in its existing protected store.

```bash
export BRIGADE_MCP_HTTP_TOKEN="$(openssl rand -hex 32)"
brigade fleet mcp --http --host 127.0.0.1 --port 8933
```

From another terminal with the same MCP token set:

```bash
curl --fail http://127.0.0.1:8933/mcp \
  -H "Authorization: Bearer $BRIGADE_MCP_HTTP_TOKEN" \
  -H 'Content-Type: application/json' \
  --data '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

The HTTP listener rejects non-loopback binds, untrusted Host/Origin headers,
and missing or incorrect bearer tokens. It is stateless and serves no SSE stream.

`fleet_send` accepts `to`, `body`, and optional `reply_to`, `kind`, `thread`, and
`client_id`. `fleet_reply` also accepts an optional `client_id`. Reuse that key
with identical arguments for duplicate-free retries under the same credential.
See the [Executor recipe](../fleet-mcp-executor.md) for annotations, explicit
approval policy, retry limits and manual compatibility checks.
It supplies the subject `Fleet message` and normal priority. `reply_to` must be
an inbox message from `to`. Sends and replies use the existing durable offline
spool and report `delivery` as `delivered` or `spooled`. `fleet_ack` accepts an
array of decimal message ID strings and returns a result for each ID, including
failures. Successful acknowledgements remain recorded when another ID fails.
Reads never acknowledge messages. Tool errors expose a fixed hub error code,
HTTP status, and `permanent`/`transient` classification without exception text.
Older hubs that accept a send but omit its requested `reply_to` produce
`reply_to_ignored: true`. Unsupported directory reads report HTTP 400.

`fleet_inbox` defaults to unread messages. Its optional `wait_secs` is bounded
at 120 seconds and polls with delays of 1, 2, 4, then at most 5 seconds. Network
reads have a minimum timeout of 2 seconds, capped at the client's flush timeout.
A final read can therefore outlast the wait deadline by that timeout. A failed
poll after the first read returns the last good page. When polling finds unread
mail, it returns the page containing that arrival even for a full-history read.
`fleet_inbox` and `fleet_sent` accept an optional decimal-string `after` cursor
(default `"0"`) and return `next_cursor` as a decimal string. Both return up to
one hub page, with `limit` from 1 to 100. Pass `next_cursor` as `after` to read
subsequent pages, or acknowledge consumed inbox messages to advance unread mail.

For a body-free inbox total, call `fleet_inbox` with `count_only: true` or run
`brigade fleet msg count --json` (with `--token-file` for an agent credential).
Both use `POST /messages` with `{"action":"count"}` and return only
`{"unread_count":N}`. The total covers the authenticated principal's entire
unacknowledged, unexpired inbox. Count mode ignores the MCP wait, cursor,
unacked-only and page options. It neither acknowledges mail nor writes retention
cleanup. Older hubs without this action return HTTP 400, without a fallback
that fetches message bodies. Existing inbox and sent calls keep their shapes.

Sender acknowledgement views remain scoped to the authenticated sender.
`fleet_sent` and `brigade fleet msg sent --id MESSAGE_ID --json` return
`acked_at` and `acked_by`, initially null. Send grants still govern new agent
messages and replies. Removing a grant does not hide the sender's own history.
A revoked token receives HTTP 401 at authentication. A principal revoked after
authentication receives HTTP 403 from the active-principal query guard.

Message results combine subject, reference, and body inside one untrusted data
block in `body`. The original `subject` and `ref` fields are omitted. Scans cover
all three fields and report counts without repeating peer-authored excerpts.
Fences use random per-call markers and neutralize embedded fence prefixes.

Node credentials can deliberately list all active agent principals and their
labels through `fleet_who`. Agent credentials see only recipients allowed by
their grants. Session filtering by permitted nodes happens before the row limit.
Node `fleet_claims` results match `GET /claims`, including active Grok Bot lease
rows. Agent claim results include only granted owner nodes and omit `job`,
`session`, and `owner_conductor`. Directory and claim reads use a read transaction
that checks the caller's active status within the same snapshot.
