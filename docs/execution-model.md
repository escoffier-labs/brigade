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
| `brigade work dispatch` | Plan, session, per-task intents, and digest-chained event receipts under `.brigade/dispatch/`. Each child has a linked worktree and run artifacts. Successful tasks stay claimed and `in_progress` for review and landing. |
| `brigade work claim` | Task state and claim ownership in the local work ledger under one store lock. Separate clones can each claim their local copy of a task. |
| `brigade runbook run` | Runbook metadata, step commands, exit codes, and log paths in its run folder. |
| `brigade memory care scan` | Latest scan and refresh queue under `.brigade/memory-care/`. |
| `brigade ingest` | Ingested memory or a review-queue entry, with reconciliation records. |
| `brigade receipts export package` | A portable directory with the selected verify receipt and a manifest for integrity checks. |

Read-only status commands inspect existing records. They do not create a verify
receipt merely because they ran. `brigade work ready` computes candidates and
parallel-safe waves without launching workers. `brigade work dispatch` claims
and launches one ready wave, then supervises its children until they finish.
`--no-wait` returns after launch. A later invocation reconciles those children
and fills available capacity before waiting. Hub summary mirroring remains
follow-up work. Initial run-patrol and memory-reflection slices are available
since 0.31.0. Remaining work is tracked in
[#1850](https://github.com/escoffier-labs/brigade/issues/1850) and
[#1856](https://github.com/escoffier-labs/brigade/issues/1856).

## Kinds of delegated work

Five mechanisms hand work to another actor. They differ in who owns execution,
where the record lives, and how much Brigade governance covers the far side.
Only the ledger, run-lineage, and batch rows are Brigade-owned today. The ledger
and batch rows launch workers. The run-lineage row creates a branched run record
for later use.

| Mechanism | Who creates it | Who owns execution | Where identity and results are recorded | Started by | Current status |
| --- | --- | --- | --- | --- | --- |
| T3 app-owned child task | A parent T3 session | The T3 app: selected environment, app-owned worktree, provider run, cancellation, and completion wake | Native T3 task, thread, and run identifiers plus the completion wake. Brigade records no governed task or result. The optional [thread-presence observer](fleet-t3-observer.md) publishes operational metadata only. | Native `delegate_task` tool in the parent session | Available natively. Brigade governance is not wired. The [native T3 design](phase-2046-native-t3-app-hosted-design.md) and [governed-child contract](phase-2046-governed-native-child-contract.md) keep production dispatch unwired, and closeout is operator-assisted per [the closeout runbook](runbooks/native-t3-assisted-closeout.md). |
| Remote fleet request | An operator, an agent, or `brigade run` routing a seat to another machine | The target machine's T3. The source keeps the orchestration receipt and takes no model lease. | `t3-fleet` request id and delegation id, the source `WorkerResult.remote` handoff, and Hub lifecycle observations through [`brigade fleet events report`](fleet-t3-events.md). | `t3-fleet submit`, directly or through the source transport in [fleet_t3_transport.py](../src/brigade/fleet_t3_transport.py) | Separate supported path. Remote `brigade run` routing needs an enrolled fleet, routing enabled, and the [deployment prerequisites](phase-fleet-remote-transport.md#deployment-prerequisites). There is no local fallback. |
| Ledger task | `brigade work task add` or a work import | Brigade: one `brigade run` child per task, in a linked worktree | Task id and claim generation in the work ledger, session and chained intent receipts under `.brigade/dispatch/<dispatch-id>/`, and child run artifacts | `brigade work dispatch` | Shipped. Successful children stay claimed and `in_progress` until the operator lands them. |
| Durable run-lineage child | An operator or agent branching a checkpoint-covered parent lifecycle event | Brigade, under the new child run identity | New run id and the `run.json` `lineage` block (`kind`, `parent_run_id`, `branch_point_event_id`, `shared_prefix`), plus child artifacts and lifecycle journal, as defined in [run receipt schemas](receipt-schemas.md) | `brigade runs child <parent> <event-id>` | Shipped. It creates the branched run record and journal and does not launch workers. |
| Explicit batch task | A caller or agent supplying a batch file | The Brigade run orchestrator, with no planner or synthesis call | `request_id` and `task_id` under `.brigade/runs/batch-<request-hash>/`, with the lifecycle journal and per-task result references, per [explicit task batches](run-batch.md) | `brigade run batch batch.json` | Shipped for 1 through 8 tasks. Synthesis is refused with `synthesis-not-implemented`. It does not dispatch ledger tasks or use the T3 route. |

The Grok Bot `t3-dispatch` pack, an earlier front end for `t3-fleet`, is retired
in 0.32.0 ([retirement notice](grokbot-t3-dispatch.md)).

## Scheduled memory application

Two optional memory jobs can run on a schedule. Both need a care install to
activate OS timers.

`memory-reflect` runs `memory reflect run --scheduled` and is part of the
default care set. A scheduled hub run also attempts policy-gated application.
Without a hub it derives local candidates only.

`memory-verify` is a separate entry, installed with
`--entry memory-verify`. It runs `memory verify run --scheduled`.

### Proposals and tiers

Reflection opens pinned proposals for exact duplicates, near-duplicates and
broken links. Exact duplicate cards merge and can be Tier 0. Near-duplicate
supersedes and broken-link repairs are always Tier 2. Reflection also proposes
block anchoring, which is Tier 0 only when the card head came from an automatic
apply. A run withdraws any anchor proposal it could not apply, then retries it
after seven days.

A proposal pins card revisions and block hashes. A proposal the run leaves open
stays `pending` until a reviewer accepts or rejects it with `brigade memory
proposals decide`.

Tier 1 covers a replacement that only changes ASCII spaces. Tier 2 covers claim
edits, supersedes, link repairs, retirements and non-exact compensations. Every
Tier 1 and Tier 2 proposal waits for a person.

### What applies without a person

Automatic apply needs workspace intent and hub policy. New workspaces seed
`.brigade/memory/reflect/policy.json` with `auto_apply` on. Care install leaves
a missing policy off and preserves an existing one. The fleet policy must also
enable auto apply, list the proposer and list the op class. The supported policy
writer accepts evidence appends and exact duplicate merges for this route.

The hub rechecks the Tier 0 class, cited evidence, ownership, retrieval
evaluation, one-card blast cap, 24-hour cooldown, card heads, pins, trust, scans
and the admin kill switch in one transaction. External and handoff-only curation
policies never pass the ownership gate. Local repository candidates stay
proposal-only because their authority contracts are unbound.

`memory reflect auto-apply off --target .` sets the workspace policy to false.
An in-flight run reads it before its next candidate. `memory reflect
auto-apply on --target .` opts an existing workspace in.

### Scopes and rollback

`[reflect] scopes` in `.brigade/memory-care.toml` limits where reflection
proposes, applies and watches. Unlisted read scopes are still read, but no
candidate is proposed and no watch is polled there. Each listed entry must be a
configured read scope, and the credential needs `reflect` in it.

A scheduled hub run polls the compensation watch in its reflected scopes. The
watch covers only the polling credential's own automatic applies, within 24
hours by default. A contradiction or failed verification, or an attributable
retrieval regression, opens a compensating proposal. Two independent verifiers
can allow that reversal to apply without a person.

`memory reflect rollback <proposal-id>` also opens a compensating proposal. It
applies at once only when the original apply was automatic, the same policy
actor asks, the inverse is exact and the window has not expired. Otherwise it
waits for a reviewer.

### Verification pass

The verify pass checks cards against cited evidence with two seats, one
proposing and one confirming. It opens pinned correction proposals. Per-class go
or no-go records decide whether `verify_auto_apply` may apply a class without a
person. The recorded decision on 2026-11-16 was no-go for all five classes, so
the pass applies nothing today.

See [memory care](memory-care.md#automatic-tier-0-reflection) for the boundaries
and the separate care-install step that activates OS timers.

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
| Grok Bot MCP listener | `brigade run cloud grokbot serve --pack dot` or `--pack duet` | Ctrl+C, or stop the owning service. | Each pack records calls in its own state. Role instances and other pack ids were removed in 0.32.0 and exit 2. |
| Dispatch watch | `brigade work dispatch --watch --worker <seat> --interval 5` | SIGTERM or Ctrl+C stops launches. Children detach by default. `--drain-wait` waits for them. | Holds `dispatch.lock` for the loop. Supervisor events link immutable tick plans and record launched and finalized intents, budget denials, and session close. |
| Run watch | `brigade runs watch <run-id>` | Ends when the run reaches a terminal state, or stop with Ctrl+C. | Prints observations from run artifacts, or newline-delimited JSON with `--json`. It can recover stale artifact collection and update the affected run's records. |

The shipped hub command is `brigade fleet serve`. The listener requires its
configured credentials. Grok Bot listeners also require pack setup and
Cloudflare Access configuration. They take no bearer. The HTTP memory and skills servers require
`BRIGADE_MCP_HTTP_TOKEN`. See the [Grok Bot operating guide](grokbot-operating-guide.md)
and [MCP transport guide](technical-guide.md#streamable-http-for-the-read-only-mcp-servers)
for setup. These commands do not enable every service during installation.

Run dispatch watch under a service manager when it should survive terminal
closure. Give it an explicit target, an available worker seat, and a committed
base. For example, a systemd user unit can use:

```ini
[Service]
Type=simple
WorkingDirectory=/path/to/repository
ExecStart=/path/to/venv/bin/brigade work dispatch --target /path/to/repository --worker coder --watch --interval 5 --spawn-delay 1
Restart=on-failure
RestartSec=5
KillMode=process
```

`KillMode=process` sends the stop signal to the dispatcher while its children
retain their process groups. This allows the default detach behavior. Add
`--drain-wait` to wait for children instead, and set `TimeoutStopSec` to cover
the expected child runtime. Both SIGTERM and SIGINT stop new launches and write
a close receipt. A second signal ends drain waiting and leaves unfinished
children detached. Unexpected tick errors are recorded as
`work.dispatch.tick_error`, preserve running children, and allow later ticks
to continue. A close receipt includes `claimed_prelaunch_intents`, the count
of intents that still hold a claim without a child. The next dispatch invocation
resumes those intents using their saved plans and worktrees.
Each tick checks the installed Brigade version and closes watch when it changes.
The dispatcher sleeps between ticks even when no task is ready. `--max-total`
and `--deadline` bound one watch process, including retries. Use `Restart=no`
when either ceiling must stop the service. A failed child or tick keeps the exit
status at 1 even when watch closes for a ceiling, version change, or signal.
`Restart=on-failure` can therefore restart a ceiling close, and `Restart=always`
restarts every close. Each restart gives watch a fresh launch ceiling and deadline.
Restart the unit explicitly after updating Brigade when `Restart=no` is set.
Infrastructure retry exhaustion is retained across ticks within one watch
process. Restarting watch resets those exclusions and permits another initial
launch plus two retries for each affected task. Non-infrastructure task breakers
remain persisted across restarts.

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
