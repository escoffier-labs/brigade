# Execution Model

Brigade runs only when something invokes it. There is no Brigade daemon, no background supervisor, and no built-in timer that wakes the CLI on its own.

That boundary is load-bearing for trust. Every local write, every queue change, and every receipt traces to an explicit command (or to a step inside an approved runbook that you started). If nothing called Brigade, nothing happened.

## What Brigade owns

Brigade ships deterministic memory-care and maintenance primitives (scan, import, lint, ingest, outcome rank/reconcile, doctor, status, center report build) plus approved runbooks that sequence those commands. Mutating commands write local artifacts under the workspace; some also write receipts. Read-only surfaces such as `memory care status`, `memory care doctor`, and `outcome rank` inspect existing artifacts without creating new work. Brigade does not decide *when* any of them run.

## What an external scheduler owns

Cron, systemd timers, CI schedules, OpenClaw cron, Task Scheduler, or any other operator-owned trigger sits outside Brigade. That trigger's job is to call Brigade (often one `brigade runbook run --approved ...` line, or a short shell sequence of Brigade commands). The schedule, host, and restart policy belong to the operator. Brigade is the thing the schedule calls, not the scheduler.

## Receipts and provenance

A scheduled invocation is still an explicit invocation: the same command runs whether you typed it or a timer did. Mutating commands leave local evidence under `.brigade/` (scan outputs, import queues, verify receipts, runbook run folders, and similar). Not every command writes a receipt; for example, `memory care scan` updates `scan-latest.json` and `refresh-queue.json` but does not emit a receipt file. Runbook receipts record runbook metadata and per-step `run` strings with exit codes and log paths, not a top-level `argv` field. Memory-care `status` and `doctor` read the latest scan and queue artifacts; they do not read runbook receipts.

## Scoped exception

`brigade tools runtime start` can launch a local MCP runtime process that you started and that you stop. That process is not a Brigade scheduler and does not run memory care, ingest, or outcome reconcile on its own.

## Future work (not shipped)

Issue [#759](https://github.com/escoffier-labs/brigade/issues/759) tracks an opt-in care scaffold that would write managed entries into the operator's own scheduler (install / status / uninstall), still without Brigade owning a daemon. Until that lands, schedule Brigade yourself with crontab, systemd timers, CI, or your existing agent cron. That scaffold is not shipped in the current release.

## Related

- [Memory care](memory-care.md): card scan, refresh queue, and import boundaries
- [Scanner registry](scanner-registry.md): explicit foreground sweeps, not a scheduler
- [Technical guide](technical-guide.md): full command surface and deliberate-friction rules
- [Roadmap](../ROADMAP.md): direction, with this page as the execution-model source of truth
