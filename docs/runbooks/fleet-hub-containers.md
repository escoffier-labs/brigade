# Fleet Hub container preview

This is the first independently useful slice of issue #1635: a versioned,
non-root Fleet Hub image plus minimal Docker Compose and Linux Podman Quadlet
recipes. The recipes are **preview**, not a claim of tested production support.
Automated checks currently validate their security and persistence invariants;
multi-architecture builds, replacement/persistence acceptance, backup/restore,
and the HTTPS proxy topology remain release work.

## Recorded baseline

The recipe pins `brigade-cli==0.27.0` on `python:3.12.7-slim-bookworm`
(Debian 12). Before approving an image, record its immutable manifest digest and
the outputs of the following from each built `linux/amd64` and `linux/arm64`
image; those architectures are targets, not yet certified by this runbook:

```bash
podman image inspect --format '{{.Digest}}' ghcr.io/escoffier-labs/brigade-fleet-hub:0.27.0
podman run --rm --entrypoint python ghcr.io/escoffier-labs/brigade-fleet-hub:0.27.0 \
  -c 'import platform,sqlite3,sys; print(platform.platform()); print(sys.version); print(sqlite3.sqlite_version)'
```

Deploy the recorded `name@sha256:<REVIEWED_DIGEST>`, not a mutable tag.

## Docker Compose

Create a random token without putting it in command arguments, then protect the
file. The example path is ignored by Git.

```bash
cd ops/fleet-hub
install -d -m 0700 secrets
umask 077
python -c 'import secrets; print(secrets.token_urlsafe(32))' > secrets/admin-token.txt
docker compose up -d
curl --fail http://127.0.0.1:3774/health
```

The named volume contains the database and its WAL/SHM sidecars. Keep it on a
local filesystem, not NFS, SMB, or a cloud-synced directory. Ordinary updates
are `docker compose pull && docker compose up -d`; never add `--volumes` to
`down`. This recipe intentionally runs one replica and has no shared-network
database support.

## Rootless Podman Quadlet

Create the Podman secret over standard input, copy both Quadlet files to
`~/.config/containers/systemd/`, then reload the user manager:

```bash
umask 077
python -c 'import secrets; print(secrets.token_urlsafe(32))' | \
  podman secret create brigade-fleet-admin-token -
install -d ~/.config/containers/systemd
install -m 0644 ops/fleet-hub/quadlet/brigade-fleet.{container,volume} \
  ~/.config/containers/systemd/
systemctl --user daemon-reload
systemctl --user enable --now brigade-fleet.service
curl --fail http://127.0.0.1:3774/health
```

Rootless UID mapping must make volume UID 10001 writable; the volume Quadlet
declares that ownership. Confirm it on the target Podman version before use.
Podman Compose is not Podman's Compose implementation: it invokes an external
provider such as `docker-compose` or `podman-compose`. On Docker/Podman Desktop,
containers and named volumes live inside its Linux VM, so backup and restore
must address that VM's storage rather than assuming a native host path.

## Security and operations boundary

Both recipes publish only on host loopback and mount the token as a protected
file. Plain HTTP is therefore local-only. Do not enable `--trust-forwarded-proto`
in these recipes. Secure-cookie enrollment requires an HTTPS reverse proxy that
overwrites forwarding headers **and is the hub's immediate loopback peer**.
Host loopback publication does not make a separate container's peer loopback,
so a conventional proxy sidecar is unsafe and unsupported. A real proxy
topology and its enrollment test remain required; do not weaken the Hub check.

Before an upgrade, stop writes and use SQLite's online backup API (for example,
Python `sqlite3.Connection.backup`) to a separate local path, then test a clean
restore with the candidate image. Upgrade only one instance, verify health,
node authentication, event ingestion, dashboard enrollment, and restart
retention, and retain the old image digest plus backup. Rollback means restoring
that schema-compatible backup with the old digest and can lose every write made
after the backup. Never downgrade a live database in place.
