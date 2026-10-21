# Execution Model

Brigade lets agents work autonomously while the operator controls policy, budgets,
scopes, and gates. The operator can inspect or stop work. A gate is a checkpoint
placed where approval is needed, so authorized work can continue between gates
without asking for a yes at every step.

Agents leave a record of their work. Verification receipts capture commands and
real exit codes. Run journals, claims, and lineage connect actions to the task
and agent responsible. When one agent hands work to another, it can pass an
[evidence package](evidence-package.md) containing a verify receipt, alongside a
handoff with an evidence section. The receiving agent can check the record
before continuing.

## One-shot work

Most CLI commands finish after one operation. They can be called by an agent,
an operator, a runbook, or a host scheduler within the authorized scope.

| Action | Record |
| --- | --- |
| `brigade work verify run` | Receipt, command logs, exit code, and Git state under `.brigade/work/verify-runs/`. |
| `brigade run` | Run artifacts under `.brigade/runs/`, including `run.json` and lifecycle journals when enabled. Run-level repository claims use Fleet Hub when configured. |
| `brigade work claim` | Task state and claim ownership in the local work ledger under one store lock. Separate clones can each claim their local copy of a task. |
| `brigade runbook run` | Runbook metadata, step commands, exit codes, and log paths in its run folder. |
| `brigade memory care scan` | Latest scan and refresh queue under `.brigade/memory-care/`. |
| `brigade ingest` | Ingested memory or a review-queue entry, with reconciliation records. |
| `brigade receipts export package` | A portable directory with the selected verify receipt and a manifest for integrity checks. |

Read-only status commands inspect existing records. They do not create a verify
receipt merely because they ran. `brigade work ready` computes candidates and
parallel-safe waves without launching workers. Automatic wave dispatch is
planned in [#1849](https://github.com/escoffier-labs/brigade/issues/1849), run
patrol in [#1850](https://github.com/escoffier-labs/brigade/issues/1850), and memory
reflection in [#1856](https://github.com/escoffier-labs/brigade/issues/1856).

## Long-running modes

The operator starts these processes and chooses any service manager and restart
policy. Foreground servers stop with Ctrl+C. When a service manager owns a
process, stop its service through that manager to prevent a restart. There is no
separate Brigade stop subcommand for the foreground servers.

| Mode | Start command | Stop | Record or output |
| --- | --- | --- | --- |
| Fleet hub | `brigade fleet serve --host 127.0.0.1` | Ctrl+C in the serving terminal, or stop the owning service. | Accepted events, claims, and coordination state in the hub SQLite database selected by `--db`. Local run journals remain authoritative. The hub also runs its Grok Bot expiry sweeper. |
| Run View | `brigade runs serve` | Ctrl+C in the serving terminal. | Reads existing run artifacts and streams events. It does not write a view receipt. |
| Center | `BRIGADE_EXTRAS=1 brigade center serve` | Ctrl+C in the serving terminal. | Reads local status and activity records. It does not write a view receipt. |
| Managed local tool runtime | `brigade tools runtime start <runtime-id>` | `brigade tools runtime stop <runtime-id>` | PID and runtime metadata under `.brigade/tools/runtime/`, with stdout and stderr logs. Configured paths can override the PID and log locations. Stop updates metadata. |
| Memory MCP over stdio | `brigade memory serve-mcp --stdio` | The MCP host closes stdin or terminates its child. Ctrl+C also stops an interactive process. | Read-only card responses over stdout. No per-read verification receipt. |
| Memory MCP over HTTP | `brigade memory serve-mcp --http` | Ctrl+C, or stop the owning service. | Read-only card responses over loopback HTTP. No per-read verification receipt. |
| Skills MCP over stdio | `brigade skills serve-mcp --stdio` | The MCP host closes stdin or terminates its child. Ctrl+C also stops an interactive process. | Read-only registry responses over stdout. No per-read verification receipt. |
| Skills MCP over HTTP | `brigade skills serve-mcp --http` | Ctrl+C, or stop the owning service. | Read-only registry responses over loopback HTTP. No per-read verification receipt. |
| Grok Bot MCP listener | `brigade run cloud grokbot serve --instance operator` or `brigade run cloud grokbot serve --pack <pack-id>` | Ctrl+C, or stop the owning service. | Role listeners journal call decisions to stderr and job transitions to the hub. Packs use their own records, such as the T3 dispatch SQLite ledger. |
| Run watch | `brigade runs watch <run-id>` | Ends when the run reaches a terminal state, or stop with Ctrl+C. | Prints observations from run artifacts, or newline-delimited JSON with `--json`. It can recover stale artifact collection and update the affected run's records. |

The shipped hub command is `brigade fleet serve`. The listener requires its
configured credentials. Grok Bot listeners also require role or pack setup and
protected bearer configuration. The HTTP memory and skills servers require
`BRIGADE_MCP_HTTP_TOKEN`. See the [Grok Bot operating guide](grokbot-operating-guide.md)
and [MCP transport guide](technical-guide.md#streamable-http-for-the-read-only-mcp-servers)
for setup. These commands do not enable every service during installation.

## Receipts and provenance

Mutating commands leave evidence under `.brigade/`, such as scan outputs, import
queues, verification receipts, and runbook run folders. Not every command writes
a receipt. For example, `memory care scan` updates `scan-latest.json` and
`refresh-queue.json` without emitting a receipt file. Runbook receipts record
runbook metadata and per-step `run` strings with exit codes and log paths, rather
than a top-level `argv` field. Memory-care `status` and `doctor` read the latest
scan and queue artifacts. They do not read runbook receipts.

Evidence packages copy selected verification artifacts and bind their bytes to
a manifest. They do not establish that a claim is true beyond what the receipt
observed. Handoffs explain the result and remaining work, with evidence links
that another agent can inspect. Lineage records connect related work without
turning free-text claims into verification.

## Scheduled care

Cron, systemd timers, launchd, CI schedules, or another operator-owned trigger
can call Brigade commands or approved runbooks. The operator chooses the
schedule, host, and restart policy. Each runbook run records its steps and their
exit codes even when the operator is away.

`brigade care install --target .` opts the target into namespaced systemd user
timers on Linux or launchd agents on macOS. Windows prints Task Scheduler
commands and returns exit 3 without creating tasks. Other platforms report
managed scheduling as unsupported. Each systemd or launchd registration
includes a hash of the absolute target path. Repeatable `--entry JOB_ID`
selects one job for install, status, or uninstall. Omitting it selects the
scheduled-care set. `memory-refresh` and `evidence-crawl` require their
operator-approved runbooks before installation.

`brigade care status` audits registrations and shows the latest runbook receipt
for each recipe. `brigade care uninstall` removes the target's hash-stamped
systemd or launchd registrations, or its managed crontab block. These care jobs
use host scheduling independently of the long-running services above. See
[scheduled care](scheduled-care.md).

## Related

- [Evidence packages](evidence-package.md): portable verification artifacts and integrity checks
- [Memory care](memory-care.md): card scans, refresh queues, and import records
- [Scheduled care](scheduled-care.md): host scheduler recipes and `brigade care`
- [Scanner registry](scanner-registry.md): configured scanner commands and run records
- [Technical guide](technical-guide.md): full command surface and policy boundaries
- [Roadmap](../ROADMAP.md): direction and planned work
