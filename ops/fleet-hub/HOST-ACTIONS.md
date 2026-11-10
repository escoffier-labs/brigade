# S31 host actions for the dogfood hub

Every change the dogfood hub needs on the GPU workstation, one numbered action
each. Nothing in this repository runs them. The maintainer approves each
action, and each new package, on its own before anyone runs it. Each action gives
what it installs or changes, why S31 needs it, the exact command, how to verify
it, and how to undo it. The verification column names the scripted check in
`checks/s31_checks.py` that turns the action into an S31 acceptance item.

Conventions:

- Run from `ops/fleet-hub/` in the repository checkout, as the compose account
  (the account that runs the compose project) unless the command says `sudo`.
- `<ANGLE_BRACKET>` values are host-specific. Put them only in the files setup
  writes, which Git ignores.
- Render the unit files once for review before actions 3 to 6:

  ```bash
  bin/hub-setup.py units --out /tmp/brigade-hub-units --compose-user <COMPOSE_ACCOUNT> \
    --hub-cpus <TWO_P_CORES> --embed-cpus <ONE_P_CORE> --agent-cpus <ALL_OTHER_CORES> \
    --db-device <DOCKER_DATA_ROOT_DEVICE>
  ```

  Pick two performance cores for the hub, one for the embedder, and give the
  agents every other core: 5 performance cores and all 16 efficiency cores on
  this CPU. `lscpu --extended` shows which logical CPUs are performance cores.

| # | Action | Kind | New package |
|---|---|---|---|
| 1 | Secrets files | files in the checkout | none |
| 2 | Private-network node for the front | private-network admin | none |
| 3 | Reserved cgroup slices | systemd units and drop-ins | none |
| 4 | Docker stop timeouts | Docker daemon setting and systemd drop-in | none |
| 5 | Boot unit | systemd unit | none |
| 6 | Watchdog and backup timers | systemd units | none |
| 7 | GPU container toolkit | package and Docker runtime | yes |
| 8 | UPS monitor and the low-battery shutdown test | package and config | yes |
| 9 | I/O cost model on the database device (optional) | kernel setting | none |
| 10 | Agent-account hardening (optional) | account | none |
| 11 | Rootless Docker for the agent account (only with 10) | package | yes |

## 1. Secrets files

- Installs or changes: `ops/fleet-hub/secrets/` (mode 0700) with
  `admin-token.txt`, `findings-key` (32 random bytes) and `front-authkey`,
  each mode 0444, plus `embed-token` for a remote embedder or
  `embed-local-token` for the local one, and
  `operator-admin-token.txt` (mode 0600), the operator's copy of the admin
  token for `brigade memory rescan` and `reindex`. Also `.env`,
  `config/hub.toml`, the backup directory (mode 0700) and the empty recovery
  ledger in it (mode 0600). All ignored by Git or outside the checkout.
- Why: the recipe's two secrets. The admin token is a Docker secret, and the
  findings and recall-log HMAC key is a second secret off the state volume, so a
  volume copy alone cannot test guesses. The front needs its private-network
  auth key once.
- Command:

  ```bash
  bin/hub-setup.py init --brigade-ref <MERGED_COMMIT_SHA> --brigade-sha256 <ARCHIVE_SHA256> \
    --brigade-version <VERSION_IN_ITS_PYPROJECT> --front-hostname <FRONT_NODE_NAME> --backup-dir "$HOME/brigade-hub-backups"
  chmod 0644 secrets/front-authkey && "${EDITOR:-vi}" secrets/front-authkey && chmod 0444 secrets/front-authkey
  ```

  Create the hub-host ledger copy too:
  `install -D -m 0600 /dev/null "$HOME/.brigade/hub-recovery-ledger.jsonl"`.
  Then copy `admin-token.txt` and `findings-key` to the password manager.
  They are backed up apart from the database copies.
- Verify: `stat -c '%a %n' secrets secrets/* "$HOME/brigade-hub-backups"`
  prints 700 for the directories, 600 for `operator-admin-token.txt` and 444
  for each other file.
  `wc -c secrets/findings-key` prints 32.
- Undo: before the hub holds data, `rm -r secrets .env config compose.override.yaml`.
  After it holds findings, a missing or different `findings-key` makes
  findings answer 503, so restore the saved key instead of deleting it.

## 2. Private-network node for the front

- Installs or changes: one new node on the private network, named
  `<FRONT_NODE_NAME>`, with a tag for the dogfood hub, and an ACL rule that
  lets the dogfood clients reach its port 443. Its state lives in the
  `front-state` volume.
- Why: the front holds the hub's private-network identity and terminates HTTPS
  with the certificate the private network issues for its own name, inside the
  hub's namespace. Its SSH server stays off (`TS_EXTRA_ARGS=--ssh=false`),
  because a remote shell into the front would sit in the hub's namespace.
- Command: in the private network's admin console, create a tagged,
  single-use, expiring auth key and put it in `secrets/front-authkey`
  (action 1). Add the ACL rule for the tag. No production node gets a rule
  that points it at this node.
- Verify: after the first start, `docker compose exec front tailscale status`
  lists the node, and `checks/s31_checks.py front` shows SSH off and the forged
  identity header replaced or removed.
- Undo: `docker compose stop front`, remove the node and the ACL rule in the
  admin console, then `docker volume rm brigade-hub_front-state`.

## 3. Reserved cgroup slices

- Installs or changes: `/etc/systemd/system/brigade.slice`,
  `brigade-hub.slice` and `brigade-embed.slice`, the drop-in
  `/etc/systemd/system/user.slice.d/50-brigade-agent-cpus.conf`, and the same
  `AllowedCPUs` drop-in for each system service that runs agent or model work
  (the host model server, the production embedder, the agent session servers).
- Why: Docker's `cpus` and memory limits are caps, and a cap does not protect
  the hub from agent fan-outs that pushed the load average to about 170. The
  slices reserve 2 performance cores, `MemoryMin` 1 GiB and `MemoryMax` 3 GiB
  for the hub, and 1 core, 2 GiB and 4 GiB for the embedder. They set
  `CPUWeight=10000`, `IOWeight=1000` and an I/O latency target on the database
  device. The kernel applies a latency target only between peers, so the one
  that acts on agent writers in `user.slice` and `system.slice` is on
  `brigade.slice`, their peer, and the hub slice's own target acts on the
  embedder slice beside it. The drop-ins keep agent work off the reserved cores.
- Command (Docker must use the systemd cgroup driver:
  `docker info -f '{{.CgroupDriver}}'` prints `systemd`):

  ```bash
  u=/tmp/brigade-hub-units
  sudo install -m 0644 "$u/brigade.slice" "$u/brigade-hub.slice" "$u/brigade-embed.slice" /etc/systemd/system/
  sudo install -D -m 0644 "$u/user.slice.d/50-brigade-agent-cpus.conf" /etc/systemd/system/user.slice.d/50-brigade-agent-cpus.conf
  for unit in <AGENT_SERVICE_UNITS>; do
    sudo install -D -m 0644 "$u/service.d/50-brigade-agent-cpus.conf" "/etc/systemd/system/$unit.d/50-brigade-agent-cpus.conf"
  done
  sudo systemctl daemon-reload
  sudo systemctl start brigade.slice brigade-hub.slice brigade-embed.slice
  ```

  Install the slices before the first `docker compose up`, so the containers
  start under them. If the containers already run, recreate them with
  `docker compose up -d --no-build --force-recreate`.
- Verify: `checks/s31_checks.py cgroups --hub-cpus <TWO_P_CORES> --embed-cpus <ONE_P_CORE> --service <AGENT_SERVICE_UNIT> ...`.
  It reads `cpuset.cpus.effective` on each slice and on every agent cgroup,
  never the configured value alone, because a set the parent cannot meet falls
  back to the parent's set without an error. If `user.slice` still shows every
  core after the reload, reboot and run the check again.
- Undo: remove the files above, `sudo systemctl daemon-reload`, then
  `docker compose up -d --no-build --force-recreate` and reboot to clear the
  `user.slice` restriction.

## 4. Docker stop timeouts

- Installs or changes: `"shutdown-timeout": 45` in `/etc/docker/daemon.json`
  and `/etc/systemd/system/docker.service.d/50-brigade-stop.conf`
  (`TimeoutStopSec=120`).
- Why: the hub's `stop_grace_period` is 40 s. The daemon's own shutdown
  timeout and the unit's stop timeout can be shorter than a container's grace
  period, which would cut the hub off during a host shutdown, the UPS one's
  included.
- Command (restarting Docker stops every container on the workstation for a
  moment, so pick a quiet time):

  ```bash
  sudo cp -a /etc/docker/daemon.json /etc/docker/daemon.json.bak-s31 2>/dev/null || echo '{}' | sudo tee /etc/docker/daemon.json
  sudo python3 -c 'import json,sys; p="/etc/docker/daemon.json"; d=json.load(open(p, encoding="utf-8")); d["shutdown-timeout"]=45; open(p,"w",encoding="utf-8").write(json.dumps(d, indent=2)+"\n")'
  sudo install -D -m 0644 /tmp/brigade-hub-units/docker.service.d/50-brigade-stop.conf /etc/systemd/system/docker.service.d/50-brigade-stop.conf
  sudo systemctl daemon-reload
  sudo systemctl restart docker
  ```

- Verify: `systemctl show docker -p TimeoutStopUSec` prints `2min`, and
  `sudo python3 -c 'import json; print(json.load(open("/etc/docker/daemon.json"))["shutdown-timeout"])'`
  prints 45. `checks/s31_checks.py restart --case stop` shows exit 0 and the
  clean-exit line.
- Undo: `sudo mv /etc/docker/daemon.json.bak-s31 /etc/docker/daemon.json`
  (or remove the key), remove the drop-in, `sudo systemctl daemon-reload` and
  `sudo systemctl restart docker`.

## 5. Boot unit

- Installs or changes: `/etc/systemd/system/brigade-hub-compose.service`,
  enabled. It runs `docker compose up -d --no-build` for the project as the
  compose account.
- Why: after a daemon restart or a host boot the daemon restores containers
  in no order the recipe relies on, and a joiner it starts before the holder
  fails to start. Compose starts the holder first. `BindsTo=docker.service`
  and `WantedBy=docker.service` run the unit again after every Docker start.
- Command:

  ```bash
  sudo install -m 0644 /tmp/brigade-hub-units/brigade-hub-compose.service /etc/systemd/system/
  sudo systemctl daemon-reload
  sudo systemctl enable --now brigade-hub-compose.service
  ```

- Verify: `systemctl is-enabled brigade-hub-compose.service` prints
  `enabled`. Reboot the host and restart the daemon at least 3 times each, and
  after each one run `checks/s31_checks.py restart --case after-reboot` or
  `--case daemon-restart`. `checks/s31_checks.py summary` counts the passes.
- Undo: `sudo systemctl disable --now brigade-hub-compose.service`, remove the
  file, `sudo systemctl daemon-reload`. The containers keep running under
  their restart policy.

## 6. Watchdog and backup timers

- Installs or changes: `/etc/systemd/system/brigade-hub-watchdog.service`
  and `.timer` (every 10 s), and `/etc/systemd/system/brigade-hub-backup.service`
  and `.timer` (hourly), both timers enabled.
- Why: Docker never restarts an unhealthy container. Until S10 the watchdog
  timer restarts the hub after 3 failed `/health` checks, or after 3 checks
  that find only loopback in its namespace. The backup timer runs the planned
  `fleet backup` hourly into the 0700 directory on the home volume.
- Command:

  ```bash
  u=/tmp/brigade-hub-units
  sudo install -m 0644 "$u/brigade-hub-watchdog.service" "$u/brigade-hub-watchdog.timer" \
    "$u/brigade-hub-backup.service" "$u/brigade-hub-backup.timer" /etc/systemd/system/
  sudo systemctl daemon-reload
  sudo systemctl enable --now brigade-hub-watchdog.timer brigade-hub-backup.timer
  ```

  Add the backup directory to the workstation's off-host backup job's include
  set.
- Verify: `systemctl list-timers 'brigade-hub-*'` lists both timers.
  `sudo systemctl start brigade-hub-backup.service`, then
  `checks/s31_checks.py backups --include-file <OFF_HOST_INCLUDE_LIST> --refusal`.
  `checks/s31_checks.py restart --case holder-restart` exercises the watchdog's
  namespace rule.
- Undo: `sudo systemctl disable --now brigade-hub-watchdog.timer brigade-hub-backup.timer`,
  remove the four files, `sudo systemctl daemon-reload`. The copies stay in
  the backup directory.

## 7. GPU container toolkit

- Installs or changes: the GPU vendor's container toolkit package
  (`nvidia-container-toolkit` from the vendor's apt repository, with its
  signing key and source list), and a `nvidia` runtime entry in
  `/etc/docker/daemon.json`.
- Why: Docker on the workstation has only the `runc` runtime, so the compose
  GPU reservation of the embedder service cannot start. Setup's first path,
  the `gpu` profile with the `local` backend, needs it. The hub container
  never gets a GPU.
- Command (restarting Docker stops every container on the workstation for a
  moment):

  ```bash
  curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
    | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
  curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
    | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
    | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
  sudo apt-get update
  sudo apt-get install -y nvidia-container-toolkit
  sudo cp -a /etc/docker/daemon.json /etc/docker/daemon.json.bak-s31-gpu
  sudo nvidia-ctk runtime configure --runtime=docker
  sudo systemctl restart docker
  ```

- Verify: `docker info --format '{{json .Runtimes}}'` includes `nvidia`.
  `bin/hub-setup.py switch --to hybrid` passes its GPU probe, then
  `docker compose up -d --no-build` starts the embedder, and
  `checks/s31_checks.py gpu` shows the embedder sees the GPU and the hub,
  holder and front have none.
- Undo: `bin/hub-setup.py switch --to lexical` and
  `docker compose --profile gpu rm -sf embedder`, then
  `sudo apt-get remove -y nvidia-container-toolkit`,
  `sudo mv /etc/docker/daemon.json.bak-s31-gpu /etc/docker/daemon.json`,
  `sudo rm /etc/apt/sources.list.d/nvidia-container-toolkit.list /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg`
  and `sudo systemctl restart docker`.

## 8. UPS monitor and the low-battery shutdown test

- Installs or changes: the `nut` package (Network UPS Tools: driver, server
  and monitor), `/etc/nut/nut.conf`, `ups.conf`, `upsd.users` and
  `upsmon.conf` from `ups/*.example`, and its services enabled.
- Why: the workstation is on a UPS, but no monitor runs, and the desktop power
  service reports no UPS device. S31 counts as done only after a simulated
  low-battery event ends in a clean host shutdown and a clean hub exit. `FULL`
  stays, because a UPS does not cover a kernel panic, a power supply failure
  or a battery that fails first.
- Command:

  ```bash
  sudo apt-get install -y nut
  sudo nut-scanner -U                         # note the vendor and product ids
  for f in nut.conf ups.conf upsd.users upsmon.conf; do
    sudo install -m 0640 -g nut "ups/$f.example" "/etc/nut/$f"
  done
  sudoedit /etc/nut/ups.conf /etc/nut/upsd.users /etc/nut/upsmon.conf   # fill the placeholders
  sudo systemctl enable --now nut-driver-enumerator.service nut-server.service nut-monitor.service
  ```

  The low-battery test shuts the host down, so save open work first. Leave
  `POWERDOWNFLAG` unset for the test so the UPS keeps its output on:

  ```bash
  checks/s31_checks.py ups --phase before
  sudo upsmon -c fsd
  # after the host boots again:
  checks/s31_checks.py ups --phase after
  ```

- Verify: `upsc workstation@localhost ups.status` prints `OL`. The `after`
  phase finds the monitor's forced shutdown in the previous boot's journal,
  the hub's `brigade fleet hub stopped cleanly` line, and the hub answering
  through the front.
- Undo: `sudo systemctl disable --now nut-monitor.service nut-server.service nut-driver-enumerator.service`,
  `sudo apt-get remove -y nut nut-server nut-client`, and remove the four
  files from `/etc/nut/`.

## 9. I/O cost model on the database device (optional)

- Installs or changes: a line in `/sys/fs/cgroup/io.cost.qos` for the device
  that holds Docker's data root. It is a runtime kernel setting with no
  package, and it is lost at reboot.
- Why: both NVMe devices use the `none` scheduler and `io.cost.qos` is empty,
  so no controller enforces `IOWeight` and only the latency target acts. S31
  either shows the weight taking effect under a synthetic agent writer or
  records that only the latency target is in force.
- Command: `lsblk -o NAME,MAJ:MIN` for the device, then
  `echo '<MAJ:MIN> enable=1 ctrl=auto' | sudo tee /sys/fs/cgroup/io.cost.qos`.
- Verify: `checks/s31_checks.py io --device-id <MAJ:MIN>` reports the cost
  model enabled. Then, as the compose account, compare
  `checks/s31_checks.py fdatasync --slice brigade-hub.slice --path <DIR_ON_DATA_ROOT> --writer <DIR_ON_SAME_DEVICE>`
  with and without the setting. Only the probe enters the hub slice (through
  `sudo systemd-run --scope`), and the writer stays outside it in the caller's
  session, like agent traffic. Record both, or record "latency target only"
  if the action is declined.
- Undo: `echo '<MAJ:MIN> enable=0' | sudo tee /sys/fs/cgroup/io.cost.qos`, or
  reboot.

## 10. Agent-account hardening (optional)

- Installs or changes: an unprivileged account for agents, not in the `docker`
  group, with no `sudo` rule, unable to act as the operator or the compose
  account. For the S31 check, a throwaway account that is removed afterwards.
- Why: on a single-user machine the owner's agents run as the owner, which is
  accepted for the dogfood hub. A multi-user or shared host that wants the
  hub isolated runs agents under such an account, because `docker` group
  membership and passwordless `sudo` are each equivalent to root. S31 runs the
  checks once from a throwaway account, so the documented hardening is known
  to work. The maintainer's agents keep running as the owner.
- Command (the throwaway check):

  ```bash
  sudo useradd --system --no-create-home --shell /usr/sbin/nologin s31-probe
  id s31-probe                                  # no docker group
  sudo -l -U s31-probe                          # no sudo rule
  sudo -u s31-probe python3 checks/s31_checks.py --record /tmp/s31-hardening.jsonl hardening \
    --volume /var/lib/docker/volumes/brigade-hub_brigade-state/_data \
    --backup-dir "$HOME/brigade-hub-backups" \
    --hub-pid "$(docker inspect -f '{{.State.Pid}}' "$(docker compose ps -q hub)")"
  ```

  Adopting it for real agents also moves their files, credentials and session
  services to the account (runbook: Optional agent-account hardening).
- Verify: the check prints PASS for `sudo -n true` failing, `docker ps`
  failing, entering the hub's namespace failing, and both directories
  unreadable. Copy `/tmp/s31-hardening.jsonl` into the S31 record.
- Undo: `sudo userdel s31-probe` (and for a real agent account,
  `sudo userdel -r <AGENT_ACCOUNT>` after moving its work back).

## 11. Rootless Docker for the agent account (only with 10)

- Installs or changes: the rootless Docker packages for the distribution's
  Docker build (on Ubuntu's `docker.io`: `rootlesskit`, `uidmap` and
  `slirp4netns`, with the names confirmed by `apt-cache policy` before approval),
  and a per-user daemon for the agent account.
- Why: only if action 10 is adopted for real agents and their Docker-based
  builds and tests should keep running on this host. Without it those builds
  stop running here. The hub itself never runs rootless: a rootless hub could
  not hold its `cpuset` or I/O reservations.
- Command: `sudo apt-get install -y rootlesskit uidmap slirp4netns`, then, as
  the agent account, the distribution's rootless setup tool, and
  `sudo loginctl enable-linger <AGENT_ACCOUNT>`.
- Verify: as the agent account, `docker info -f '{{.SecurityOptions}}'`
  includes `rootless`, and action 10's checks still pass, because the rootless
  daemon cannot see the system daemon's containers.
- Undo: as the agent account, the setup tool's `uninstall`, then
  `sudo loginctl disable-linger <AGENT_ACCOUNT>` and
  `sudo apt-get remove -y rootlesskit uidmap slirp4netns`.

## Checks that need no host action

Run these once the hub is up and record them in the S31 PR:
`checks/s31_checks.py access`, `front`, `load`, `restart --case stop`,
`--case hub-kill`, `--case holder-restart`, `restore --drive`, `probe`,
`commit` (with and without `--fanout`) and `fdatasync` on both hosts' hub
volumes. Copy the stdlib-only `checks/s31_checks.py` to the LXC hub host for
its `fdatasync` run.
