# Fleet Hub container preview

This is a versioned, non-root Fleet Hub image plus minimal Docker Compose and
Linux Podman Quadlet recipes. The recipes are **preview**, not a claim of tested
production support. CI builds and exercises the image on Linux/amd64 and
Linux/arm64, including health, node authentication, event ingestion, crash
retention, SQLite online backup and restore, and HTTPS enrollment through a
loopback proxy sharing the Hub's network namespace. Podman/Quadlet remains a
recipe proposal pending operator acceptance.

## Recorded baseline

The image installs Brigade from reviewed source commit
`e6009f6969dc6e45db2ab93790aa3e407c791409` (CLI version 0.28.0) on
`python:3.12.14-slim-bookworm` (Debian 12). This immutable ref includes the
loopback proxy trust option; released 0.27.0 does not. The CI acceptance check
prints the OS, architecture, Python, OpenSSL, and linked SQLite versions for
each built `linux/amd64` and `linux/arm64` image. It requires OpenSSL 3.0.20 or
newer. Debian's DSA-6015-1 marks Bookworm's 3.0.20 package fixed; the pinned
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
python -c 'import re,sys; m=re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", sys.argv[1]); assert m and tuple(map(int, m.groups())) >= (28, 0, 0), f"Docker Engine 28.0.0 or newer is required, found {sys.argv[1]}"' "$engine_version"
```

Create a random token without putting it in command arguments, then protect the
file. Compose file-backed secrets are read-only bind mounts, so the container
must be able to read the source file's permissions. Keep the host directory
private and make the file readable by the container's non-root UID; the file
remains inaccessible to other host users because they cannot traverse the
directory. The example path is ignored by Git. See [Docker Compose secrets](https://docs.docker.com/compose/how-tos/use-secrets/).

```bash
cd ops/fleet-hub
install -d -m 0700 secrets
umask 077
python -c 'import secrets; print(secrets.token_urlsafe(32))' > secrets/admin-token.txt
chmod 0444 secrets/admin-token.txt
```

Before starting the service, record the built image's immutable local image ID
in an ignored Compose override. The base Compose file uses a version tag for
the build; use the recorded ID for every start or recreation:

```bash
docker compose build --pull hub
image_id="$(docker image inspect --format '{{.Id}}' localhost/brigade-fleet-hub:0.28.0-e6009f6)"
case "$image_id" in sha256:*) ;; *) echo "Expected a sha256 image ID" >&2; exit 1 ;; esac
cat > secrets/compose.image-id.yaml <<EOF
services:
  hub:
    image: ${image_id}
EOF
docker compose -f compose.yaml -f secrets/compose.image-id.yaml up -d --no-build
curl --fail http://127.0.0.1:3774/health
```

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

Then rebuild the versioned tag, inspect it for the new ID, replace the
override's `image` value, and run the same Compose command. To roll back, load
the saved image and verify that its ID matches before restoring the old ID in
the override:

```bash
# Set these from the recorded image archive name and ID.
archive="secrets/brigade-fleet-hub-REPLACE_WITH_RECORDED_IMAGE_ID.tar"
previous_id="sha256:REPLACE_WITH_RECORDED_IMAGE_ID"
docker image load --input "$archive"
restored_id="$(docker image inspect --format '{{.Id}}' "$previous_id")"
test "$restored_id" = "$previous_id"
cat > secrets/compose.image-id.yaml <<EOF
services:
  hub:
    image: ${previous_id}
EOF
docker compose -f compose.yaml -f secrets/compose.image-id.yaml up -d --no-build
```

The archive, override, and token are ignored with the token directory; retain
the archive and ID alongside the database backup.

Run the local image, token, persistence, backup/restore, and HTTPS proxy
acceptance check from the repository root with
`./.venv/bin/python tests/fleet_hub_container_smoke.py`. It requires a running
Docker daemon and builds the pinned package version before exercising the
container.

The named volume contains the database and its WAL/SHM sidecars. Keep it on a
local filesystem, not NFS, SMB, or a cloud-synced directory. For ordinary
updates, rebuild the versioned tag, record its new local image ID in the ignored
`secrets/compose.image-id.yaml`, then run `docker compose -f compose.yaml -f
secrets/compose.image-id.yaml up -d --no-build`. Never add `--volumes` to
`down`. This recipe intentionally runs one replica and has no shared-network
database support.

## Rootless Podman Quadlet

Build the pinned image locally and record its digest. Keep an operator copy of
the admin token in a private file, import that file as a Podman secret, render
the Quadlet with the digest, and copy the volume unit to
`~/.config/containers/systemd/`:

```bash
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
else
  python -c 'import secrets; print(secrets.token_urlsafe(32))' > "$token_file"
  chmod 0600 "$token_file"
  podman secret create brigade-fleet-admin-token "$token_file"
fi
install -d -m 0700 ~/.brigade
cat > ~/.brigade/fleet.toml <<EOF
[fleet]
hub_url = "http://127.0.0.1:3774"
token_file = "$token_file"
EOF
chmod 0600 ~/.brigade/fleet.toml
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
curl --fail http://127.0.0.1:3774/health
```

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

Build and deploy the candidate image using the steps above, recording its new
digest and replacing the digest in the installed unit. To roll back, load the
archive, verify the restored image ID, render the old digest into the unit, and
start the generated service again:

```bash
# Set these from the saved archive name and recorded previous digest.
archive="$HOME/.local/share/brigade-fleet-hub/archives/brigade-fleet-hub-REPLACE_WITH_RECORDED_DIGEST.tar"
previous_digest="sha256:REPLACE_WITH_RECORDED_DIGEST"
previous_ref="localhost/brigade-fleet-hub@${previous_digest}"
image_id_file="${archive%.tar}.image-id"
previous_id="$(cat "$image_id_file")"
test -n "$previous_id"
podman image load --input "$archive"
restored_id="$(podman image inspect --format '{{.Id}}' "$previous_ref")"
test "$restored_id" = "$previous_id"
sed "s|REPLACE_WITH_RECORDED_DIGEST|${previous_digest}|" \
  ops/fleet-hub/quadlet/brigade-fleet.container \
  > "$HOME/.config/containers/systemd/brigade-fleet.container"
chmod 0644 "$HOME/.config/containers/systemd/brigade-fleet.container"
systemctl --user daemon-reload
systemctl --user restart brigade-fleet.service
```

Rootless UID mapping must make volume UID 10001 writable; the volume Quadlet
declares owner UID and group GID. Confirm it on the target Podman version before
use. Quadlet generates this service, so start or update it with
`systemctl --user restart`; restart starts an inactive service and reloads an
active one. Do not enable the generated unit directly. Its `[Install]` section
supplies the generator's startup wiring.
Podman Compose is not Podman's Compose implementation: it invokes an external
provider such as `docker-compose` or `podman-compose`. On Docker/Podman Desktop,
containers and named volumes live inside its Linux VM, so backup and restore
must address that VM's storage rather than assuming a native host path.

## Security and operations boundary

Both recipes publish only on host loopback and mount the token as a protected
file. Docker Compose requires Engine 28.0.0 or newer for localhost publication
to remain inaccessible from same-L2 hosts. Plain HTTP is therefore local-only
under that requirement. The smoke check proves HTTPS
enrollment and Secure cookies with a TLS proxy sharing the Hub's network
namespace. The Hub binds to numeric loopback with `--trust-forwarded-proto`,
and the proxy overwrites `X-Forwarded-Proto`; this makes its Hub-side peer
loopback. A proxy on a separate container network is unsupported because host
loopback publication does not make its peer loopback. Do not weaken the Hub
check to make that topology work.

Before an upgrade, stop writes and use SQLite's online backup API (for example,
Python `sqlite3.Connection.backup`) to a separate local path, then test a clean
restore with the candidate image. Upgrade only one instance, verify health,
node authentication, event ingestion, dashboard enrollment, and restart
retention, and retain the database backup and image archive. Compose rollback
restores the archived image ID; Quadlet rollback uses its saved image digest.
Restoring the schema-compatible database backup can lose every write made
after it. Never downgrade a live database in place.
