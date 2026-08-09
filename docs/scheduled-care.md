# Scheduled Memory Care

The schedule belongs to the operator; Brigade only runs when invoked. Brigade does not install cron jobs, systemd timers, GitHub Actions workflows, or any other scheduler. Copy one of the recipes below into your own crontab, user timer, or CI job after `brigade init` wires the target.

Prerequisites:

```bash
pipx install brigade-cli
pipx ensurepath   # open a new shell, or set PATH manually (see Scheduler wiring)
brigade init --depth repo --harnesses codex --target .
brigade memory care init --target .
brigade daily init --target .
brigade extras on   # once per machine; required for center report and runbook commands
```

Replace `WORKSPACE` with the absolute path to your Brigade-wired repo or operator workspace. Every `brigade` command below assumes `cd WORKSPACE` first, so `--target .` resolves correctly.

Related docs: [memory care](memory-care.md), [scanner registry](scanner-registry.md), [operator center](operator-center.md), [agents guide](agents-guide.md).

## Scheduler wiring

### PATH

Cron and systemd user units do not load your shell profile. After `pipx install brigade-cli`, resolve the binary once:

```bash
command -v brigade   # typically ~/.local/bin/brigade
```

Put that directory on `PATH` in every scheduled invocation.

Crontab header (once per crontab file):

```cron
PATH=/home/you/.local/bin:/usr/local/bin:/usr/bin:/bin
```

systemd `[Service]` block (on every Brigade unit):

```ini
Environment=PATH=/home/you/.local/bin:/usr/local/bin:/usr/bin:/bin
```

Replace `/home/you` with your account home directory. Cron cannot expand `$HOME`.

### Tracked runbooks

`brigade init` gitignores `.brigade/` by default. Nightly runbooks referenced from CI must be committed. Inside the managed brigade gitignore block, add:

```gitignore
!.brigade/runbooks/
!.brigade/runbooks/**
```

Then commit `.brigade/runbooks/nightly-maintenance.json`. Runtime receipts under `.brigade/runbooks/runs/` stay gitignored.

## GitHub Actions bootstrap

Fresh checkouts have no `.brigade/` runtime state. Scheduled workflows need a bootstrap on every run and a cache when you want scan queues, receipts, or reports to accumulate across runs.

Use this step block before the recipe commands in each workflow below:

```yaml
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'
      - run: python -m pip install --upgrade pip pipx
      - run: pipx install brigade-cli
      - run: echo "$HOME/.local/bin" >> "$GITHUB_PATH"
      - name: Bootstrap Brigade
        run: |
          brigade init --depth repo --harnesses codex --target .
          brigade memory care init --target .
          brigade daily init --target .
          brigade extras on
      - uses: actions/cache@v4
        with:
          path: .brigade
          key: brigade-${{ github.workflow }}-${{ github.ref_name }}-${{ hashFiles('.gitignore') }}
          restore-keys: |
            brigade-${{ github.workflow }}-${{ github.ref_name }}-
```

Adjust `--depth` and `--harnesses` to match your repo. `brigade init` is idempotent on an already-wired checkout.

## Daily care pass

Scan local memory cards for decay, import flagged items into the work inbox, and read the daily brief.

```bash
brigade memory care scan --target .
brigade memory care import-issues --target .
brigade work brief --target .
```

Optional follow-ups when the scan surfaces metadata gaps or stale review queues:

```bash
brigade memory care plan-fixes --target .
brigade memory care backfill --target .
brigade memory care backfill --apply --target .
brigade memory care closeout --target .
```

Foreground batch alternative when scanner producers are configured in `.brigade/scanners.toml`:

```bash
brigade work scanners init --target .
brigade work scanners run handoff-ingest --target .
brigade work sweep --scanner handoff-ingest --target .
```

On a workspace with several enabled producers, `brigade work scanners run --due --target .` and `brigade work sweep --target .` run the full due batch. Disable scanners in `.brigade/scanners.toml` until their output paths exist.

### Crontab

```cron
PATH=/home/you/.local/bin:/usr/local/bin:/usr/bin:/bin
15 6 * * * cd WORKSPACE && brigade memory care scan --target . && brigade memory care import-issues --target . && brigade work brief --target .
```

### systemd user timer

`~/.config/systemd/user/brigade-daily-care.service`:

```ini
[Unit]
Description=Brigade daily memory care pass

[Service]
Type=oneshot
WorkingDirectory=WORKSPACE
Environment=PATH=/home/you/.local/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=/bin/sh -c 'brigade memory care scan --target . && brigade memory care import-issues --target . && brigade work brief --target .'
```

`~/.config/systemd/user/brigade-daily-care.timer`:

```ini
[Unit]
Description=Run Brigade daily memory care at 06:15 local time

[Timer]
OnCalendar=*-*-* 06:15:00
Persistent=true

[Install]
WantedBy=timers.target
```

Enable with `systemctl --user daemon-reload`, then `systemctl --user enable --now brigade-daily-care.timer`.

### GitHub Actions

`.github/workflows/brigade-daily-care.yml`:

```yaml
name: brigade-daily-care
on:
  schedule:
    - cron: '15 6 * * *'
  workflow_dispatch:
jobs:
  daily-care:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'
      - run: python -m pip install --upgrade pip pipx
      - run: pipx install brigade-cli
      - run: echo "$HOME/.local/bin" >> "$GITHUB_PATH"
      - name: Bootstrap Brigade
        run: |
          brigade init --depth repo --harnesses codex --target .
          brigade memory care init --target .
          brigade daily init --target .
          brigade extras on
      - uses: actions/cache@v4
        with:
          path: .brigade
          key: brigade-${{ github.workflow }}-${{ github.ref_name }}-${{ hashFiles('.gitignore') }}
          restore-keys: |
            brigade-${{ github.workflow }}-${{ github.ref_name }}-
      - run: brigade memory care scan --target .
      - run: brigade memory care import-issues --target .
      - run: brigade work brief --target .
```

## Ingest sweep

Lint pending handoffs, then ingest safe notes into durable memory.

```bash
brigade handoff lint --strict --target .
brigade ingest --promote-cards --route-documents --target .
```

Run this on a shorter cadence when writer harnesses produce handoffs throughout the day. A runbook with `allowed_commands` restricted to `brigade` is the one-line cron shape once you pin the sequence (see [technical guide](technical-guide.md)).

### Crontab

```cron
PATH=/home/you/.local/bin:/usr/local/bin:/usr/bin:/bin
*/30 * * * * cd WORKSPACE && brigade handoff lint --strict --target . && brigade ingest --promote-cards --route-documents --target .
```

### systemd user timer

`~/.config/systemd/user/brigade-ingest-sweep.service`:

```ini
[Unit]
Description=Brigade handoff ingest sweep

[Service]
Type=oneshot
WorkingDirectory=WORKSPACE
Environment=PATH=/home/you/.local/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=/bin/sh -c 'brigade handoff lint --strict --target . && brigade ingest --promote-cards --route-documents --target .'
```

`~/.config/systemd/user/brigade-ingest-sweep.timer`:

```ini
[Unit]
Description=Run Brigade ingest sweep every 30 minutes

[Timer]
OnCalendar=*:0/30
Persistent=true

[Install]
WantedBy=timers.target
```

### GitHub Actions

`.github/workflows/brigade-ingest-sweep.yml`:

```yaml
name: brigade-ingest-sweep
on:
  schedule:
    - cron: '*/30 * * * *'
  workflow_dispatch:
jobs:
  ingest-sweep:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'
      - run: python -m pip install --upgrade pip pipx
      - run: pipx install brigade-cli
      - run: echo "$HOME/.local/bin" >> "$GITHUB_PATH"
      - name: Bootstrap Brigade
        run: |
          brigade init --depth repo --harnesses codex --target .
          brigade memory care init --target .
          brigade daily init --target .
          brigade extras on
      - uses: actions/cache@v4
        with:
          path: .brigade
          key: brigade-${{ github.workflow }}-${{ github.ref_name }}-${{ hashFiles('.gitignore') }}
          restore-keys: |
            brigade-${{ github.workflow }}-${{ github.ref_name }}-
      - run: brigade handoff lint --strict --target .
      - run: brigade ingest --promote-cards --route-documents --target .
```

## Weekly outcome ratchet

Rank verified-learning outcomes, then reconcile skill promotion state. `reconcile` is dry-run by default; pass `--apply` only when you intend to install or roll back registry skills.

```bash
brigade outcome rank --target .
brigade outcome reconcile --target .
```

Deliberate promotion or rollback:

```bash
brigade outcome reconcile --apply --target .
```

Schedule rank first, then reconcile 30 to 60 minutes later so new verify receipts land before decisions run.

### Crontab

```cron
PATH=/home/you/.local/bin:/usr/local/bin:/usr/bin:/bin
0 7 * * 1 cd WORKSPACE && brigade outcome rank --target .
30 7 * * 1 cd WORKSPACE && brigade outcome reconcile --target .
```

To apply promotions automatically after review:

```cron
30 7 * * 1 cd WORKSPACE && brigade outcome reconcile --apply --target .
```

### systemd user timer

`~/.config/systemd/user/brigade-outcome-rank.service`:

```ini
[Unit]
Description=Brigade weekly outcome rank

[Service]
Type=oneshot
WorkingDirectory=WORKSPACE
Environment=PATH=/home/you/.local/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=brigade outcome rank --target .
```

`~/.config/systemd/user/brigade-outcome-rank.timer`:

```ini
[Unit]
Description=Run Brigade outcome rank Monday 07:00 local time

[Timer]
OnCalendar=Mon *-*-* 07:00:00
Persistent=true

[Install]
WantedBy=timers.target
```

`~/.config/systemd/user/brigade-outcome-reconcile.service`:

```ini
[Unit]
Description=Brigade weekly outcome reconcile

[Service]
Type=oneshot
WorkingDirectory=WORKSPACE
Environment=PATH=/home/you/.local/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=brigade outcome reconcile --target .
```

`~/.config/systemd/user/brigade-outcome-reconcile.timer`:

```ini
[Unit]
Description=Run Brigade outcome reconcile Monday 07:30 local time

[Timer]
OnCalendar=Mon *-*-* 07:30:00
Persistent=true

[Install]
WantedBy=timers.target
```

Add `--apply` to the reconcile `ExecStart` line only after you accept automatic skill install and rollback.

### GitHub Actions

`.github/workflows/brigade-outcome-ratchet.yml`:

```yaml
name: brigade-outcome-ratchet
on:
  schedule:
    - cron: '0 7 * * 1'
  workflow_dispatch:
jobs:
  outcome-rank:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'
      - run: python -m pip install --upgrade pip pipx
      - run: pipx install brigade-cli
      - run: echo "$HOME/.local/bin" >> "$GITHUB_PATH"
      - name: Bootstrap Brigade
        run: |
          brigade init --depth repo --harnesses codex --target .
          brigade memory care init --target .
          brigade daily init --target .
          brigade extras on
      - uses: actions/cache@v4
        with:
          path: .brigade
          key: brigade-${{ github.workflow }}-${{ github.ref_name }}-${{ hashFiles('.gitignore') }}
          restore-keys: |
            brigade-${{ github.workflow }}-${{ github.ref_name }}-
      - run: brigade outcome rank --target .
  outcome-reconcile:
    needs: outcome-rank
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'
      - run: python -m pip install --upgrade pip pipx
      - run: pipx install brigade-cli
      - run: echo "$HOME/.local/bin" >> "$GITHUB_PATH"
      - name: Bootstrap Brigade
        run: |
          brigade init --depth repo --harnesses codex --target .
          brigade memory care init --target .
          brigade daily init --target .
          brigade extras on
      - uses: actions/cache@v4
        with:
          path: .brigade
          key: brigade-${{ github.workflow }}-${{ github.ref_name }}-${{ hashFiles('.gitignore') }}
          restore-keys: |
            brigade-${{ github.workflow }}-${{ github.ref_name }}-
      - run: brigade outcome reconcile --target .
```

## Daily observability

Check handoff pipeline health, read the daily driver snapshot, and build the local operator report bundle. `center report build` is extras-gated; run `brigade extras on` once per machine or prefix with `BRIGADE_EXTRAS=1`.

```bash
brigade handoff doctor --target .
brigade daily status --target .
brigade center report build --target .
```

### Crontab

```cron
PATH=/home/you/.local/bin:/usr/local/bin:/usr/bin:/bin
0 8 * * * cd WORKSPACE && brigade handoff doctor --target . && brigade daily status --target . && brigade center report build --target .
```

### systemd user timer

`~/.config/systemd/user/brigade-daily-observability.service`:

```ini
[Unit]
Description=Brigade daily observability pass

[Service]
Type=oneshot
WorkingDirectory=WORKSPACE
Environment=PATH=/home/you/.local/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=/bin/sh -c 'brigade handoff doctor --target . && brigade daily status --target . && brigade center report build --target .'
```

`~/.config/systemd/user/brigade-daily-observability.timer`:

```ini
[Unit]
Description=Run Brigade daily observability at 08:00 local time

[Timer]
OnCalendar=*-*-* 08:00:00
Persistent=true

[Install]
WantedBy=timers.target
```

### GitHub Actions

`.github/workflows/brigade-daily-observability.yml`:

```yaml
name: brigade-daily-observability
on:
  schedule:
    - cron: '0 8 * * *'
  workflow_dispatch:
jobs:
  observability:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'
      - run: python -m pip install --upgrade pip pipx
      - run: pipx install brigade-cli
      - run: echo "$HOME/.local/bin" >> "$GITHUB_PATH"
      - name: Bootstrap Brigade
        run: |
          brigade init --depth repo --harnesses codex --target .
          brigade memory care init --target .
          brigade daily init --target .
          brigade extras on
      - uses: actions/cache@v4
        with:
          path: .brigade
          key: brigade-${{ github.workflow }}-${{ github.ref_name }}-${{ hashFiles('.gitignore') }}
          restore-keys: |
            brigade-${{ github.workflow }}-${{ github.ref_name }}-
      - run: brigade handoff doctor --target .
      - run: brigade daily status --target .
      - run: brigade center report build --target .
```

## Nightly ops pass

Run an approved maintenance runbook. Pin the steps you trust, restrict `allowed_commands` to `brigade`, and keep the JSON under `.brigade/runbooks/`. Commit the runbook and add the gitignore exceptions from [Tracked runbooks](#tracked-runbooks) when CI should run the same file.

Example runbook at `.brigade/runbooks/nightly-maintenance.json`:

```json
{
  "id": "nightly-maintenance",
  "description": "Operator nightly maintenance pass.",
  "approved": true,
  "allowed_commands": ["brigade"],
  "steps": [
    {
      "id": "daily-status",
      "run": "brigade daily status --target .",
      "timeout_seconds": 120
    }
  ]
}
```

Run it (`runbook` is extras-gated; run `brigade extras on` once per machine or prefix with `BRIGADE_EXTRAS=1`):

```bash
brigade runbook run --approved --target . .brigade/runbooks/nightly-maintenance.json
```

### Crontab

```cron
PATH=/home/you/.local/bin:/usr/local/bin:/usr/bin:/bin
0 4 * * * cd WORKSPACE && brigade runbook run --approved --target . .brigade/runbooks/nightly-maintenance.json
```

### systemd user timer

`~/.config/systemd/user/brigade-nightly-ops.service`:

```ini
[Unit]
Description=Brigade nightly maintenance runbook

[Service]
Type=oneshot
WorkingDirectory=WORKSPACE
Environment=PATH=/home/you/.local/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=brigade runbook run --approved --target . .brigade/runbooks/nightly-maintenance.json
```

`~/.config/systemd/user/brigade-nightly-ops.timer`:

```ini
[Unit]
Description=Run Brigade nightly ops at 04:00 local time

[Timer]
OnCalendar=*-*-* 04:00:00
Persistent=true

[Install]
WantedBy=timers.target
```

### GitHub Actions

`.github/workflows/brigade-nightly-ops.yml`:

```yaml
name: brigade-nightly-ops
on:
  schedule:
    - cron: '0 4 * * *'
  workflow_dispatch:
jobs:
  nightly-ops:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'
      - run: python -m pip install --upgrade pip pipx
      - run: pipx install brigade-cli
      - run: echo "$HOME/.local/bin" >> "$GITHUB_PATH"
      - name: Bootstrap Brigade
        run: |
          brigade init --depth repo --harnesses codex --target .
          brigade memory care init --target .
          brigade daily init --target .
          brigade extras on
      - uses: actions/cache@v4
        with:
          path: .brigade
          key: brigade-${{ github.workflow }}-${{ github.ref_name }}-${{ hashFiles('.gitignore') }}
          restore-keys: |
            brigade-${{ github.workflow }}-${{ github.ref_name }}-
      - run: brigade runbook run --approved --target . .brigade/runbooks/nightly-maintenance.json
```

Commit the runbook JSON when you want CI to execute the same pinned steps as your laptop.

## More recipes

Add new sections here as adapters land. Future candidates include a beads reconcile pass once a `bd` adapter ships; follow the same crontab, systemd, and GitHub Actions shape as the recipes above.
