# Repo Fleet

`brigade repos` inspects configured local repositories from gitignored `.brigade/repos.toml`. It is a local operator workflow for safe labels, readiness checks, report bundles, and reviewed fleet actions.

Commands:

```bash
brigade repos init
brigade repos list
brigade repos show <repo-id>
brigade repos scan
brigade repos doctor
brigade repos import-issues
brigade repos sweep plan
brigade repos sweep run
brigade repos sweep runs
brigade repos sweep show <sweep-id>
brigade repos sweep closeout <sweep-id|latest>
brigade repos report plan
brigade repos report build
brigade repos report list
brigade repos report show <report-id>
brigade repos report archive <report-id>
brigade repos report closeout <report-id>
brigade repos actions plan <report-id>
brigade repos actions build <report-id>
brigade repos actions list
brigade repos actions show <fleet-action-id>
brigade repos actions start <fleet-action-id>
brigade repos actions done <fleet-action-id>
brigade repos actions defer <fleet-action-id> --reason "not today"
brigade repos actions archive --completed
```

Fleet reports are written under:

```text
.brigade/repos/reports/
```

Fleet action queues are written under:

```text
.brigade/repos/actions/
```

Fleet sweep receipts are written under:

```text
.brigade/repos/sweeps/
```

Fleet reports include safe repo ids, safe labels, status counts, blocker and warning counts, top pending action summaries, receipt labels, and suggested next commands. Fleet actions store local metadata only: repo id, safe label, source subsystem, source local id, status, priority or severity, safe summary, suggested command, timestamps, and source fingerprint.

`brigade repos sweep plan` shows which configured repos would be refreshed and which read-only local Brigade commands would run. `brigade repos sweep run` executes the configured foreground refresh for selected repos, records per-repo command summaries, and stores raw stdout and stderr only in gitignored local logs. Receipt JSON uses repo ids, safe labels, command labels, status counts, fingerprints, and local log labels. It does not store exact repo paths.

Repos may define optional read-only health commands in gitignored config:

```toml
[[repo.health_command]]
label = "local-health"
command = "python3 -m brigade work brief --json"
timeout = 120
```

Health commands are parsed into argv and run without a shell. High-risk shell, remote-copy, and metacharacter-heavy command shapes are rejected before a sweep runs.

Sweep filters include `--repo <repo-id>`, `--all`, `--stale-only`, `--include-disabled`, and `--force`. `brigade repos sweep closeout` marks a sweep as `reviewed`, `deferred`, `superseded`, or `archived` after the operator has inspected the refreshed evidence. Fleet reports, center status, center reviews, work brief, work doctor, and release doctor surface stale, failed, or unclosed fleet sweeps.

`brigade repos actions build` requires the source fleet report to be closed out as `reviewed` or `deferred` unless `--allow-unreviewed` is passed. Repeated builds dedupe by repo id, report fingerprint, and source item fingerprint, including archived actions from the same report.

Privacy boundaries:

- No cloning.
- No remote mutation.
- No automatic action execution.
- No automatic promotion or dismissal.
- No exact private repo names, owner names, org names, hostnames, local paths, raw logs, scanner output, private config contents, secrets, or raw evidence in public files.
- Gitignored local config may store local paths and safe labels.
