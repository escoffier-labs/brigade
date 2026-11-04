# Dogfood hub from the Compose recipe

This runbook stands up the dogfood Fleet Hub on the GPU workstation from the
Compose recipe in `ops/fleet-hub/` (S31 in
[the v2 hub design](../phase-v2-hub-architecture.md#the-dogfood-hub-on-the-gpu-workstation)).
The recipe is the install path the design offers everyday users. The dogfood
hub is not production: no production node points at it, and it holds dogfood
data only. Production stays on the LXC hub host
([Fleet hub on a Proxmox LXC](fleet-hub-proxmox.md)). It runs today's Python
hub. S10 moves the same recipe to the Rust hub.

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
| `hub` | `service:netns` | `brigade fleet serve` bound to `127.0.0.1:3774`. No GPU, no published port. |
| `front` | `service:netns` | The private network's HTTPS front. Terminates HTTPS with the certificate the private network issues for its own name and forwards to `127.0.0.1:3774`, so the hub's peer is loopback, as on the LXC hub host. SSH stays off. |
| `embedder` | `embed` only (internal) | Only under the `gpu` profile. One GPU through a device reservation, its own model volume, no published port. The hub reaches it as `http://embedder:11434`. |
| `backup` | none | One-shot `brigade fleet backup`, started hourly by a timer. |

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
- `embed-token`: only for the `remote` embedder backend.

Each file is mode 0444. The containers run as other UIDs and must read the
bind mount, so the 0700 directory protects the files from other host users.

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
`BRIGADE_FLEET_FINDINGS_KEY_FILE`, which landed with S31. The default
`BRIGADE_REF` in `compose.yaml` predates them. Pass the merged commit and its
source archive digest to setup:

```bash
ref=<MERGED_COMMIT_SHA>
curl -fsSL "https://github.com/escoffier-labs/brigade/archive/${ref}.tar.gz" | sha256sum
```

## Set up

Run setup as the compose account, from the repository checkout:

```bash
cd ops/fleet-hub
bin/hub-setup.py init \
  --brigade-ref <MERGED_COMMIT_SHA> --brigade-sha256 <ARCHIVE_SHA256> \
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

`config/hub.toml` is mounted into the hub for the planned Rust hub (S10, S15).
Today's Python hub has no embedder and ignores it, so on this hub both modes
answer from BM25. The mode switch below already works on the files and the
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
docker compose exec front tailscale serve status
```

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
docker compose up -d --no-build               # starts the embedder under the gpu profile
docker compose --profile gpu exec embedder ollama pull <R1_MODEL>   # once per model volume
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
- The watchdog timer (`brigade-hub-watchdog.timer`, every 10 s) restarts the
  hub container after 3 failed `/health` checks, or after 3 checks that find
  only loopback in its namespace. Docker never restarts an unhealthy
  container by itself.
- `stop_grace_period: 40s` covers the hub's shutdown. The Docker stop timeout
  drop-in and `"shutdown-timeout": 45` keep a host shutdown from cutting it
  short. On SIGTERM the hub logs `brigade fleet hub stopped cleanly` and exits
  0, which the restart and UPS checks look for.
- Fence the hub with `docker compose stop hub front`. `restart: unless-stopped`
  does not undo a stop at boot.
- Never run `docker compose down -v`. It deletes the named volume and every
  row in it.

## Backups

The backup timer (`brigade-hub-backup.timer`, HOST-ACTIONS 4) runs
`docker compose --profile backup run --rm --no-deps backup` every hour.
`brigade fleet backup` copies each database file through SQLite's online
backup API into a dated directory under `BRIGADE_BACKUP_DIR` on the home
volume, never Docker's data root. It runs `integrity_check` on every copy and
keeps 48 hourly copies and the newest copy of each of the last 14 days.

Size the retention once, from the real file sizes:

```bash
docker compose --profile backup run --rm --no-deps backup \
  --db /var/lib/brigade/fleet-hub.db --dest /backups --owner "$(id -u):$(id -g)" --plan --json
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

1. Fence the hub: `docker compose stop hub front`. Clients spool from here on.
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
     'rm -f /var/lib/brigade/fleet-hub.db-wal /var/lib/brigade/fleet-hub.db-shm &&
      install -o 10001 -g 10001 -m 0600 /restore/fleet-hub.db /var/lib/brigade/fleet-hub.db'
   ```

4. Before clients reconnect, each mesh signer publishes its newest membership,
   manifest and revocation statements again from its own copy. If the hub
   identity record changed after the copy, publish that too. Re-apply every
   credential revocation, roster change and policy edit made since the copy,
   from your own record, because the hub's audit rows for that time are gone.
5. Start: `docker compose up -d --no-build`. Nodes enrolled after the copy get
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
| I/O weight or latency target only | `checks/s31_checks.py io --device-id <MAJ:MIN>`, then `sudo systemd-run --scope --slice=brigade-hub.slice python3 checks/s31_checks.py fdatasync --path <dir on the data root> --writer <dir on the same device>` |
| Setup on throwaway projects | `checks/s31_checks.py setup [--remote --remote-url <URL> --remote-token-file <FILE>]` |
| Access from the host | `checks/s31_checks.py access` |
| Front: forged header, SSH off, forwarded-for | `checks/s31_checks.py front` (starts a header echo for the test only) |
| Refused connections at the fleet's concurrency | `checks/s31_checks.py load --concurrency <N>` |
| Restarts and reboots (each at least 3 times) | `checks/s31_checks.py restart --case stop`, `--case hub-kill`, `--case holder-restart`, `--case daemon-restart`, and after each host reboot `--case after-reboot`. `checks/s31_checks.py summary` counts them |
| Backups | `checks/s31_checks.py backups --include-file <OFF_HOST_INCLUDE_LIST> --refusal` |
| Restore tool and the full drive on a copy | `checks/s31_checks.py restore --drive` |
| Agent-account hardening | the `hardening` command above, from a throwaway account |
| Measurements | `checks/s31_checks.py probe`, `checks/s31_checks.py commit --fanout <N>` with and without the fan-out, and `checks/s31_checks.py fdatasync` on both hosts' hub volumes |

The status fields of [Retrieval modes](../phase-v2-hub-architecture.md#retrieval-modes)
(`lexical` and `ok` for BM25 chosen, `degraded` for a hybrid project whose
embedder is stopped) need S15's status route. `checks/s31_checks.py setup`
reports them as pending until then.
