# Fleet Hub container preview

This is a versioned, non-root Fleet Hub image plus minimal Docker Compose and
Linux Podman Quadlet recipes. The recipes are **preview**, not a claim of tested
production support. CI builds and exercises the image on Linux/amd64 and
Linux/arm64, including health, node authentication, event ingestion, crash
retention, SQLite online backup and restore, and HTTPS enrollment through a
loopback proxy sharing the Hub's network namespace. The Compose and Quadlet
files are not executed in CI. Podman/Quadlet remains a recipe proposal pending
operator acceptance.

## Recorded baseline

The image installs Brigade from reviewed source commit
`e6009f6969dc6e45db2ab93790aa3e407c791409` (CLI version 0.28.0) on
`python:3.12.14-slim-bookworm` (Debian 12). This immutable ref includes the
loopback proxy trust option. Released 0.27.0 does not include it. The CI acceptance check
prints the OS, architecture, Python, OpenSSL, and linked SQLite versions for
each built `linux/amd64` and `linux/arm64` image. It requires OpenSSL 3.0.20 or
newer. Debian's DSA-6015-1 marks Bookworm's 3.0.20 package fixed. The pinned
image reports OpenSSL 3.0.20. For a local image, record its immutable manifest
digest before deployment:

The Linux/amd64 smoke run on 2026-10-13 reported Debian GNU/Linux 12
(bookworm), x86_64, Python 3.12.14, OpenSSL 3.0.20, and linked SQLite 3.40.1.
The Linux/arm64 values are printed by the matching CI matrix run.

```bash
podman image inspect --format '{{.Digest}}' localhost/brigade-fleet-hub:0.28.0-e6009f6
podman run --rm --entrypoint python localhost/brigade-fleet-hub:0.28.0-e6009f6 \
  -c 'import platform,sqlite3,sys; print(platform.platform()); print(sys.version); print(sqlite3.sqlite_version)'
```

Deploy the recorded `localhost/brigade-fleet-hub@sha256:<REVIEWED_DIGEST>`,
not a mutable tag. This repository does not publish the image to a registry.

## Docker Compose

Docker Engine 28.0.0 or newer is required for the localhost-only port
publication below. Older releases can expose localhost-published ports to
hosts on the same layer-2 network. Check the server version before deployment:

```bash
engine_version="$(docker version --format '{{.Server.Version}}')"
python3 -c 'import re,sys; m=re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", sys.argv[1]); assert m and tuple(map(int, m.groups())) >= (28, 0, 0), f"Docker Engine 28.0.0 or newer is required, found {sys.argv[1]}"' "$engine_version"
```

Create a random token without putting it in command arguments, then protect the
file. Compose file-backed secrets are read-only bind mounts, so the container
must be able to read the source file's permissions. Keep the host directory
private and make the file readable by the container's non-root UID. The file
remains inaccessible to other host users because they cannot traverse the
directory. The example path is ignored by Git. Running `git clean -xdf` deletes
`ops/fleet-hub/secrets/`, including tokens, image archives, and database backups.
Keep a protected copy outside the checkout before cleaning it. See [Docker Compose secrets](https://docs.docker.com/compose/how-tos/use-secrets/).

```bash
cd ops/fleet-hub
install -d -m 0700 secrets
umask 077
generated_token="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')" || exit 1
test -n "$generated_token" || { echo "Token generation returned an empty value" >&2; exit 1; }
printf '%s\n' "$generated_token" > secrets/admin-token.txt
unset generated_token
chmod 0444 secrets/admin-token.txt
```

Before starting the service, record the built image's immutable local image ID
in an ignored Compose override. The base Compose file uses a version tag for
the build. Use the recorded ID for every start or recreation:

```bash
docker compose build --pull hub
image_id="$(docker image inspect --format '{{.Id}}' localhost/brigade-fleet-hub:0.28.0-e6009f6)"
case "$image_id" in sha256:*) ;; *) echo "Expected a sha256 image ID" >&2; exit 1 ;; esac
cat > secrets/compose.image-id.yaml <<EOF
services:
  hub:
    image: ${image_id}
EOF
docker compose -f compose.yaml -f secrets/compose.image-id.yaml up -d --no-build \
  --wait --wait-timeout 120
curl --fail http://127.0.0.1:3774/health
```

The wait options require a Compose CLI that supports
[`up --wait`](https://docs.docker.com/reference/cli/docker/compose/up/).
Compose waits for the health check before returning. Then `curl` verifies the
local endpoint.

The health check reports an unhealthy container, but Docker restart policies
apply only when a container terminates. An unhealthy Hub process may stay
running. Check its logs, then restart the pinned service and verify health:

```bash
docker compose -f compose.yaml -f secrets/compose.image-id.yaml logs --tail=100 hub
docker compose -f compose.yaml -f secrets/compose.image-id.yaml restart hub
deadline=$((SECONDS + 120))
while true; do
  remaining=$((deadline - SECONDS))
  if [ "$remaining" -le 0 ]; then
    echo "Fleet Hub did not recover within 120 seconds after restart" >&2
    exit 1
  fi
  if curl --fail --connect-timeout 2 --max-time "$remaining" http://127.0.0.1:3774/health; then
    break
  fi
  remaining=$((deadline - SECONDS))
  if [ "$remaining" -gt 0 ]; then
    sleep "$((remaining < 2 ? remaining : 2))"
  fi
done
```

For an upgrade, follow the backup and stop sequence below before rebuilding
or starting a candidate image. The preceding start command is for the initial
deployment only.

The override pins the local Docker image configuration digest and is ignored
with the token directory. Before rebuilding the versioned tag for an update,
save the current image locally. This preserves its bytes if the tag is
retargeted or Docker later prunes unused images:

```bash
previous_id="$(sed -n 's/^[[:space:]]*image: //p' secrets/compose.image-id.yaml)"
case "$previous_id" in sha256:*) ;; *) echo "Expected a sha256 image ID" >&2; exit 1 ;; esac
archive="secrets/brigade-fleet-hub-${previous_id#sha256:}.tar"
docker image save --output "$archive" "$previous_id"
chmod 0600 "$archive"
```

To roll back, load the saved image and verify that its ID matches before
restoring the database with that image. The override changes only after the
restore succeeds:

```bash
set -euo pipefail
# Set these from the recorded image archive name and ID.
archive="secrets/brigade-fleet-hub-REPLACE_WITH_RECORDED_IMAGE_ID.tar"
previous_id="sha256:REPLACE_WITH_RECORDED_IMAGE_ID"
backup_path="secrets/fleet-hub-pre-upgrade-REPLACE_WITH_TIMESTAMP.db"
docker image load --input "$archive"
restored_id="$(docker image inspect --format '{{.Id}}' "$previous_id")"
test "$restored_id" = "$previous_id"
docker compose -f compose.yaml -f secrets/compose.image-id.yaml stop hub
docker run --rm --pull never --user 0 \
  --volumes-from "$(docker compose -f compose.yaml -f secrets/compose.image-id.yaml ps -aq hub)" \
  --volume "$PWD/$backup_path:/restore/fleet-hub.db:ro" \
  --entrypoint python "$previous_id" -c 'import os,shutil,sqlite3,tempfile
database="/var/lib/brigade/fleet-hub.db"
source="/restore/fleet-hub.db"
if not os.path.isfile(source) or os.path.getsize(source) == 0: raise SystemExit("Backup must be a nonempty regular file")
fd, staged = tempfile.mkstemp(prefix=".fleet-hub-restore-", dir=os.path.dirname(database))
os.close(fd)
try:
 shutil.copyfile(source, staged)
 source_connection=sqlite3.connect(staged)
 target_connection=None
 try:
  result=source_connection.execute("PRAGMA integrity_check").fetchone()[0]
  if result != "ok": raise RuntimeError(result)
  target_connection=sqlite3.connect(database)
  source_connection.backup(target_connection)
 finally:
  if target_connection is not None: target_connection.close()
  source_connection.close()
 os.chown(database, 10001, 10001)
finally:
 if os.path.exists(staged): os.remove(staged)'
cat > secrets/compose.image-id.yaml <<EOF
services:
  hub:
    image: ${previous_id}
EOF
docker compose -f compose.yaml -f secrets/compose.image-id.yaml up -d --no-build \
  --wait --wait-timeout 120
```

The restore validates a staged copy, then uses SQLite's online backup API to
write into the stopped Hub database. If that copy fails before completion,
SQLite rolls back the destination write transaction and manages the database's
WAL/SHM sidecars with the live database. See [SQLite's backup API
failure behavior](https://sqlite.org/c3ref/backup_finish.html).

The archive, override, and token are ignored with the token directory. Retain
the archive and ID alongside the database backup.

Run the local image, token, persistence, backup/restore, and HTTPS proxy
acceptance check from the repository root with
`./.venv/bin/python tests/fleet_hub_container_smoke.py`. It requires a running
Docker daemon and builds the pinned package version before exercising the
container.

Before an upgrade, create a SQLite online backup while the current Hub is
running. Keep it with the image archive and preserve it mode 0600:

```bash
set -euo pipefail
backup_path="secrets/fleet-hub-pre-upgrade-$(date -u +%Y%m%dT%H%M%SZ).db"
docker compose -f compose.yaml -f secrets/compose.image-id.yaml exec -T --user 10001 hub \
  python -c 'import sqlite3; source=sqlite3.connect("/var/lib/brigade/fleet-hub.db"); target=sqlite3.connect("/var/lib/brigade/fleet-hub-pre-upgrade.db"); source.backup(target); target.close(); source.close()'
docker cp "$(docker compose -f compose.yaml -f secrets/compose.image-id.yaml ps -q hub):/var/lib/brigade/fleet-hub-pre-upgrade.db" "$backup_path"
chmod 0600 "$backup_path"
docker compose -f compose.yaml -f secrets/compose.image-id.yaml exec -T hub \
  rm /var/lib/brigade/fleet-hub-pre-upgrade.db
docker compose -f compose.yaml -f secrets/compose.image-id.yaml stop hub
```

After the backup succeeds and the service stops, rebuild the candidate, inspect
it for its new ID, replace the override's `image` value, then start it with
`docker compose -f compose.yaml -f secrets/compose.image-id.yaml up -d --no-build
--wait --wait-timeout 120`. Starting it may migrate the database. Compose waits
for the configured health check before returning.

The named volume contains the database and its WAL/SHM sidecars. Keep it on a
local filesystem, not NFS, SMB, or a cloud-synced directory. Ordinary updates
require the same sequence above: archive the deployed image, create the online
database backup, and stop the Hub. Only after those steps succeed,
rebuild the versioned tag, record its new local image ID in the ignored
`secrets/compose.image-id.yaml`, then run `docker compose -f compose.yaml -f
secrets/compose.image-id.yaml up -d --no-build --wait --wait-timeout 120`. Never add `--volumes` to
`down`. This recipe intentionally runs one replica and has no shared-network
database support.

## Rootless Podman Quadlet

Use a Podman release whose Quadlet generator supports `HealthOnFailure=kill`
and [`Notify=healthy`](https://docs.podman.io/en/stable/markdown/podman-systemd.unit.5.html#notify-defaults-to-false).
The unit waits for its health check before systemd reports startup success,
with a 120-second startup timeout. Both initial deployment and rollback use
this readiness gate.

Hub setup leaves the operator account's Fleet client configuration untouched.
An admin-only client config sends the admin token for events and claims,
which this Hub rejects with HTTP 403 because admin writes are disabled by
default. Enroll the local client after the Hub starts, as described below.

Build the pinned image locally and record its digest. Keep an operator copy of
the admin token in a private file, import that file as a Podman secret, render
the Quadlet with the digest, and copy the volume unit to
`~/.config/containers/systemd/`:

```bash
set -euo pipefail
podman build -f ops/fleet-hub/Containerfile \
  --build-arg BRIGADE_REF=e6009f6969dc6e45db2ab93790aa3e407c791409 \
  -t localhost/brigade-fleet-hub:0.28.0-e6009f6 .
image_digest="$(podman image inspect --format '{{.Digest}}' localhost/brigade-fleet-hub:0.28.0-e6009f6)"
case "$image_digest" in sha256:*) ;; *) echo "Expected a sha256 image digest" >&2; exit 1 ;; esac
umask 077
token_dir="$HOME/.local/share/brigade-fleet-hub"
install -d -m 0700 "$token_dir"
token_file="$token_dir/admin-token"
if podman secret inspect brigade-fleet-admin-token >/dev/null 2>&1; then
  test -s "$token_file" || {
    echo "Existing Podman secret needs its saved operator token file at $token_file" >&2
    exit 1
  }
elif [ -e "$token_file" ] || [ -L "$token_file" ]; then
  echo "Token file exists without the Podman secret; inspect and reconcile this partial state before continuing" >&2
  exit 1
else
  generated_token="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')" || exit 1
  test -n "$generated_token" || { echo "Token generation returned an empty value" >&2; exit 1; }
  printf '%s\n' "$generated_token" > "$token_file"
  unset generated_token
  chmod 0600 "$token_file"
  podman secret create brigade-fleet-admin-token "$token_file" || {
    echo "Could not create the Podman secret; no unit was installed" >&2
    exit 1
  }
fi
install -d ~/.config/containers/systemd
sed "s|REPLACE_WITH_RECORDED_DIGEST|${image_digest}|" \
  ops/fleet-hub/quadlet/brigade-fleet.container \
  > ~/.config/containers/systemd/brigade-fleet.container
chmod 0644 ~/.config/containers/systemd/brigade-fleet.container
install -m 0644 ops/fleet-hub/quadlet/brigade-fleet.volume \
  ~/.config/containers/systemd/
if [ "$(loginctl show-user "$USER" --property=Linger --value)" != yes ]; then
  loginctl enable-linger "$USER" || {
    echo "Could not enable lingering; ask the system administrator to enable it for $USER" >&2
    exit 1
  }
fi
test "$(loginctl show-user "$USER" --property=Linger --value)" = yes || {
  echo "Lingering is required to keep the rootless Hub running after logout" >&2
  exit 1
}
systemctl --user daemon-reload
systemctl --user restart brigade-fleet.service
curl --fail --max-time 5 http://127.0.0.1:3774/health
```

If this account also runs Brigade work and is not already enrolled, enroll its
machine identity using the saved admin token for this command only:

```bash
node_id="$(brigade node --machine --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["node_id"])')"
BRIGADE_FLEET_HUB_URL=http://127.0.0.1:3774 \
  BRIGADE_FLEET_TOKEN="$(cat "$HOME/.local/share/brigade-fleet-hub/admin-token")" \
  brigade fleet nodes add "$node_id"
```

Save the returned node token in a private file before enabling the client.
Follow [client enrollment](fleet-hub-proxmox.md#7-enroll-and-configure-each-brigade-client)
to install it and configure `node_token_file`, using `http://127.0.0.1:3774`
for this local Hub. To retain admin CLI access, also set
`token_file = "~/.local/share/brigade-fleet-hub/admin-token"` in that final
`[fleet]` section. Preserve existing client settings when merging the config.

Before rebuilding the versioned tag, archive the image referenced by the
installed unit. Podman's image archive preserves the image layers, history,
and tags so loading it restores the original local image:

```bash
unit="$HOME/.config/containers/systemd/brigade-fleet.container"
previous_ref="$(sed -n 's/^Image=//p' "$unit")"
previous_digest="${previous_ref##*@}"
case "$previous_digest" in sha256:*) ;; *) echo "Expected a sha256 image digest" >&2; exit 1 ;; esac
previous_id="$(podman image inspect --format '{{.Id}}' "$previous_ref")"
archive_dir="$HOME/.local/share/brigade-fleet-hub/archives"
install -d -m 0700 "$archive_dir"
archive="$archive_dir/brigade-fleet-hub-${previous_digest#sha256:}.tar"
podman image save --format oci-archive --output "$archive" "$previous_ref"
image_id_file="${archive%.tar}.image-id"
printf '%s\n' "$previous_id" > "$image_id_file"
chmod 0600 "$archive" "$image_id_file"
```

Before deploying a candidate that may migrate the database, create a SQLite
online backup while the current Hub is running. This is the restore point for
the archived image, because that image may reject the candidate's newer schema.
Keep this file with the image archive and preserve it mode 0600:

```bash
set -euo pipefail
backup_dir="$HOME/.local/share/brigade-fleet-hub/backups"
install -d -m 0700 "$backup_dir"
backup_path="$backup_dir/fleet-hub-pre-upgrade-$(date -u +%Y%m%dT%H%M%SZ).db"
podman exec brigade-fleet-hub python -c 'import sqlite3; source=sqlite3.connect("/var/lib/brigade/fleet-hub.db"); target=sqlite3.connect("/var/lib/brigade/fleet-hub-pre-upgrade.db"); source.backup(target); target.close(); source.close()'
podman cp brigade-fleet-hub:/var/lib/brigade/fleet-hub-pre-upgrade.db "$backup_path"
chmod 0600 "$backup_path"
podman exec brigade-fleet-hub rm /var/lib/brigade/fleet-hub-pre-upgrade.db
systemctl --user stop brigade-fleet.service
```

After the backup succeeds and the user service stops, deploy the candidate
image. Starting it may migrate the persistent database.

Build and deploy the candidate image using the steps above, recording its new
digest and replacing the digest in the installed unit. To roll back, load the
archive, verify the restored image ID, render the old digest into the unit, and
start the generated service again:

```bash
set -euo pipefail
# Set these from the saved archive name, digest, and pre-upgrade database backup.
archive="$HOME/.local/share/brigade-fleet-hub/archives/brigade-fleet-hub-REPLACE_WITH_RECORDED_DIGEST.tar"
previous_digest="sha256:REPLACE_WITH_RECORDED_DIGEST"
backup_path="$HOME/.local/share/brigade-fleet-hub/backups/fleet-hub-pre-upgrade-REPLACE_WITH_TIMESTAMP.db"
previous_ref="localhost/brigade-fleet-hub@${previous_digest}"
image_id_file="${archive%.tar}.image-id"
previous_id="$(cat "$image_id_file")"
test -n "$previous_id"
podman image load --input "$archive"
restored_id="$(podman image inspect --format '{{.Id}}' "$previous_ref")"
test "$restored_id" = "$previous_id"
systemctl --user stop brigade-fleet.service
podman run --rm --user 0:0 \
  --volume brigade-fleet-state:/var/lib/brigade \
  --volume "$backup_path:/restore/fleet-hub.db:ro,Z" \
  --entrypoint python "$previous_ref" -c 'import os,shutil,sqlite3,tempfile
database="/var/lib/brigade/fleet-hub.db"
source="/restore/fleet-hub.db"
if not os.path.isfile(source) or os.path.getsize(source) == 0: raise SystemExit("Backup must be a nonempty regular file")
fd, staged = tempfile.mkstemp(prefix=".fleet-hub-restore-", dir=os.path.dirname(database))
os.close(fd)
try:
 shutil.copyfile(source, staged)
 source_connection=sqlite3.connect(staged)
 target_connection=None
 try:
  result=source_connection.execute("PRAGMA integrity_check").fetchone()[0]
  if result != "ok": raise RuntimeError(result)
  target_connection=sqlite3.connect(database)
  source_connection.backup(target_connection)
 finally:
  if target_connection is not None: target_connection.close()
  source_connection.close()
 os.chown(database, 10001, 10001)
finally:
 if os.path.exists(staged): os.remove(staged)'
sed "s|REPLACE_WITH_RECORDED_DIGEST|${previous_digest}|" \
  ops/fleet-hub/quadlet/brigade-fleet.container \
  > "$HOME/.config/containers/systemd/brigade-fleet.container"
chmod 0644 "$HOME/.config/containers/systemd/brigade-fleet.container"
systemctl --user daemon-reload
systemctl --user restart brigade-fleet.service
curl --fail --max-time 5 http://127.0.0.1:3774/health
```

Rootless UID mapping must make volume UID 10001 writable. The volume Quadlet
declares owner UID and group GID. Confirm it on the target Podman version before
use. Quadlet generates this service, so start or update it with
`systemctl --user restart`. This starts an inactive service and reloads an
active one. Do not enable the generated unit directly. Its `[Install]` section
supplies the generator's startup wiring.
Podman Compose is not Podman's Compose implementation: it invokes an external
provider such as `docker-compose` or `podman-compose`. On Docker/Podman Desktop,
containers and named volumes live inside its Linux VM, so backup and restore
must address that VM's storage rather than assuming a native host path.

## Security and operations boundary

The image binds to `0.0.0.0` inside the container. A bare
`docker run -p 3774:3774` publishes plain HTTP on every host interface. Use the
explicit loopback publication in these recipes.

Both recipes publish only on host loopback and mount the token as a protected
file. Docker Compose requires Engine 28.0.0 or newer for localhost publication
to remain inaccessible from same-L2 hosts. Plain HTTP is therefore local-only
under that requirement. The smoke check proves HTTPS
enrollment and Secure cookies with a TLS proxy sharing the Hub's network
namespace. The Hub binds to numeric loopback with `--trust-forwarded-proto`,
and the proxy overwrites `X-Forwarded-Proto`. This makes its Hub-side peer
loopback. A proxy on a separate container network is unsupported because host
loopback publication does not make its peer loopback. Do not weaken the Hub
check to make that topology work.

Before an upgrade, stop writes and use SQLite's online backup API (for example,
Python `sqlite3.Connection.backup`) to a separate local path, then test a clean
restore with the candidate image. Upgrade only one instance, verify health,
node authentication, event ingestion, dashboard enrollment, and restart
retention, and retain the database backup and image archive. Compose rollback
restores the archived image ID. Quadlet rollback uses its saved image digest.
Restoring the schema-compatible database backup can lose every write made
after it. Never downgrade a live database in place.
