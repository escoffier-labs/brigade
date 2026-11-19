# Scheduled Memory Care

The operator chooses when scheduled care runs and which runbooks it may execute.
Each runbook run records step exit codes and log paths for later inspection. Copy
one of the recipes below into your own crontab, user timer, or CI job after
`brigade init` wires the target, or explicitly opt in with
`brigade care install --target .`. With the default `auto` backend it installs
systemd user timers on Linux or launchd agents on macOS. On Linux, install also
links every timer it wrote into `timers.target.wants`, reloads systemd, and starts
each newly linked timer; pass `--no-enable` to write unit files without the links. On Windows it prints
a Task Scheduler plan without creating tasks and exits 3. Other platforms report
an unsupported scheduler. Use the manual crontab recipes on this page
when cron is your trigger.
Systemd and launchd registrations are namespaced by a hash of the absolute target path, so
status and uninstall operate only on that target. Uninstalling a missing
registration is a clear no-op. These care registrations use the host scheduler.
Brigade also has operator-started long-running fleet and MCP services, described
in the [execution model](execution-model.md).

Prerequisites:

```bash
pipx install brigade-cli
pipx ensurepath   # open a new shell, or set PATH manually (see Scheduler wiring)
brigade init --depth repo --harnesses codex --target .
brigade memory care init --target .
brigade daily init --target .
brigade extras on   # once per machine; required for center report and runbook commands
```

Replace `WORKSPACE` with the absolute path to your Brigade-wired repo or operator workspace. Every `brigade` command below assumes `cd` into that directory first, so `--target .` resolves correctly. In crontab lines, quote the path (`cd "WORKSPACE"`) so spaces and shell metacharacters do not break the job.

### brigade care (opt-in scaffold)

```bash
brigade memory care init --with-runbooks --target .
brigade extras on   # runbook + center report are extras-gated
brigade care install --target .
brigade care install --target . --entry handoff-ingest --entry care-scan
brigade care install --target . --entry care-scan --schedule 'care-scan=*-*-* 07:15:00'
brigade care status --target . --entry care-scan
# brigade care uninstall --target . --entry handoff-ingest
# brigade care uninstall --target .
```

`care install` scaffolds the recipes on this page. Daily care, ingest sweep, and
weekly outcome ratchet invoke the shipped memory-care runbooks under
`.brigade/memory-care/runbooks/` so each fire writes a runbook receipt. Weekly
outcome uses the combined runbook at Monday 07:00 (rank then reconcile in one
approved run) instead of the 30-minute gap in the hand-written crontab below.
Daily observability uses the shipped runbook at
`.brigade/memory-care/runbooks/daily-observability.json` so each fire writes a
receipt; nightly ops calls
`.brigade/runbooks/nightly-maintenance.json` (operator-authored). Entries live
inside a `# BEGIN BRIGADE CARE ... hash:...` managed block so status can detect
drift and uninstall can remove only Brigade's span.

On the systemd backend, install enables each timer it writes by creating a
relative `timers.target.wants/<timer>` symlink to `../<timer>`. A new link only
takes effect when `timers.target` next starts, so after linking, install runs
`systemctl --user daemon-reload` and then `systemctl --user start <timer>` for each
newly linked timer, best-effort. If `systemctl` is missing or fails, the link stays
and the payload records `started: false` with the error per timer, and
`next_commands` prints the exact `daemon-reload` and `start` commands to run.

A timer is not linked when its unit was preserved, when the entry was adopted from
an installed predecessor, when the unit is not a Brigade-owned managed block, when
the timer file is absent, or under `--no-enable`. A timer that already has its link
is reported as enabled in every one of those cases, and `--no-enable` does not
remove an existing link. An existing foreign entry at the link path, or a
`timers.target.wants` that is a symlink or resolves outside the unit directory, is
reported as a conflict and nothing is written. Reinstalling an already-linked timer
changes nothing and calls no `systemctl`. `--dry-run` reports the planned per-timer
`enable` action and calls no `systemctl`. The JSON payload lists the result under
`enable`, and `next_commands` adds `systemctl --user enable --now <timer>` for every
timer that is still disabled.

`brigade care uninstall` stops each timer it removes (`systemctl --user stop`,
best-effort) and removes `timers.target.wants/<timer>` only when that entry is a
symlink to the unit being removed, so a later `install --no-enable` does not
silently re-enable it. Foreign entries are left alone and a symlinked
`timers.target.wants` is not followed. The result is under `enable_links` in the
payload, and `--dry-run` reports it without changing anything.

`brigade care status`, `brigade work brief`, and `brigade doctor` report schedule
health from installed unit files, LaunchAgents plists, and runbook receipts,
without running `systemctl`. You get one warning per installed entry whose timer is
disabled, one when an enabled timer or LaunchAgent has been enabled for longer
than its schedule period and no run has ever written a receipt (with `brigade
extras` on), and one when runbook-backed entries are enabled while `brigade extras`
is off (those runs exit 2 without a receipt). A timer's age counts from its
`timers.target.wants` link, so a long-installed timer that was just enabled does
not warn. A crontab is read only when its spool file is readable
without a subprocess; otherwise `brigade doctor` says crontab was not inspected
instead of reporting it healthy, and with nothing installed it reports that no
schedule was found. The status aggregate `enabled` is false while any installed
timer is disabled. Each warning names the exact fix command.

`--schedule JOB_ID=VALUE` is repeatable and applies only to entries selected by
`--entry` (or to entries in the default set). Its value uses the selected
scheduler's syntax: `OnCalendar` syntax for systemd and the supported cron
subset for launchd and Task Scheduler. A later `care install` reads the
schedule from an existing managed systemd unit or launchd plist and keeps it
unless that entry gets another explicit `--schedule` value.
The schedule is stored in the registration itself, not in a separate Brigade
preferences file.

For systemd, `care status --json` reports both `managed_calendar`, read from
Brigade's timer file, and `effective_calendar`, read from
`systemctl --user show ... TimersCalendar` after systemd resolves drop-ins. A
different effective value sets `schedule_diverged`. If `evidence-crawl` shares
an effective calendar with the known `brigadeclaw-daily-report` timer, status
adds a `brigade-evidence-calendar-collision` warning. The warning is advisory and
does not disable either timer.

To install one job without the rest of that set, pass `--entry JOB_ID`
(repeatable). The five maintainer memory jobs from the care-managed inventory
are `handoff-ingest`, `care-scan`, `memory-refresh`, `evidence-crawl`, and
`memory-closeout`. `memory-refresh` and `evidence-crawl` require an
operator-approved runbook at `.brigade/memory-care/runbooks/<job-id>.json`; if
that file is missing, install writes nothing and says why. Omitting `--entry`
still installs the default set: `daily-care`, `ingest-sweep`, `weekly-outcome-ratchet`, `daily-observability`, `nightly-ops` and `memory-reflect`. Status and uninstall take the
same selector, and namespaced backends still key registrations by
`(target identity, job_id)`. Per-job installs support systemd and launchd. The
crontab examples below remain operator-owned manual recipes.

Re-enable `memory-closeout` only after reviewing a dry-run of the current
queue. Policy recorded next to the shipped runbook: **review**, not `--defer`.
Preview with `brigade memory care closeout --dry-run --json --target .`, then
`brigade care install --target . --entry memory-closeout`. The scheduled step
stays `brigade memory care closeout --target .` and still refuses a nonempty
queue. Do not pass `--defer` unless an operator explicitly accepts the backlog
with a nonblank `--reason`. After the timer fires, `brigade care status`
should show a completed receipt when the queue is empty (or an explicit defer
was chosen).

### Run patrol

`run-patrol` is an optional care entry on systemd and launchd. Its default
schedule is every 10 minutes. Install one timer with:

```bash
brigade care install --target . --entry run-patrol --schedule 'run-patrol=*:0/10'
brigade care status --target . --entry run-patrol --json
```

The schedule above uses systemd syntax. For launchd use
`--schedule 'run-patrol=*/10 * * * *'`. On Windows the same entry prints a
Task Scheduler plan with `/SC MINUTE /MO 10` and exits 3, like the other entries.

Installation writes `.brigade/runbooks/run-patrol.json` only when run-patrol is
selected. It contains one report-only
step:

```bash
brigade runs patrol --target . --json --receipt --fail-on stalled,zombie,dead
```

To enable repairs, review the generated runbook and add an explicit
`--apply reap,release-claim` to its step. `escalate` can also be selected.
`finalize-intent` is not implemented. Reinstall preserves an existing runbook,
except that a pristine copy of a past release is refreshed (see
[Shipped runbooks and upgrades](#shipped-runbooks-and-upgrades)).
Patrol retains the newest 144 receipts for at most 7 days, including under
`--apply`. Compact action fences survive pruning in
`.brigade/patrol/.action-fences/`, with one file per lane. Apply reads only the
scanned lanes' fence files and at most the newest 144 receipts. Its first
retention sweep also migrates the legacy `.action-fences.json` archive. Fences
retain the lifetime escalation cap and have no shared size ceiling across lanes.
Care status checks at most the newest 10 patrol receipt candidates and includes the latest valid patrol
receipt and the latest runbook receipt.
Two consecutive failed runbook receipts use care's existing repeated-failure
reporting in work brief and Center. A selected patrol finding returns 1 and
therefore fails that scheduled runbook step.

The crontab backend keeps one atomic managed block. `care install --backend
crontab` without `--entry` includes run-patrol in that block. `--entry
run-patrol` still refuses crontab. Systemd and launchd default installs retain
the same default set until run-patrol is explicitly selected.
Existing CARE crontab blocks show as stale after upgrading, and `brigade care install --target .` refreshes the managed block.

Related docs: [memory care](memory-care.md), [scanner registry](scanner-registry.md), [operator center](operator-center.md), [agents guide](agents-guide.md), [execution model](execution-model.md).

## Scheduler wiring

### PATH

Cron and systemd user units do not load your shell profile. After `pipx install brigade-cli`, resolve the binary once:

```bash
command -v brigade   # typically ~/.local/bin/brigade
```

Put that directory on `PATH` in every scheduled invocation.

Crontab header (once per crontab file):

```cron
PATH=/ABSOLUTE_ACCOUNT_HOME/.local/bin:/usr/local/bin:/usr/bin:/bin
```

systemd `[Service]` block (on every Brigade unit):

```ini
Environment=PATH=/ABSOLUTE_ACCOUNT_HOME/.local/bin:/usr/local/bin:/usr/bin:/bin
```

Replace `/ABSOLUTE_ACCOUNT_HOME` with your account's real absolute home directory before installing these entries. The placeholder is not a shell variable; cron cannot expand `$HOME` in these assignments.

### systemd user timers

User timers stop when you log out unless lingering is enabled. Run once per account:

```bash
sudo loginctl enable-linger "$USER"
```

Then `systemctl --user daemon-reload`, `systemctl --user list-timers` to confirm the
next fire time, and `systemctl --user enable --now <timer>.timer` only for a timer
you wrote by hand. Timers written by `brigade care install` already carry the
`timers.target.wants` link and are started by install, so they need nothing more
unless install printed a `start` or `daemon-reload` command for them.

### Tracked runbooks

`brigade init` adds a managed gitignore block that keeps `.brigade/*` out of git by default. Nightly runbooks referenced from CI must be committed. Inside that block, add:

```gitignore
!.brigade/runbooks/
!.brigade/runbooks/**
.brigade/runbooks/runs/
```

The final line re-ignores runtime receipts after the `**` un-ignore. Then commit `.brigade/runbooks/nightly-maintenance.json`. Receipts under `.brigade/runbooks/runs/` stay out of git.

## GitHub Actions bootstrap

Fresh checkouts have no `.brigade/` runtime state. Scheduled workflows need a bootstrap on every run and a cache when you want scan queues, receipts, or reports to accumulate across runs.

Use this step block before the recipe commands in each workflow below. Restore the `.brigade` cache before bootstrap so scan queues, receipts, and reports survive across runs:

```yaml
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'
      - run: python -m pip install --upgrade pip pipx
      - run: pipx install brigade-cli
      - run: echo "$HOME/.local/bin" >> "$GITHUB_PATH"
      - uses: actions/cache@v4
        with:
          path: .brigade
          key: brigade-${{ github.workflow }}-${{ github.ref_name }}-${{ github.run_id }}
          restore-keys: |
            brigade-${{ github.workflow }}-${{ github.ref_name }}-
      - name: Bootstrap Brigade
        run: |
          brigade init --depth repo --harnesses codex --target .
          brigade memory care init --target .
          brigade daily init --target .
          brigade extras on
```

The cache `key` includes `${{ github.run_id }}` so each run writes a fresh entry. `restore-keys` keeps the stable workflow/ref prefix so the latest prior `.brigade` state restores on the next run.

Adjust `--depth` and `--harnesses` to match your repo. `brigade init` is idempotent on an already-wired checkout.

## Daily care pass

Scan local memory cards for decay, import flagged items into the work inbox, and read the daily brief.

```bash
brigade memory care scan --target .
brigade memory care import-issues --target .
brigade work brief --target .
brigade memory care closeout --target .
```

Optional follow-ups when the scan surfaces metadata gaps or stale review queues:

```bash
brigade memory care plan-fixes --target .
brigade memory care backfill --target .
brigade memory care backfill --apply --target .
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
PATH=/ABSOLUTE_ACCOUNT_HOME/.local/bin:/usr/local/bin:/usr/bin:/bin
15 6 * * * cd "WORKSPACE" && brigade memory care scan --target . && brigade memory care import-issues --target . && brigade work brief --target . && brigade memory care closeout --target .
```

### systemd user timer

`~/.config/systemd/user/brigade-daily-care.service`:

```ini
[Unit]
Description=Brigade daily memory care pass

[Service]
Type=oneshot
WorkingDirectory=WORKSPACE
Environment=PATH=/ABSOLUTE_ACCOUNT_HOME/.local/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=/bin/sh -c 'brigade memory care scan --target . && brigade memory care import-issues --target . && brigade work brief --target . && brigade memory care closeout --target .'
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

Enable with `systemctl --user daemon-reload`, then `systemctl --user enable --now brigade-daily-care.timer`. See [systemd user timers](#systemd-user-timers) for `loginctl enable-linger`.

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
      - uses: actions/cache@v4
        with:
          path: .brigade
          key: brigade-${{ github.workflow }}-${{ github.ref_name }}-${{ github.run_id }}
          restore-keys: |
            brigade-${{ github.workflow }}-${{ github.ref_name }}-
      - name: Bootstrap Brigade
        run: |
          brigade init --depth repo --harnesses codex --target .
          brigade memory care init --target .
          brigade daily init --target .
          brigade extras on
      - run: brigade memory care scan --target .
      - run: brigade memory care import-issues --target .
      - run: brigade work brief --target .
      - run: brigade memory care closeout --target .
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
PATH=/ABSOLUTE_ACCOUNT_HOME/.local/bin:/usr/local/bin:/usr/bin:/bin
*/30 * * * * cd "WORKSPACE" && brigade handoff lint --strict --target . && brigade ingest --promote-cards --route-documents --target .
```

### systemd user timer

`~/.config/systemd/user/brigade-ingest-sweep.service`:

```ini
[Unit]
Description=Brigade handoff ingest sweep

[Service]
Type=oneshot
WorkingDirectory=WORKSPACE
Environment=PATH=/ABSOLUTE_ACCOUNT_HOME/.local/bin:/usr/local/bin:/usr/bin:/bin
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
      - uses: actions/cache@v4
        with:
          path: .brigade
          key: brigade-${{ github.workflow }}-${{ github.ref_name }}-${{ github.run_id }}
          restore-keys: |
            brigade-${{ github.workflow }}-${{ github.ref_name }}-
      - name: Bootstrap Brigade
        run: |
          brigade init --depth repo --harnesses codex --target .
          brigade memory care init --target .
          brigade daily init --target .
          brigade extras on
      - run: brigade handoff lint --strict --target .
      - run: brigade ingest --promote-cards --route-documents --target .
```

## Weekly outcome ratchet

The weekly ratchet is the scheduled half of the automated scoring loop described in [Outcome scoring](outcome-scoring.md#the-default-loop). It is part of the default care set, so `brigade care install --target .` with no `--entry` installs it. `brigade init` does not install it, so a new workspace has no ratchet until you run that command. To schedule only the ratchet:

```bash
brigade extras on   # once per machine, runbook steps are extras-gated
brigade care install --target . --entry weekly-outcome-ratchet
```

### What install writes

On Linux, install writes `brigade-care-<hash>-weekly-outcome-ratchet.service` and `.timer` under `~/.config/systemd/user/`, links the timer into `timers.target.wants` and starts it (see [brigade care](#brigade-care-opt-in-scaffold) above). On macOS it writes a LaunchAgent. It also writes the shipped runbook `.brigade/memory-care/runbooks/weekly-outcome-ratchet.json` when that file is missing. The default schedule is Monday at 07:00 local time (`Mon *-*-* 07:00:00`). Change it with `--schedule 'weekly-outcome-ratchet=<value>'`.

### What it runs

The runbook has two steps, run in order in one approved run:

```bash
brigade outcome rank --target .
brigade outcome reconcile --target . --report --proposals
```

Both steps are dry runs. `reconcile` is dry-run by default and the runbook never passes `--apply`, so a scheduled run changes no skill status, no route policy and no skill file. It evaluates every scored artifact (it does not read `rank` output), writes the advisory report, and `--proposals` files a skills inbox review request for a verified regression on a Brigade-bundled skill, at most one per skill text (see [Outcome scoring](outcome-scoring.md#regression-proposals)). A proposal is a review request only.

`--apply` is routing only and stays a deliberate operator choice. It records promotion and demotion status and route policy. It never installs or rolls back a skill file, and it lists the `brigade skills install` or `brigade skills rollback` commands an operator would run (also counted as `pending_physical_actions` in the ratchet report and the session brief):

```bash
brigade outcome reconcile --apply --target .
```

### Where the output appears

`--report` writes `.brigade/memory-care/outcome/ratchet-report.json`, a versioned summary of that run: decision counts by action, regression holds and rollbacks with their reason codes and helped/hurt counts, the receipt ineligibility rate, and the leading ineligibility reason. The report is advisory and never changes a status. It shows up in three places with no command:

- `brigade work brief` prints one `outcome_ratchet:` line.
- The Center dashboard outcomes view shows the same summary below the ranking and marks a report older than seven days as stale.
- `brigade outcome ratchet --target .` prints the last report without ranking or reconciling.

See [Outcome scoring](outcome-scoring.md#weekly-ratchet-report) for the fields.

### How to check it ran

```bash
brigade care status --target . --entry weekly-outcome-ratchet
brigade outcome ratchet --target .
```

`care status` should show `enabled: True`, an empty `schedule_warnings` list and a `last_receipt` of `completed@<time>`. A `last_receipt` of `none` right after install means the timer has not fired yet. After the timer has been enabled for longer than its period without a receipt, `care status`, `brigade work brief` and `brigade doctor` warn `never-ran` and name the fix. `outcome ratchet` prints `report: none` until the first run writes a report, and its `generated_at` shows the age of the last one. To run it now, use `systemctl --user start brigade-care-<hash>-weekly-outcome-ratchet.service` (`brigade care status --json` lists the unit names) or `brigade runbook run --approved .brigade/memory-care/runbooks/weekly-outcome-ratchet.json`.

If you schedule the ratchet by hand instead, pass `--report` to `reconcile` so the brief and dashboard have a report to show. Schedule rank first, then reconcile 30 to 60 minutes later so new verify receipts land before decisions run.

### Crontab

```cron
PATH=/ABSOLUTE_ACCOUNT_HOME/.local/bin:/usr/local/bin:/usr/bin:/bin
0 7 * * 1 cd "WORKSPACE" && brigade outcome rank --target .
30 7 * * 1 cd "WORKSPACE" && brigade outcome reconcile --target . --report --proposals
```

Scheduling `--apply` is not part of the default setup. It records status and route policy only, so read a dry run first if you add it:

```cron
30 7 * * 1 cd "WORKSPACE" && brigade outcome reconcile --apply --target .
```

### systemd user timer

`~/.config/systemd/user/brigade-outcome-rank.service`:

```ini
[Unit]
Description=Brigade weekly outcome rank

[Service]
Type=oneshot
WorkingDirectory=WORKSPACE
Environment=PATH=/ABSOLUTE_ACCOUNT_HOME/.local/bin:/usr/local/bin:/usr/bin:/bin
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
Environment=PATH=/ABSOLUTE_ACCOUNT_HOME/.local/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=brigade outcome reconcile --target . --report --proposals
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

Add `--apply` to the reconcile `ExecStart` line only after you accept automatic status and route policy changes. It never installs or rolls back skill files.

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
      - uses: actions/cache@v4
        with:
          path: .brigade
          key: brigade-${{ github.workflow }}-${{ github.ref_name }}-${{ github.run_id }}
          restore-keys: |
            brigade-${{ github.workflow }}-${{ github.ref_name }}-
      - name: Bootstrap Brigade
        run: |
          brigade init --depth repo --harnesses codex --target .
          brigade memory care init --target .
          brigade daily init --target .
          brigade extras on
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
      - uses: actions/cache@v4
        with:
          path: .brigade
          key: brigade-${{ github.workflow }}-${{ github.ref_name }}-${{ github.run_id }}
          restore-keys: |
            brigade-${{ github.workflow }}-${{ github.ref_name }}-
      - name: Bootstrap Brigade
        run: |
          brigade init --depth repo --harnesses codex --target .
          brigade memory care init --target .
          brigade daily init --target .
          brigade extras on
      - run: brigade outcome reconcile --target . --report --proposals
```

The GitHub Actions recipe runs reconcile immediately after rank in the same workflow (`needs: outcome-rank`), not 30 to 60 minutes later. That is fine for CI: both jobs share the same restored `.brigade` cache and rank only reads receipts already on disk. On a laptop, keep the cron or timer gap so verify runs between rank and reconcile can finish first.

## Daily observability

Check handoff pipeline health, read the daily driver snapshot, and build the local operator report bundle. `center report build` is extras-gated. Run `brigade extras on` once per machine or prefix with `BRIGADE_EXTRAS=1`.

`brigade care install` materializes `.brigade/memory-care/runbooks/daily-observability.json` and schedules it as a runbook so each fire writes a normal runbook receipt.

```bash
brigade runbook run --approved .brigade/memory-care/runbooks/daily-observability.json --target .
```

The runbook steps are:

```bash
brigade handoff doctor --target .
brigade daily status --target .
brigade center report build --target .
```

### Crontab

```cron
PATH=/ABSOLUTE_ACCOUNT_HOME/.local/bin:/usr/local/bin:/usr/bin:/bin
0 8 * * * cd "WORKSPACE" && brigade runbook run --approved ".brigade/memory-care/runbooks/daily-observability.json" --target .
```

### systemd user timer

`~/.config/systemd/user/brigade-daily-observability.service`:

```ini
[Unit]
Description=Brigade daily observability pass

[Service]
Type=oneshot
WorkingDirectory=WORKSPACE
Environment=PATH=/ABSOLUTE_ACCOUNT_HOME/.local/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=brigade runbook run --approved .brigade/memory-care/runbooks/daily-observability.json --target .
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
      - uses: actions/cache@v4
        with:
          path: .brigade
          key: brigade-${{ github.workflow }}-${{ github.ref_name }}-${{ github.run_id }}
          restore-keys: |
            brigade-${{ github.workflow }}-${{ github.ref_name }}-
      - name: Bootstrap Brigade
        run: |
          brigade init --depth repo --harnesses codex --target .
          brigade memory care init --target .
          brigade daily init --target .
          brigade extras on
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

Run it (`runbook` is extras-gated. Run `brigade extras on` once per machine or prefix with `BRIGADE_EXTRAS=1`):

```bash
brigade runbook run .brigade/runbooks/nightly-maintenance.json --target . --approved
```

### Crontab

```cron
PATH=/ABSOLUTE_ACCOUNT_HOME/.local/bin:/usr/local/bin:/usr/bin:/bin
0 4 * * * cd "WORKSPACE" && brigade runbook run .brigade/runbooks/nightly-maintenance.json --target . --approved
```

### systemd user timer

`~/.config/systemd/user/brigade-nightly-ops.service`:

```ini
[Unit]
Description=Brigade nightly maintenance runbook

[Service]
Type=oneshot
WorkingDirectory=WORKSPACE
Environment=PATH=/ABSOLUTE_ACCOUNT_HOME/.local/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=brigade runbook run .brigade/runbooks/nightly-maintenance.json --target . --approved
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
      - uses: actions/cache@v4
        with:
          path: .brigade
          key: brigade-${{ github.workflow }}-${{ github.ref_name }}-${{ github.run_id }}
          restore-keys: |
            brigade-${{ github.workflow }}-${{ github.ref_name }}-
      - name: Bootstrap Brigade
        run: |
          brigade init --depth repo --harnesses codex --target .
          brigade memory care init --target .
          brigade daily init --target .
          brigade extras on
      - run: brigade runbook run .brigade/runbooks/nightly-maintenance.json --target . --approved
```

Commit the runbook JSON when you want CI to execute the same pinned steps as your laptop.

## More recipes

Add new sections here as adapters land. Future candidates include a beads reconcile pass once a `bd` adapter ships. Follow the same crontab, systemd, and GitHub Actions shape as the recipes above.

### Hub memory reflection

`brigade memory care install --entry memory-reflect --target <workspace>`
installs a daily 10:00 reflection entry through the existing care scheduler.
The default care set now includes this entry. It runs
`memory reflect run --scheduled --apply`, selecting configured hub memory or
local candidates. A missing workspace reflect policy is created with Tier 0
application enabled. Existing policies, including an off switch, are preserved.
Hub authority gates decide whether each proposal can apply. Gate holds stay
proposal-only without failing care. Local authority gates remain unbound.
Use `brigade memory reflect auto-apply off --target .` to stop application
before the next candidate. See [Automatic Tier 0 reflection](memory-care.md#automatic-tier-0-reflection).

Three consecutive non-zero reflect runs pause the job in workspace state.
Scheduled triggers then skip hub work. Inspect `memory reflect status`,
`memory care status`, or `care status --entry memory-reflect`. Fix the fault
and run `memory reflect run --hub --target <workspace>` manually. A successful
non-dry manual run resumes scheduled work. Dry runs and reinstalls leave the
pause set. The timer sets only `PATH`, so configure user-level `[memory_hub]`
settings or import the memory hub environment pair into the user service manager.
See [Scheduled hub reflection](memory-operations.md#scheduled-hub-reflection-s19d)
for grants, credentials, rollback and scheduler activation.

## Shipped runbooks and upgrades

`brigade care install` and `brigade memory care init --with-runbooks` place the
shipped runbooks (`daily-care-pass`, `ingest-sweep`, `lesson-review`,
`weekly-outcome-ratchet`, `daily-observability`, `memory-closeout`, and
`run-patrol` when selected). An existing regular file is judged by its content
without the top-level `pins` block (and only that block):

| Installed copy | Result |
| --- | --- |
| Same as the bundled text | Left alone. A different `pins` block alone is not a difference. |
| Same as a text a past release shipped for that runbook | Replaced with the current text and fresh pin hashes, no backup. An installed `version_cmd` on a pin is kept and its `version` re-observed. Printed as `refreshed (pristine): <path>`. A pin with any other field, or a `version_cmd` for a command the template no longer runs, would be lost, so such a copy is kept as `kept (drifted)`. |
| Anything else (edited, unparseable, unknown) | Never overwritten. Printed once as `kept (drifted): <path>`. |

The past-release record is `src/brigade/templates/shipped-file-history.json`,
generated by `scripts/shipped_file_history.py` and checked by `scripts/verify`. A
missing, linked or malformed record means nothing is refreshed. The copy is
re-read without following a link right before the replace and kept if it changed
meanwhile; a symlink or non-regular file at a runbook name is still refused with
exit 2. With `--json` the two lines go to stderr so stdout stays one document. A
dry run prints `would refresh (pristine)` and writes nothing. The explicit
`memory care init --force --with-runbooks` keeps its own rule: any differing
shipped file is copied to `<name>.bak-<UTC timestamp>` and replaced.
