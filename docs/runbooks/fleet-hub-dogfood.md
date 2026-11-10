# Dogfood hub from the Compose recipe

This runbook stands up the dogfood Fleet Hub on the GPU workstation from the
Compose recipe in `ops/fleet-hub/` (S31 in
[the v2 hub design](../phase-v2-hub-architecture.md#the-dogfood-hub-on-the-gpu-workstation)).
The recipe is the install path the design offers everyday users. The dogfood
hub is not production: no production node points at it, and it holds dogfood
data only. Production stays on the LXC hub host
([Fleet hub on a Proxmox LXC](fleet-hub-proxmox.md)). The hub container runs
the Rust hub, `brigade-hub`, in front of the Python hub, with hub memory
served natively. `hub/entry.py` runs it through
`brigade fleet serve --engine rust` (S10) when the pinned Brigade has that
engine, and through its own S31 wrapper otherwise. The default
`BRIGADE_REF` predates S10, so a default build uses the wrapper until the
pin moves at the next release. Moving a
hub from the Python engine to the Rust one, and back, is
[Rust hub cutover and rollback](fleet-hub-rust-engine.md).

Machines are named by role. Replace every angle-bracket value. Host-specific
values (core numbers, devices, the compose account, the front's name) belong
only in the files setup writes, which Git ignores.

Every host package, unit and setting this runbook needs is a numbered step in
[HOST-ACTIONS.md](../../ops/fleet-hub/HOST-ACTIONS.md). The maintainer approves
each one before anyone runs it. The scripted acceptance checks are in
`ops/fleet-hub/checks/s31_checks.py`.

## Layout

| Container | Network | Role |
|---|---|---|
| `netns` (holder) | compose default network and the internal `embed` network | Owns the network namespace. Runs only a pause process with no health check and an OOM score adjustment of -1000, so nothing restarts it. |
| `hub` | `service:netns` | `hub/entry.py`, which runs `brigade-hub` (through `brigade fleet serve --engine rust` when the pinned Brigade has it, checked against the checksum the image recorded) bound to `127.0.0.1:3774` in managed mode, with `--route-mode memory=native`. `brigade-hub` starts `brigade fleet serve` as its child on an ephemeral loopback port, and the child refuses any request without the key `brigade-hub` generates at each start. Memory lives in `fleet-hub-memory.db` beside `fleet-hub.db` on the state volume. No GPU, no published port. |
| `front` | `service:netns` | The private network's HTTPS front. Terminates HTTPS with the certificate the private network issues for its own name and forwards to `127.0.0.1:3774`, so the hub's peer is loopback, as on the LXC hub host. SSH stays off. |
| `embedder` | `embed` only (internal) | Only under the `gpu` profile. One GPU through a device reservation, its own model volume, no published port. The hub reaches it as `http://embedder:11434`. |
| `backup` | none | One-shot `brigade fleet backup`, started hourly by a timer. |
| `model-pull` | compose default network | One-shot download of the R1 model into the model volume, under the `model-pull` profile. The embedder itself cannot reach a registry. |

No service publishes a port. A process on the host, outside the hub's
namespace and without Docker rights or `sudo`, cannot reach the hub port or
the upstream on any address of that namespace. It reaches the hub through the
front with a token, like any node. It can reach the embedder, whose exposure is
a loaded GPU and BM25 answers while its breaker is open. The embedder holds no
secret.

Why a holder: a container that joins another container's namespace keeps the
namespace it joined. If the owner restarts, the joiner is left with loopback
only. The holder never needs a restart, so a restarted hub rejoins the
namespace the front is still in. If the holder does restart (a daemon restart
or `docker restart`), each joiner notices. The host watchdog timer restarts
the hub after 3 checks, 10 s apart, that find only loopback. The front's
wrapper (`front/front-wrapper.sh`) exits after 3 such checks, or after 3
checks in which the hub port refuses connections. The restart policy then
starts each joiner in the holder's current namespace.

Secrets, in `ops/fleet-hub/secrets/` (mode 0700, ignored by Git):

- `admin-token.txt`: the hub's admin token.
- `findings-key`: the findings and recall-log HMAC key, 32 random bytes,
  mounted read-only and named by `BRIGADE_FLEET_FINDINGS_KEY_FILE`. It is off
  the state volume, so a volume copy alone cannot test guesses.
- `front-authkey`: the private network's auth key for the front's first login.
- `embed-token`: only for the `remote` embedder backend, a copy of the
  token file passed to setup.
- `embed-local-token`: only for the `local` backend. The hub sends a bearer
  token to every embedder endpoint, so setup makes a random one. The
  recipe's embedder ignores it.
- `operator-admin-token.txt`: the operator's copy of the admin token, mode
  0600. No container mounts it.

Each container's file is mode 0444. The containers run as other UIDs and must
read the bind mount, so the 0700 directory protects the files from other host
users.

`brigade memory rescan` and `brigade memory reindex` accept only a mode-0600
`--admin-token-file`, so they refuse the 0444 container copy. Pass the operator
copy instead:

```bash
brigade memory rescan --target . --admin-token-file secrets/operator-admin-token.txt --limit 4 --until-done
```

`bin/hub-setup.py init` and `switch --to hybrid` keep the operator copy equal
to `admin-token.txt`. They create it when it is missing, rewrite it after the
admin token changes and reset its mode to 0600. To add it to an existing
install without rerunning setup, run this from the recipe directory. It
writes a 0600 file inside `secrets/` and renames it over the copy, so the copy
is never a partial file or wider than 0600. It stops at the first failing
command, and its `EXIT` trap removes the temporary file, so a failure leaves no
stray copy of the token:

```bash
(
  set -eu
  umask 077
  operator_tmp="$(mktemp secrets/.operator-admin-token.XXXXXX)"
  trap 'rm -f "$operator_tmp"' EXIT
  cat secrets/admin-token.txt > "$operator_tmp"
  chmod 0600 "$operator_tmp"
  mv -fT "$operator_tmp" secrets/operator-admin-token.txt
  trap - EXIT
)
```

## Prerequisites

Approved and done, from [HOST-ACTIONS.md](../../ops/fleet-hub/HOST-ACTIONS.md):

- Docker uses the systemd cgroup driver (`docker info -f '{{.CgroupDriver}}'`
  prints `systemd`), needed for the reserved slices.
- The reserved slices are installed, before the first `up`, so the containers
  start under them.
- The Docker stop timeout drop-in and `"shutdown-timeout": 45`.
- The GPU container toolkit, only for the `gpu` profile.

The Brigade image must install a reviewed commit that includes
`brigade fleet backup`, `brigade fleet restore`, the recovery floor and
`BRIGADE_FLEET_FINDINGS_KEY_FILE`, which landed with S31, and the Rust hub's
native memory route (#2283). The image builds both the Python package and
`brigade-hub` from that one commit. The default `BRIGADE_REF` in
`compose.yaml` has all of them. To build a newer reviewed commit, pass it,
its source archive digest and the version in its `pyproject.toml` (the image's
version label) to setup:

```bash
ref=<MERGED_COMMIT_SHA>
curl -fsSL "https://github.com/escoffier-labs/brigade/archive/${ref}.tar.gz" | sha256sum
curl -fsSL "https://github.com/escoffier-labs/brigade/archive/${ref}.tar.gz" \
  | tar -xzO "brigade-${ref}/pyproject.toml" | grep -m1 '^version'
```

## Set up

Run setup as the compose account, from the repository checkout:

```bash
cd ops/fleet-hub
bin/hub-setup.py init \
  --brigade-ref <MERGED_COMMIT_SHA> --brigade-sha256 <ARCHIVE_SHA256> \
  --brigade-version <VERSION_IN_ITS_PYPROJECT> \
  --front-hostname <FRONT_NODE_NAME> \
  --backup-dir "$HOME/brigade-hub-backups"
```

Setup picks the retrieval mode and the backend (D30), in order:

1. A supported local GPU. The GPU container runtime is registered with Docker,
   a probe container with one GPU and no network sees the GPU, and the card
   has free memory for a model in `r1-models.json` that passed R1. Setup
   writes `COMPOSE_PROFILES=gpu`, the `local` backend and
   `retrieval = "hybrid"`.
2. Otherwise a remote endpoint, given with `--remote-url` and
   `--remote-token-file` (and `--remote-api v1/embeddings` for that request
   shape). The endpoint must be HTTPS, or plain HTTP to a tailnet name or
   address. Setup sends one probe embedding and checks the dimensions and the
   reported model digest. It writes the `remote` backend,
   `retrieval = "hybrid"` and a `compose.override.yaml` that mounts the token.
   The embedder container is never created.
3. Otherwise BM25 mode. Setup names each check that failed, writes
   `retrieval = "lexical"` and prints how to switch later.

To skip the GPU on purpose, pass `--no-gpu`. Setup never writes a hybrid
configuration it could not probe, offers only R1 models, and never
overwrites an existing secret. It writes `.env`, `config/hub.toml` and the
secrets, and creates the backup directory with mode 0700.

`config/hub.toml` is mounted into the hub for the planned `--config` (S15). Neither hub reads it yet, so on this hub both modes answer from BM25. The mode switch below already works on the files and the
containers, and S15 adds the status fields.

Put the private network's auth key in `secrets/front-authkey` (tagged, not
reusable, and expiring is enough: the front keeps its state in its volume
after the first login). Then restrict the file:

```bash
chmod 0644 secrets/front-authkey && "${EDITOR:-vi}" secrets/front-authkey && chmod 0444 secrets/front-authkey
```

Render the systemd units for review. Nothing is installed:

```bash
bin/hub-setup.py units --out /tmp/brigade-hub-units --compose-user <COMPOSE_ACCOUNT> \
  --hub-cpus <TWO_P_CORES> --embed-cpus <ONE_P_CORE> --agent-cpus <ALL_OTHER_CORES> \
  --db-device <DOCKER_DATA_ROOT_DEVICE>
```

## Build and start

```bash
docker compose build --pull hub
image_id="$(docker image inspect --format '{{.Id}}' localhost/brigade-fleet-hub:dogfood)"
sed -i "s|^BRIGADE_HUB_IMAGE=.*|BRIGADE_HUB_IMAGE=${image_id}|" .env
docker compose up -d --no-build
docker compose ps
docker compose exec hub python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:3774/health', timeout=2).read())"
checks/s31_checks.py memory
docker compose exec front tailscale serve status
```

`docker compose build` compiles `brigade-hub` in the Containerfile's Rust
stage, which takes a few minutes the first time. `checks/s31_checks.py memory`
sends `POST /memory` with the admin token and the `status` action on loopback
inside the hub container and expects 200. A 404 means the Python hub answered,
so `--route-mode memory=native` is missing from the hub's command.

Pinning `BRIGADE_HUB_IMAGE` to the image ID makes every start, the boot unit's
included, use the reviewed build. After the boot unit is enabled
(HOST-ACTIONS 4), `systemctl start brigade-hub-compose.service` replaces the
manual `up`.

Enroll the workstation's own node and the test principals through the front,
as for any hub ([Fleet hub on a Proxmox LXC](fleet-hub-proxmox.md), step 7).
The dogfood node's `fleet.toml` points at the front's HTTPS URL. No production
node points at this hub.

`--trust-tailscale-identity` is allowed in this layout, because only the
holder's pause process, the hub and the front share the namespace. It is off by
default. Turn it on only after `checks/s31_checks.py front` shows the front
replaces or removes a client's own `Tailscale-User-Login`. Add the flag to the
hub's `command` in `compose.override.yaml`.

## Switch retrieval modes

Both directions keep search answering.

Hybrid to BM25:

```bash
bin/hub-setup.py switch --to lexical          # retrieval = "lexical", gpu profile removed
docker compose --profile gpu rm -sf embedder  # stops and removes a running embedder
docker compose up -d --no-build               # restarts nothing else that is unchanged
docker compose ps                             # no embedder container
```

The model volume stays, so a switch back does not download the model again.
On the Rust hub, stored vectors stay in the memory file, unloaded, and only
the planned `memory reindex --drop <embedder id>` removes them.

BM25 to hybrid:

```bash
bin/hub-setup.py switch --to hybrid           # probes the GPU or the remote endpoint again
docker compose --profile model-pull run --rm model-pull   # once per model volume: the embedder has no egress
docker compose up -d --no-build               # starts the embedder under the gpu profile
```

If no embedder passes its probe, `switch --to hybrid` changes nothing and says
why. On the Rust hub the switch reports `switching` with coverage until the
backfill covers every live card, then `hybrid`. BM25 answers in the meantime.

## Restart and boot

- `restart: unless-stopped` on every long-running container. The Docker
  service is enabled at boot.
- The boot unit (`brigade-hub-compose.service`, HOST-ACTIONS 4) runs
  `docker compose up -d --no-build` after the Docker service, and again after
  every Docker start, a daemon restart's included. The daemon restores
  containers in no order the recipe relies on, and a joiner it starts before
  the holder fails. Compose starts the holder first and starts any joiner that
  failed.
- With the Rust engine, `fleet serve --engine rust` runs the same two
  checks inside the hub container every 10 s: after 3 failed `/health` checks, or 3 checks that
  find only loopback in its namespace (`BRIGADE_HUB_WATCHDOG_NETNS=1` in the
  recipe), it stops `brigade-hub` and exits 1, and the restart policy starts
  the hub again. The watchdog timer (`brigade-hub-watchdog.timer`, every
  10 s) stays as the backstop and restarts the hub container on the same
  rules when the process inside cannot act, such as a hung exec, and is the
  only watchdog for a pin without the engine. Docker
  never restarts an unhealthy container by itself.
- With a native family (memory here), `brigade-hub` restarts a Python hub
  that died, after 1 s and then doubling to 30 s, while the native routes keep
  serving. Proxied routes answer `502 upstream-down` and `/ready` answers 503
  until it is back. With every family proxied, `brigade-hub` exits instead and
  the restart policy restarts both.
- `stop_grace_period: 40s` covers the hub's shutdown. The Docker stop timeout
  drop-in and `"shutdown-timeout": 45` keep a host shutdown from cutting it
  short. On SIGTERM `brigade-hub` stops accepting and drains for at most
  25 s. Then it sends the Python hub SIGTERM, so the Python hub's own
  shutdown runs, and waits up to 10 s for it. A Python hub still running then
  gets SIGKILL. `brigade-hub` logs which of these happened and exits 0 only
  when the Python hub exited 0 after its SIGTERM. A stop that comes while the
  hub is still starting also sends the Python hub SIGTERM, but the Python hub
  may not have installed its handler yet. It then ends by that SIGTERM's
  default action, without its own shutdown, and `brigade-hub` exits 2. The
  Python hub runs in its own process group, so a Ctrl-C or a signal to the
  whole group reaches only `brigade-hub`, which stops it once. Native long
  polls answer their current page when the drain starts. When `brigade-hub`
  exits 0 after a stop, the entrypoint (`fleet serve --engine rust`, or
  the wrapper for an older pin) logs
  `brigade fleet hub stopped cleanly`, which the restart and UPS checks look
  for. It logs `brigade fleet hub listening on` once `/health` answers.
- Fence the hub with `touch hub.fenced && docker compose stop hub front` in
  the project directory. The boot unit and the watchdog leave the hub down
  while `hub.fenced` exists, so the fence survives a reboot or a Docker
  restart. Unfence with `rm hub.fenced && docker compose up -d --no-build`.
- Never run `docker compose down -v`. It deletes the named volume and every
  row in it.

## Backups

The backup timer (`brigade-hub-backup.timer`, HOST-ACTIONS 4) runs
`docker compose --profile backup run --rm --no-deps backup` every hour. The
job (`hub/backup.py`) runs `brigade fleet backup` twice, once per file, and
runs both calls whatever the other did. `fleet-hub.db` goes into a dated
directory under `BRIGADE_BACKUP_DIR`, and `fleet-hub-memory.db` into a dated
directory under `BRIGADE_BACKUP_DIR/memory`. Both are on the home volume,
never Docker's data root. Each copy goes through SQLite's online backup API
and gets an `integrity_check`. Each directory keeps 48 hourly copies and the
newest copy of each of the last 14 days. A missing memory file is logged and
skipped, so it never stops the hub file's copy. Any other failure of either
call fails the job.

Size the retention once, from the real file sizes:

```bash
docker compose --profile backup run --rm --no-deps --entrypoint brigade backup \
  fleet backup \
  --db /var/lib/brigade/fleet-hub.db --db /var/lib/brigade/fleet-hub-memory.db \
  --dest /backups --owner "$(id -u):$(id -g)" --plan --json
```

`--plan` prints the file sizes and the hourly count that keeps the directory
under 10% of the home volume's free space. Set `BRIGADE_BACKUP_KEEP_HOURLY`
in `.env` to it if it is under 48, and record both sizes in the S31 PR. A copy
that would leave the volume under `BRIGADE_BACKUP_MIN_FREE_GIB` (20) is not
written. First the oldest copies that only the hourly window keeps are
removed. If that cannot make room, nothing is removed and the job fails with
the reason, which the timer's journal shows.

The directory holds message bodies, card text and the audit log. It belongs to
the compose account, mode 0700, and must be in the workstation's off-host
backup job's include set. Never back up the live volume instead, because
committed rows can sit in the WAL. Back up the four secrets apart from the
copies, for example in the password manager.

## Restore from a copy

Any restore from a copy older than the hub's last write is lossy. Everything
committed after the copy is gone. Never put a copy back with a plain file
copy: use the restore tool, which keeps ids, claim generations and revisions
moving forward.

The restore below replaces `fleet-hub.db`. Step 3 can also put back a copy
of `fleet-hub-memory.db` from `BRIGADE_BACKUP_DIR/memory`, for example when
the memory file was lost. Skip that part to keep the live memory file, and
leave it and its sidecars in place. If the memory file is lost and not
restored, the hub creates an empty one on its next start.

A restored memory file needs a new `log_epoch` before the hub serves it.
Revisions are unique only within one epoch, and the copy hands out revision
numbers that clients saw before the loss. `brigade-hub memory-restore-epoch`
starts the new epoch. After that, each client's next `memory hub pull`
discards its replica and rebuilds it. A write based on a revision from before
the restore is refused with `409 memory_epoch_changed`, and a spooled `sync`
write of that kind becomes a local proposal.

1. Fence the hub: `touch hub.fenced && docker compose stop hub front` in the
   project directory. Clients spool from here on, and the fence holds across
   a reboot or Docker restart until step 5 removes it.
2. Restore on the host, as the compose account, with a Brigade at least as
   new as the copy's schema. Name both ledger copies, one beside the backups
   and one on the hub host:

   ```bash
   restore_dir="$(mktemp -d)"
   brigade fleet restore \
     --copy "$HOME/brigade-hub-backups/<STAMP>/fleet-hub.db" \
     --db "$restore_dir/fleet-hub.db" \
     --ledger "$HOME/brigade-hub-backups/recovery-ledger.jsonl" \
     --ledger "$HOME/.brigade/hub-recovery-ledger.jsonl" \
     --json
   ```

   Setup creates the ledger beside the backups, and HOST-ACTIONS 1 creates
   the hub-host copy with
   `install -D -m 0600 /dev/null "$HOME/.brigade/hub-recovery-ledger.jsonl"`.
   Only a ledger file that exists counts: a missing file may be a lost
   ledger, so the tool refuses rather than start from nothing. The tool
   creates a new ledger file only when you pass `--assert-no-prior-restore`.
   Without it, a named ledger that does not exist is listed under
   `ledgers_unreachable` and left alone, because its directory may be the
   empty mount point of a volume that is not mounted. Recreate a lost copy
   from a surviving one, then restore again.

   The tool advances the message sequence, the roster and policy revisions and
   the claim recovery floor, each 2^32 above the higher of the copy's value and
   the newest ledger record. It ends every claim, so every holder from the
   copy is refused as stale on its next renewal. It syncs its ledger record
   before the restored file is written. If no ledger can be reached, it
   refuses, unless you pass `--assert-no-prior-restore` because you know no
   earlier restore from this copy happened, and it records that statement.
3. Put the restored file on the volume with the hub's owner, replacing the
   old file and its sidecars:

   ```bash
   docker compose run --rm --no-deps --user 0 --cap-add CHOWN --cap-add DAC_OVERRIDE \
     -v "$restore_dir:/restore:ro" --entrypoint sh hub -c \
     'rm -f /var/lib/brigade/fleet-hub.db-wal /var/lib/brigade/fleet-hub.db-shm /var/lib/brigade/fleet-hub.db-journal &&
      install -o 10001 -g 10001 -m 0600 /restore/fleet-hub.db /var/lib/brigade/fleet-hub.db'
   ```

   To restore the memory file too, put its copy in place the same way, then
   start a new epoch on it. Both commands run while the hub is still stopped:

   ```bash
   docker compose run --rm --no-deps --user 0 --cap-add CHOWN --cap-add DAC_OVERRIDE \
     -v "$HOME/brigade-hub-backups/memory/<STAMP>:/restore:ro" --entrypoint sh hub -c \
     'rm -f /var/lib/brigade/fleet-hub-memory.db-wal /var/lib/brigade/fleet-hub-memory.db-shm /var/lib/brigade/fleet-hub-memory.db-journal &&
      install -o 10001 -g 10001 -m 0600 /restore/fleet-hub-memory.db /var/lib/brigade/fleet-hub-memory.db'
   docker compose run --rm --no-deps --entrypoint brigade-hub hub \
     memory-restore-epoch --memory-db /var/lib/brigade/fleet-hub-memory.db
   ```

   The command prints one JSON line with `previous_log_epoch` and the new
   `log_epoch`. Keep it with the restore record. It changes only the
   `log_epoch` value and refuses, changing nothing, in these cases: the
   path is a symbolic link, a process still holds the file, a non-empty `-wal` or any `-journal` sits
   beside it, the file is not a memory file, its schema is newer than this
   `brigade-hub`, or SQLite's `quick_check` fails. Running it again starts
   another new epoch, which is safe. Never skip the command because an
   earlier run seemed to cover this copy.

4. Start the hub alone, still fenced, and bring it up to date before any
   client can reach it:

   ```bash
   docker compose up -d --no-build netns
   docker compose up -d --no-build --no-deps hub
   ```

   `hub.fenced` stays in place and `front` stays stopped, so the hub listens
   only on loopback inside its namespace and no client can connect. Run every
   update from inside that namespace, against loopback:

   ```bash
   docker compose exec hub sh -c \
     'BRIGADE_FLEET_HUB_URL=http://127.0.0.1:3774 BRIGADE_FLEET_TOKEN="$(cat /run/secrets/admin_token)" brigade fleet <command>'
   ```

   Re-apply every credential revocation, roster change and policy edit made
   since the copy, from your own record, because the hub's audit rows for
   that time are gone. If the memory file was restored, check
   `verifier_generation.current` in the memory `status` and run
   `verifier_resync` when it is false
   ([memory operations](../memory-operations.md#recovery-after-restoring-either-database)). Each mesh signer's newest membership, manifest and
   revocation statements must be published again from the signer's own copy.
   Signers cannot reach a fenced hub, so bring their signed statements to the
   hub host and submit them through the same loopback path. If the hub
   identity record changed after the copy, publish that too.
5. Open the hub to clients: `rm hub.fenced && docker compose up -d --no-build`. Nodes enrolled after the copy get
   401 and enroll again. Messages acknowledged after the copy are gone, and a
   message can arrive twice.

A Python hub that predates the recovery floor refuses the restored file for
its `user_version` (schema 50), so a downgrade fails closed.

Rehearse the restore monthly with `checks/s31_checks.py restore --drive`. It
restores the newest copy twice into a throwaway directory, checks that every
counter rises, and runs the full drive against the result in a throwaway
container with no network.

## Optional agent-account hardening

On a single-user machine the owner's agents run as the owner, and an owner in
the `docker` group or with `sudo` gives every agent the same rights:
`docker exec` into the hub, a read of both secrets and the volume, or a
container that joins the hub's namespace. The recipe does not try to isolate
the hub from its owner's own processes. That is accepted for the dogfood hub.

A multi-user or shared host that wants the hub isolated runs its agents under a
separate account (HOST-ACTIONS 5): not in the `docker` group, with no `sudo`
rule, and unable to act as the operator or the compose account. The agents'
files, credentials and session services move to that account. Their
Docker-based builds and tests then need a rootless Docker daemon for that
account, a separate package that needs approval, or they stop running there.
The hub is only as isolated as the host's most privileged agent account.

Check it from the agent account itself:

```bash
sudo -u <AGENT_ACCOUNT> python3 ops/fleet-hub/checks/s31_checks.py --record /tmp/s31-hardening.jsonl \
  hardening --volume /var/lib/docker/volumes/brigade-hub_brigade-state/_data \
  --backup-dir <BACKUP_DIR> --hub-pid "$(docker inspect -f '{{.State.Pid}}' brigade-hub-hub-1)"
```

`sudo -n true`, `docker ps`, entering the hub's namespace and reading the
volume and the backup directory must all fail.

## Acceptance checks

After the matching host actions are done, run each check from
`ops/fleet-hub/` and keep `s31-results.jsonl` for the S31 PR. Set
`BRIGADE_S31_FRONT_URL` to the front's HTTPS URL first.

| Acceptance item | Command |
|---|---|
| UPS monitor and simulated low battery | `checks/s31_checks.py ups --phase before`, then `sudo upsmon -c fsd`, then after the boot `checks/s31_checks.py ups --phase after` |
| GPU toolkit, embedder sees the GPU, hub has none | `checks/s31_checks.py gpu` |
| cgroup readbacks | `checks/s31_checks.py cgroups --hub-cpus <...> --embed-cpus <...> --service <agent-service> ...` |
| I/O weight or latency target only | `checks/s31_checks.py io --device-id <MAJ:MIN>`, then, as the compose account, `checks/s31_checks.py fdatasync --slice brigade-hub.slice --path <dir on the data root> --writer <dir on the same device>`. Only the probe enters the slice, and the writer stays in the caller's session |
| Setup on throwaway projects | `checks/s31_checks.py setup [--remote --remote-url <URL> --remote-token-file <FILE>]` |
| Access from the host | `checks/s31_checks.py access [--allow-loopback-listener PORT=EXE_REALPATH//REASON ...]`, see [Loopback exemptions](#loopback-exemptions) |
| Front: forged header, SSH off, forwarded-for | `checks/s31_checks.py front` (starts a header echo for the test only) |
| Refused connections at the fleet's concurrency | `checks/s31_checks.py load --concurrency <N>` |
| Hub memory answers on the Rust hub | `checks/s31_checks.py memory` |
| Restarts and reboots (each at least 3 times) | `checks/s31_checks.py restart --case stop`, `--case hub-kill`, `--case holder-restart`, `--case daemon-restart`, and after each host reboot `--case after-reboot`. `checks/s31_checks.py summary` counts them |
| Backups | `checks/s31_checks.py backups --include-file <OFF_HOST_INCLUDE_LIST> --refusal` |
| Restore tool and the full drive on a copy | `checks/s31_checks.py restore --drive` |
| Agent-account hardening | the `hardening` command above, from a throwaway account |
| Measurements | `checks/s31_checks.py probe`, `checks/s31_checks.py commit --fanout <N>` with and without the fan-out, and `checks/s31_checks.py fdatasync` on both hosts' hub volumes |

### Loopback exemptions

`access` fails on any listener the host can reach at a hub port, including one
on host loopback. The hub runs in its own network namespace, so a loopback
listener at its port number is another host process, but no network probe can
prove that process is not a proxy to the hub. The check records each listener's
status line and headers as evidence and never passes on them.

An exemption trusts the named program not to forward to the hub. Before you
add one, confirm what the program is and that it does not proxy or relay to
the hub port. The check can confirm which program holds the socket, not what
that program does.

To exempt an unrelated program that holds a hub port number on host loopback,
find the canonical path of its executable and name it in the exemption:

```bash
ss -ltnpH 'sport = :3774'              # the holder's pid
readlink -f /proc/<PID>/exe            # EXE_REALPATH
sudo --preserve-env=BRIGADE_S31_FRONT_URL python3 checks/s31_checks.py \
  --record "$PWD/s31-results.jsonl" access \
  --allow-loopback-listener "3774=/opt/desktop-app/bin/app//desktop app on host loopback, verified not to forward to the hub"
```

The value is `PORT=EXE_REALPATH//REASON`. PORT is digits, so the first `=` ends
it. EXE_REALPATH must be the canonical absolute path that `readlink -f` prints,
and a canonical path never contains `//`, so the first `//` after it starts the
reason. The reason may contain any text and is required. The check rejects
anything else, including a relative or non-normalized path and a path ending in
` (deleted)`. The flag repeats for more ports.

An exemption needs a complete census of the processes holding the socket, and
only root can read every process's fd table. Run the check with an exemption
under `sudo`, as above. Without root every exemption is refused with the reason
`exemption needs a complete holder census; run the access check as root`, and
the listener stays FAIL. The check without exemptions still runs unprivileged.

Running it as root executes the checkout's `checks/s31_checks.py` as root. Run
it only from a checkout that no other account can write to, and pass an
absolute `--record` path. The check appends to the record without following a
symlink, refuses anything that is not a regular file, and as root refuses a
record file owned by any account other than root or the one that ran `sudo`.

The check detects exposure misconfiguration. It does not defend against a
privileged process that deliberately evades it while it runs, because such a
process could also edit the check.

The exemption turns that listener into INFO only when all of these hold:

- The check runs as root.
- The netns container does not use host networking and publishes no port at
  all, because a published container port can sit behind any host port number.
- The census is complete: every process's fd table and fd links on the host
  were read while nothing changed. Any read error, such as a non-dumpable child
  that inherited the socket, refuses the exemption, because a hidden co-holder
  could be the hub. A process or fd that vanishes during the scan refuses it
  too, with `processes changed during the census; rerun`. On a busy host, run
  the check again.
- The check can name every process holding a LISTEN socket on that port on
  loopback or the wildcard address, and can read each holder's executable link,
  command name and cgroup.
- Every holder's `/proc/<pid>/exe` resolves to exactly EXE_REALPATH. A copy at
  another path, another program, or a binary that was replaced or deleted
  since it started (its link reads `... (deleted)`) does not match.
- No holder is `brigade-hub` or sits in a container of the hub's image.

Otherwise the result is FAIL and the record says which condition failed. The
record keeps the port, the path, the reason, and each holder's pid, executable
and command name. An exemption that matches no reachable listener is recorded
as unused. A program that runs from a per-launch mount path needs the path
updated after each launch.

The status fields of [Retrieval modes](../phase-v2-hub-architecture.md#retrieval-modes)
(`lexical` and `ok` for BM25 chosen, `degraded` for a hybrid project whose
embedder is stopped) need S15's status route. `checks/s31_checks.py setup`
reports them as pending until then.
