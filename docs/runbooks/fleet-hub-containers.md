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
`python:3.12.7-slim-bookworm` (Debian 12). This immutable ref includes the
loopback proxy trust option; released 0.27.0 does not. The CI acceptance check
prints the OS, architecture, Python version, and linked SQLite version for
each built `linux/amd64` and `linux/arm64` image. For a local image, record its
immutable manifest digest before deployment:

The Linux/amd64 smoke run on 2026-10-13 reported Debian GNU/Linux 12
(bookworm), x86_64, Python 3.12.7, and linked SQLite 3.40.1. The Linux/arm64
values are recorded by the matching CI matrix run.

```bash
podman image inspect --format '{{.Digest}}' localhost/brigade-fleet-hub:0.28.0-e6009f6
podman run --rm --entrypoint python localhost/brigade-fleet-hub:0.28.0-e6009f6 \
  -c 'import platform,sqlite3,sys; print(platform.platform()); print(sys.version); print(sqlite3.sqlite_version)'
```

Deploy the recorded `localhost/brigade-fleet-hub@sha256:<REVIEWED_DIGEST>`,
not a mutable tag. This repository does not publish the image to a registry.

## Docker Compose

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
docker compose up --build -d
curl --fail http://127.0.0.1:3774/health
```

Run the local image, token, persistence, backup/restore, and HTTPS proxy
acceptance check from the repository root with
`./.venv/bin/python tests/fleet_hub_container_smoke.py`. It requires a running
Docker daemon and builds the pinned package version before exercising the
container.

The named volume contains the database and its WAL/SHM sidecars. Keep it on a
local filesystem, not NFS, SMB, or a cloud-synced directory. Ordinary updates
are `docker compose build --pull && docker compose up -d`; never add `--volumes`
to `down`. This recipe intentionally runs one replica and has no shared-network
database support.

## Rootless Podman Quadlet

Build the pinned image locally, create the Podman secret over standard input, copy both Quadlet files to
`~/.config/containers/systemd/`, then reload the user manager:

```bash
podman build -f ops/fleet-hub/Containerfile \
  --build-arg BRIGADE_REF=e6009f6969dc6e45db2ab93790aa3e407c791409 \
  -t localhost/brigade-fleet-hub:0.28.0-e6009f6 .
umask 077
python -c 'import secrets; print(secrets.token_urlsafe(32))' | \
  podman secret create brigade-fleet-admin-token -
install -d ~/.config/containers/systemd
install -m 0644 ops/fleet-hub/quadlet/brigade-fleet.{container,volume} \
  ~/.config/containers/systemd/
systemctl --user daemon-reload
systemctl --user start brigade-fleet.service
curl --fail http://127.0.0.1:3774/health
```

Rootless UID mapping must make volume UID 10001 writable; the volume Quadlet
declares owner UID and group GID. Confirm it on the target Podman version before
use. Quadlet generates this service, so start it with `systemctl --user start`;
do not enable the generated unit directly. Its `[Install]` section supplies the
generator's startup wiring.
Podman Compose is not Podman's Compose implementation: it invokes an external
provider such as `docker-compose` or `podman-compose`. On Docker/Podman Desktop,
containers and named volumes live inside its Linux VM, so backup and restore
must address that VM's storage rather than assuming a native host path.

## Security and operations boundary

Both recipes publish only on host loopback and mount the token as a protected
file. Plain HTTP is therefore local-only. The smoke check proves HTTPS
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
retention, and retain the old image digest plus backup. Rollback means restoring
that schema-compatible backup with the old digest and can lose every write made
after the backup. Never downgrade a live database in place.
