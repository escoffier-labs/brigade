# Proposal: seat health probes and typed worker failures

Status: proposed for issue #474. Planning only, no implementation.

## Decision summary

Replace the current collection of roster, doctor, transport, and dispatch
checks with one adapter-owned health probe for every requested seat. Run the
probe before planning, record every result, and route only through healthy
seats or their declared fallbacks. Re-run a targeted probe after a retryable
infrastructure failure, a harness restart, or a resumed run. Do not add
periodic polling.

Normalize worker failures into the closed taxonomy below. Each terminal
worker attempt gets one failure class, a retry disposition, detection
metadata, and an operator message. Existing `failure_phase`, `failure_kind`,
`timed_out`, status, and exit-code fields remain in place during migration.

This proposal separates three outcomes that are currently easy to mix:

- worker infrastructure failed, which is neutral evidence about the skill;
- the worker produced an invalid result, which is also neutral evidence about
  the skill; and
- the declared verifier rejected the produced change, which is negative
  evidence under the `VerificationContract` proposed in #500.

Issue #489 continues to own run lifecycle and resume semantics. Issue #475
continues to own capture-before-retry for verification receipts.

## Evidence and limits

The design is based on the current source, issues #474, #394, #284, #299,
#206, #231, #277, and #87, plus the field incidents supplied with #474.
MiseLedger searches returned no evidence IDs for the operator-supplied
incidents involving the GPT-5.5 app-server stdin hang, restart survivors that
contained only a virtual environment, the Codex client-version gate, or an
OpenCode isolation escape. Those incidents are therefore labeled as operator
reports below, not independently verified receipts.

The example workstation run `20260726-052351` was not readable from this host.
The most recent failed GPU-box run inspected during this planning pass,
`20260726-210233-ac64f883`, failed with a typed run-level
`run-isolation/branch-head-drift` receipt. That failure is useful as a boundary
example: it belongs to the run guard, not the worker taxonomy.

## Current state

### Seat and transport declaration

`roster.toml` already contains the inputs needed for a probe:

| Declaration | Current source |
| --- | --- |
| Global sandbox and Codex transport | `src/brigade/roster.py:16-20`, `src/brigade/roster.py:53-62` |
| Seat CLI, endpoint, model, reasoning, transport, version, environment, and read-only claim | `src/brigade/roster.py:30-49` |
| Seat requirements and ordered fallback list | `src/brigade/roster.py:46-47` |
| Global and per-seat validation | `src/brigade/roster.py:319-430` |
| Requirement and fallback resolution | `src/brigade/roster.py:562-711` |

Codex uses the roster-wide `codex_transport`. Cursor ACP seats use the
per-seat `transport` and `transport_version`. Direct CLI and endpoint seats
use the remaining per-seat fields. The probe must consume this model rather
than introduce a second transport registry.

### Existing preflight checks

The same fact is often checked by different code with different failure
strings.

| Check | Where it runs today | Current behavior |
| --- | --- | --- |
| Executable presence and Cursor auth requirement | `src/brigade/roster.py:105-132`, `src/brigade/roster.py:562-711` | Used by capability resolution in roster suggestion and doctor paths, not as the run's single admission gate. |
| Executable, model inventory, Cloudflare environment, ACPX version, and Cursor auth | `src/brigade/roster_cmd.py:340-457` | Doctor emits a mixture of `OK`, `WARN`, and `FAIL`; run dispatch does not consume one typed result from it. |
| Live model inventory | `src/brigade/model_inventory.py:11-17`, `src/brigade/model_inventory.py:55-64`, `src/brigade/model_inventory.py:106-161` | Cursor, Grok, and Ollama probes are bounded, but Codex has no equivalent inventory result. |
| Cloudflare provider configuration | `src/brigade/run_transport.py:76-93`, `src/brigade/run_transport.py:301-317`, `src/brigade/run_transport.py:586-600` | Checked separately for the primary and fallback worker attempts. |
| Orchestrator Cloudflare configuration | `src/brigade/aboyeur.py:748-756` | Separate from the worker helper. |
| ACPX executable, reviewed version, Cursor auth, and safe worktree | `src/brigade/acpx_adapter.py:359-461` | Produces adapter-specific failure kinds. |
| Environment references, executable, Ollama model, sandbox, and argument construction | `src/brigade/agents.py:907-1053` | Checked at dispatch time after the run has already committed to a seat. |
| Codex app-server spawn and initialize handshake | `src/brigade/codex_appserver.py:89-150` | A failed start causes the orchestrator path to fall back to exec at `src/brigade/aboyeur.py:2673-2685`; the warning is not a queryable routing decision. |
| Read-only capability | `src/brigade/cli/run.py:350-367`, `src/brigade/agents.py:327-370` | Weak or absent enforcement warns. It does not make an unsafe seat ineligible. |
| Branch and HEAD drift | `src/brigade/aboyeur.py:2350-2385` | Correctly handled as a run-isolation failure. It is not a seat-health check. |

### What happens after launch

Subprocess timeouts return exit code 124 after process-group termination in
`src/brigade/proc.py:323-426`. Direct adapters infer some auth, trust, and
stdin-hang failures from output in `src/brigade/agents.py:886-904` and
`src/brigade/agents.py:1127-1247`. ACPX has its own timeout, permission,
startup, transport, and output classifications in
`src/brigade/acpx_adapter.py:538-600`. App-server turns can be interrupted and
salvaged, but a dead server commonly reaches the worker result as untyped
detail through `src/brigade/codex_appserver.py:295-423` and
`src/brigade/aboyeur.py:1070-1166`.

Unexpected dispatch exceptions become failed worker results without a failure
kind at `src/brigade/run_transport.py:684-694`. Result validation has another
set of free-form kinds in `src/brigade/result_integrity.py:17-59` and
`src/brigade/result_integrity.py:200-245`.

`worker-results.json` preserves optional `failure_phase` and `failure_kind`
fields at `src/brigade/run_receipts.py:27-78`. Attempt receipts carry the same
free-form fields at `src/brigade/run_receipts.py:81-102`. When any worker
fails, `run.json` reduces the set to `workers/worker-failure` at
`src/brigade/aboyeur.py:3008-3055`. The operator sees:

```text
warning: run incomplete: N worker(s) failed; see worker-results.json
```

That message is emitted at `src/brigade/aboyeur.py:3135-3141`. It does not say
which seat failed, whether a retry is safe, or what action could restore the
seat.

The downstream loss is measurable in source. Friction scanning ignores the
worker failure kind and records every failure as `tool_failure`, with only
timeout versus `adapter_error`, at `src/brigade/friction_cmd.py:683-705`.
Outcome capture derives a signal from the top-level run status at
`src/brigade/outcome_cmd.py:563-568` and
`src/brigade/outcome_cmd.py:1066-1147`. Infrastructure failures can therefore
lower an artifact's outcome score.

## Closed worker-failure taxonomy

Every failed worker attempt must end with exactly one class from this table.
Adapters may retain a provider-specific `cause_code`, but they may not create
new public classes. Unrecognized failures use `unclassified` so additions to
the closed set require a schema review.

Retry dispositions have fixed meanings:

- `never`: do not retry this task on the same seat in this run;
- `after-remediation`: an operator or account action is required;
- `fallback`: quarantine this seat for the run and try the next declared
  fallback;
- `same-seat-once`: re-probe, then allow one bounded retry before fallback.

| Class | Detection | Receipt and retry | Operator message | Observed basis |
| --- | --- | --- | --- | --- |
| `configuration-invalid` | Roster validation, missing environment reference, unsupported argument or reasoning value, provider configuration validation | `phase=preflight`; `after-remediation` | `seat {seat} has invalid configuration: {safe_detail}; update roster.toml or the named environment reference` | Cloudflare environment work in #394; wrong CLI flag and rejected effort noted in #474 |
| `executable-unavailable` | Adapter executable cannot be resolved or launched | `phase=preflight`; `after-remediation` | `seat {seat} cannot start because {executable} is unavailable` | Existing executable checks in roster, doctor, ACPX, and direct dispatch |
| `auth-required` | A structured auth-status command reports logged out, token rejection is explicitly authenticated as the cause, or the provider returns an authentication code | `phase=preflight` or `dispatch`; `after-remediation` | `seat {seat} is not authenticated; run {adapter_remediation}` | Cursor ACP auth work in #284 |
| `entitlement-denied` | Provider reports account, subscription, data-policy, organization, or feature entitlement denial | `phase=preflight` or `dispatch`; `after-remediation` | `seat {seat} is authenticated but the account cannot use {model_or_feature}: {safe_detail}` | Provider 403 and Fable data-policy acknowledgement noted in #474 |
| `version-gate` | Installed client or transport version is outside the adapter's reviewed range, or the provider explicitly requires a newer client | `phase=preflight`; `after-remediation` | `seat {seat} needs {component} {required}; found {actual}` | ACPX reviewed-version gate; Codex client-version gate operator report, no MiseLedger evidence |
| `model-unavailable` | Exact live inventory says the model is absent, or an exact model-selection smoke request returns a structured unavailable response | `phase=preflight`; `fallback` | `seat {seat} cannot reach requested model {model}; trying declared fallback {fallback}` | Live model inventory in #299 |
| `capacity-exhausted` | Structured provider rate, quota, or capacity response | `phase=dispatch`; `same-seat-once` when a retry delay is supplied, otherwise `fallback` | `seat {seat} has no current capacity for {model}; {retry_or_fallback}` | Model capacity failures noted in #474 |
| `network-unavailable` | DNS, connection, TLS, proxy, or endpoint-health failure with a transport-independent error | `phase=preflight` or `dispatch`; `same-seat-once` | `seat {seat} cannot reach {redacted_endpoint}: {safe_detail}; rechecking once` | Existing `network-error` result classification |
| `transport-unavailable` | Transport process cannot start, initialize, negotiate its protocol, or create a session | `phase=preflight` or `dispatch`; `same-seat-once`, then `fallback` | `{transport} for seat {seat} could not initialize: {safe_detail}; {retry_or_fallback}` | App-server and ACPX startup paths; acpx exit 5 in #277 |
| `transport-hang` | No protocol progress or expected handshake marker before the transport watchdog, including a known stdin or permission-wait state | `phase=preflight` or `dispatch`; `fallback` | `{transport} for seat {seat} stopped making progress after {seconds}s at {last_event}; seat quarantined for this run` | GPT-5.5 app-server stdin operator report, no MiseLedger evidence; Cursor empty/hanging reports in #231 and #474 |
| `interactive-blocked` | Structured permission or trust request cannot be answered under the run policy | `phase=preflight` or `dispatch`; `never` | `seat {seat} requested interactive approval for {operation}; run noninteractively cannot continue` | Cursor trust and late ACPX permission behavior in #277 and #474 |
| `worker-crash` | Worker or harness exits unexpectedly, receives a non-operator signal, or its transport disappears after a successful handshake | `phase=dispatch`; `fallback` | `seat {seat} exited unexpectedly ({safe_exit}); partial logs are in {receipt_path}` | Harness-restart worker death operator report, no MiseLedger evidence |
| `timeout` | The worker is still alive and has made protocol progress, but exceeds its declared task deadline | `phase=dispatch`; `fallback` | `seat {seat} exceeded its {seconds}s task deadline after {last_event}` | Current subprocess and app-server turn timeouts |
| `isolation-breach` | A post-attempt snapshot shows a seat wrote outside its authorized worktree or a read-only seat changed tracked or canonical files | `phase=postflight`; `never` | `seat {seat} changed {bounded_paths} outside its allowed workspace; run stopped and seat quarantined` | OpenCode isolation operator report, no MiseLedger evidence; weak enforcement documented in #87 |
| `output-contract-violation` | Exit or turn completes but the normalized final is empty, malformed, tool-only, progress-only, non-final, or violates the plan/result schema | `phase=validation`; `fallback` only when an existing reviewed invalid-final fallback is declared, otherwise `never` | `seat {seat} completed without a valid {contract}: {safe_detail}` | Empty output in #206 and #474; current result-integrity checks |
| `provider-rejected` | A structured provider rejection does not fit auth, entitlement, version, model, capacity, or configuration | `phase=dispatch`; `fallback` | `provider rejected seat {seat}: {safe_detail}; trying declared fallback {fallback}` | Current generic `provider-error` classification |
| `unclassified` | Mandatory last resort after adapter and common classifiers fail | Actual phase; `never` | `seat {seat} failed for an unclassified reason; inspect {receipt_path} and file a classifier issue` | Current free-form and exception-only failures |

Operator cancellation, run abandonment, branch drift, dirty-worktree rejection,
planner failure, receipt-write failure, and verification failure are not worker
classes. They retain their owning run, lifecycle, receipt, or verification
domains.

### Classification precedence

Classification follows evidence, not string order:

1. A structured adapter or provider code wins.
2. A probe result tied to the same seat fingerprint wins over later generic
   process output.
3. A bounded adapter-specific output classifier may map a known signature.
4. A generic exit or timeout classifier applies only when the preceding
   evidence is absent.
5. Everything else is `unclassified`.

This prevents a timeout wrapper from hiding a known auth prompt, and it avoids
turning every nonzero process exit into `worker-crash`. Text signatures remain
adapter-owned and tested. They are diagnostics, not new taxonomy values.

## Unified seat health probe

### Interface

The implementation should expose one common operation:

```text
probe(seat, roster, run_policy, workspace_snapshot) -> SeatHealthResult
```

The common coordinator owns deadlines, caching, receipt serialization, and
routing. Each adapter supplies the checks that can establish its health. A
result contains:

- seat and immutable probe identifier;
- a redacted seat fingerprint;
- requested and effective adapter, transport, model, and reasoning;
- `healthy`, `degraded`, or `unhealthy`;
- one result for every applicable check;
- start, finish, duration, and cache provenance;
- a normalized failure when status is `unhealthy`;
- any declared fallback resolution.

`degraded` means a check is unsupported or advisory, not that a known failure
was ignored. For example, an adapter without live inventory may be degraded
when the transport and exact model smoke pass but inventory enumeration is
unavailable. A failed required check is always unhealthy.

### Checks

The probe runs these checks in order inside each seat's deadline:

1. **Declaration:** reuse roster parsing to validate the adapter, transport,
   version, model, reasoning, timeout, environment references, and sandbox
   combination.
2. **Executable identity:** resolve the executable and capture a normalized
   component name and version. Do not record a user-specific absolute path.
3. **Authentication and entitlement:** use a supported status command when
   available. Otherwise use the adapter's minimal transport request and
   classify only structured responses.
4. **Transport liveness:** start and close the declared transport. Exec seats
   run their bounded native status or version operation. Codex app-server must
   spawn, initialize, create a thread with the requested model, and close it.
   ACPX must pass its reviewed-version, auth, workspace, startup, and protocol
   handshake.
5. **Version gates:** compare the detected client and transport versions with
   adapter-owned reviewed ranges, including provider responses that explicitly
   require a newer client.
6. **Model reachability:** prefer exact live inventory. When no inventory API
   exists, perform a minimal exact-marker request through the declared
   transport in a temporary throwaway Git repository. A fuzzy model match is
   advisory and never silently changes the requested model.
7. **Isolation compatibility:** verify that the requested run policy can be
   enforced by the seat or by Brigade's detached worktree. A read-only run
   cannot admit a seat whose effective enforcement is `soft` or `none`.
   Canonical-write runs remain authorized by their existing policy.

The probe does not write to the target worktree. Model and transport smoke
requests use a temporary repository and a no-tools, no-write prompt. Environment
receipts contain variable names and presence only, never values. Endpoint
receipts contain a redacted host, never credentials or query parameters.

### Budget and cache

Requested seats and all members of their declared fallback chains are probed
in parallel. Each seat has a 30-second hard deadline. Model reachability gets
at most 15 seconds within that budget. The overall admission phase has a
35-second deadline, so a broken seat cannot serialize delay across the roster.

A healthy result may be reused for five minutes when its fingerprint matches.
An unhealthy result may be reused for 30 seconds to prevent immediate restart
loops. The fingerprint includes executable identity and version, adapter and
transport version, model, reasoning, environment variable presence, sandbox
policy, and workspace mode. It excludes secret values. Isolation snapshots and
postflight checks are never served from cache.

These limits should be constants in the first implementation, not new
`roster.toml` fields. Field data can justify configuration later.

### When it runs

- **Run admission:** after the initial `run.json` exists and before planning.
  This ensures a probe crash is receipted and the planner sees only usable
  seats.
- **Dispatch:** reuse a matching fresh result. Re-probe only when the result
  expired or the effective seat fingerprint changed.
- **After a retryable infrastructure failure:** persist the failed attempt
  first, then re-probe before the one permitted same-seat retry. This ordering
  follows the capture-before-retry principle in #475 without taking ownership
  of verification retries.
- **After harness or transport restart:** invalidate affected cache entries and
  probe before admitting more work.
- **Resume:** #489 invokes the same targeted probe before it resumes an
  interrupted worker. The probe does not define resumable states.
- **Postflight:** compare the workspace snapshot after every attempt to detect
  an isolation breach.

There is no background polling. A healthy idle seat does not need network
traffic, and polling would create a second lifecycle system.

### Routing

Routing uses only the ordered `fallback` declarations already present in the
roster:

| Probe result | Routing action |
| --- | --- |
| Orchestrator healthy | Continue to planning. |
| Orchestrator unhealthy with a healthy declared fallback | Select it, record the resolution, and print one warning before planning. |
| Orchestrator unhealthy without a healthy declared fallback | Abort with the typed cause before planning. |
| Worker healthy | Keep it in the effective roster. |
| Worker unhealthy with a healthy declared fallback | Replace it through the existing fallback resolution and record requested plus effective seat. |
| Worker unhealthy without a healthy declared fallback | Drop it before planning. Abort only when no route can satisfy the requested work. |
| Seat becomes unhealthy mid-run | Persist the attempt, apply its retry disposition, quarantine when required, and use only a declared fallback. |
| Isolation breach | Stop dispatch, fail the run, quarantine the seat, and do not synthesize over potentially contaminated canonical state. |

Direct `--worker` runs use the same declared fallback behavior and print the
requested and effective seat. Brigade never invents a fallback based on model
similarity, speed, or historical score.

The existing app-server-to-exec fallback remains available during migration,
but becomes an explicit `transport-unavailable` routing decision with both
attempts in the receipt.

## Receipt design

### Seat-health receipt

Write `seat-health.json` beside `run.json` before planning. Update it
atomically after targeted re-probes.

```json
{
  "schema": "brigade.seat_health.v1",
  "run_id": "20260726-123456-abcdefgh",
  "started_at": "2026-07-26T16:34:56Z",
  "finished_at": "2026-07-26T16:35:07Z",
  "results": [
    {
      "probe_id": "probe-01",
      "seat": "implementer",
      "fingerprint": "sha256:redacted-inputs",
      "status": "unhealthy",
      "cached": false,
      "requested": {
        "adapter": "codex",
        "transport": "app-server",
        "model": "gpt-5.6"
      },
      "checks": [
        {
          "name": "transport-liveness",
          "status": "failed",
          "duration_seconds": 10.004,
          "cause_code": "initialize-no-progress"
        }
      ],
      "failure": {
        "schema": "brigade.worker_failure.v1",
        "class": "transport-hang",
        "domain": "infrastructure",
        "phase": "preflight",
        "retry": "fallback",
        "detected_by": "codex-app-server-handshake",
        "detail": "no initialize response before the 10s check deadline",
        "remediation": "inspect the app-server log and client version",
        "probe_id": "probe-01"
      },
      "resolution": {
        "outcome": "fallback",
        "effective_seat": "implementer-exec"
      }
    }
  ]
}
```

Check details are bounded and redacted. Full stdout and stderr remain in the
existing log files. `cause_code` is adapter-specific and queryable, but the
public `class` stays closed.

### Worker attempts

Add the same normalized `failure` object to each failed attempt and failed
worker result in `worker-results.json`:

```json
{
  "worker": "reviewer",
  "ok": false,
  "failure_phase": "validation",
  "failure_kind": "empty-output",
  "failure": {
    "schema": "brigade.worker_failure.v1",
    "class": "output-contract-violation",
    "domain": "infrastructure",
    "phase": "validation",
    "retry": "never",
    "detected_by": "normalized-final-validator",
    "cause_code": "empty-output",
    "detail": "process exited 0 without a normalized final",
    "attempt": 1
  }
}
```

The legacy fields stay populated with their current values. Readers that know
the new schema use `failure.class`; older readers keep working.

### Run summary

Add these fields to `run.json` without removing the current
`failure_phase`/`failure_kind` summary:

```json
{
  "health": {
    "schema": "brigade.seat_health_summary.v1",
    "receipt": "seat-health.json",
    "healthy": 3,
    "degraded": 0,
    "unhealthy": 1,
    "fallbacks_selected": 1
  },
  "worker_failure_summary": {
    "domain": "infrastructure",
    "classes": {
      "transport-hang": 1
    },
    "seats": ["implementer"]
  }
}
```

The human summary uses the same data:

```text
run incomplete: implementer failed [transport-hang]
app-server made no progress for 10s; seat quarantined
declared fallback implementer-exec also failed [auth-required]
receipt: .brigade/runs/<id>/worker-results.json
```

### Outcome and friction attribution

Receipts expose a stable attribution:

- `domain=infrastructure` for every class in this proposal;
- `domain=verification` for an executed `VerificationContract` failure from
  #500;
- `domain=run` for lifecycle, isolation guard, receipt, and orchestrator
  failures outside a worker attempt.

Outcome capture treats an infrastructure-only incomplete or failed run as
neutral evidence for the skill or card. It still records the event and
evidence reference. A verifier failure is negative evidence. A successful
verifier is positive evidence. A mixed run with any infrastructure failure
and no completed verifier is neutral, because the skill was not given a clean
trial.

Friction scanning uses `failure.class` as `error_class`, retains
`cause_code`, and groups by seat fingerprint plus transport. Legacy receipts
are mapped at read time through a documented compatibility table. Unknown
legacy values map to `unclassified`; old files are not rewritten.

## Migration plan

### Phase 1: taxonomy and additive schemas

Create the closed enum, normalized failure type, serializer, and compatibility
mapper. Add normalized fields to worker and attempt receipts while preserving
all existing fields, schema names, exit codes, and log paths. Map every current
failure kind in tests to a class or `unclassified`.

### Phase 2: one probe, shadow routing

Implement adapter checks behind the common coordinator. Have roster doctor
render the probe result. At run admission, execute and receipt the probe but
retain current routing behavior for one compatibility release, except for
already-fatal checks. Compare the probe decision with current inline
preflights in tests and receipts.

### Phase 3: probe-owned admission and routing

Make the effective roster consume probe results before planning. Route through
declared fallbacks, add targeted re-probes, and turn app-server exec fallback
into a receipted decision. Existing helpers become thin adapter checks called
by the probe. Remove duplicate call sites only after parity tests cover their
current messages and exit behavior.

### Phase 4: isolation postflight and ledger attribution

Enforce the read-only isolation admission rule, add per-attempt postflight
snapshots, and stop on `isolation-breach`. Teach friction and outcome capture
to consume normalized attribution. Keep read-time mapping for legacy receipts.

### Backward compatibility

- No `roster.toml` key changes are required.
- Current `requires`, `fallback`, `codex_transport`, seat `transport`, and
  `transport_version` semantics remain authoritative.
- `brigade.run.v1` and `brigade.worker_results.v1` gain additive fields.
- Existing `failure_phase`, `failure_kind`, `timed_out`, `exit_code`, status,
  logs, and CLI exit codes remain populated.
- Doctor retains its current check labels during the transition, but derives
  their state and detail from the unified result.
- Existing app-server-to-exec behavior remains possible and becomes visible.
- Legacy receipts are classified when read, never rewritten.

## Composition with adjacent issues

### #489 lifecycle and resume

The probe reports whether a seat is usable now. It does not decide whether a
run is `running`, `interrupted`, `resumable`, `canceled`, `abandoned`, or
`complete`. On resume, #489 calls the probe with the saved seat fingerprint.
If it changed or is unhealthy, #489 records the lifecycle transition and
applies its own resume policy.

### #500 VerificationContract

The worker taxonomy ends when a valid worker result exists. #500 owns which
verifier runs, its budget, rollback policy, and its result. Outcome capture
uses the receipt domain to keep infrastructure failure neutral and actual
verification failure negative.

### #475 capture before retry

Every failed worker attempt is written before a re-probe or retry. This mirrors
#475's ordering, but this proposal does not alter verification retry behavior.

## Implementation decomposition

The tracking issue remains #474. The implementation should be split as
follows.

### 1. M: Add the closed worker-failure taxonomy and receipt compatibility

Define the enum and normalized failure object, map all current worker and
adapter kinds, serialize it additively in worker and attempt receipts, and
provide legacy read-time mapping. This has no routing changes.

Acceptance:

- every failed worker and attempt has one normalized class;
- unknown values become `unclassified`;
- legacy fields and schema identifiers remain unchanged;
- table-driven tests cover every class and every existing failure kind;
- operator cancellation and run-level failures cannot be serialized as worker
  classes.

Dependencies: none.

### 2. M: Implement the adapter-owned unified seat health probe

Build the coordinator, adapter checks, budgets, redaction, cache fingerprint,
and `seat-health.json`. Make roster doctor render the shared result while
preserving its public labels.

Acceptance:

- direct, endpoint, Codex exec, Codex app-server, and Cursor ACPX seats have
  adapter check matrices;
- probes run in parallel within the 30-second seat and 35-second overall
  limits;
- fixture adapters cover healthy, degraded, auth, version, model, transport,
  hang, and redaction cases without live credentials;
- probe smoke work occurs only in a temporary repository;
- doctor and the run probe agree for the same fixture.

Dependencies: sub-issue 1.

### 3. M: Route runs from probe results and persist retry attempts

Run admission before planning, build the effective roster from healthy seats
and declared fallbacks, add cache invalidation and targeted re-probes, and
surface actionable operator messages. Convert app-server exec fallback into a
recorded decision.

Acceptance:

- an unhealthy orchestrator falls back or aborts before planning;
- unhealthy workers are replaced or dropped before the planner receives the
  roster;
- failed attempts are written before any retry;
- only `same-seat-once` classes retry the same seat, at most once;
- restart and #489 resume entry points invalidate and re-probe the affected
  seat without defining lifecycle states;
- current CLI exit codes remain compatible.

Dependencies: sub-issues 1 and 2.

### 4. M: Detect and stop worker isolation breaches

Add per-attempt workspace authorization snapshots, enforce hard read-only
admission, and classify writes outside the authorized worktree. Do not change
the semantics of explicitly authorized canonical-write runs.

Acceptance:

- a read-only seat with `soft` or `none` enforcement is not admitted;
- a fixture worker that mutates canonical tracked files produces
  `isolation-breach`;
- dispatch stops before synthesis over contaminated state;
- the receipt lists a bounded, repository-relative path sample without file
  contents;
- detached worktree writes remain allowed.

Dependencies: sub-issues 1 and 2. It may run in parallel with sub-issue 3.

### 5. S: Attribute infrastructure failures in friction and outcomes

Consume normalized classes in friction scanning and distinguish
infrastructure from verification evidence in outcome capture.

Acceptance:

- friction records `failure.class` and optional `cause_code`;
- an infrastructure-only failed or incomplete run produces neutral artifact
  evidence;
- a #500 verifier failure remains negative and verifier success remains
  positive;
- mixed and legacy receipt fixtures have explicit expected signals;
- no historical receipt is rewritten.

Dependencies: sub-issue 1 for readers and sub-issues 3 and 4 for final receipt
fixtures.

Ordering: 1, then 2, then 3 and 4 in parallel, then 5.

## Non-goals

- Implementing lifecycle states, cancellation, abandonment, or resume from
  #489.
- Defining verifier commands, rollback, or verification budgets from #500.
- Changing verification capture-before-retry behavior from #475.
- Logging in, refreshing credentials, accepting data policies, upgrading
  clients, installing transports, or downloading models automatically.
- Selecting undeclared fallback seats or using outcome rank as a router.
- Adding a daemon or continuous seat polling.
- Guaranteeing that a third-party sandbox is secure. The design checks declared
  enforcement and detects repository mutations; it is not a general host
  security boundary.
- Changing scheduler, planner, synthesis, or route semantics beyond removing
  unhealthy seats before planning.
- Rewriting historical receipts.
- Adding new roster configuration until measured probe data shows a need.

## Main design risk

Provider and harness errors are not stable APIs. A classifier based mainly on
stderr text could label a work failure as infrastructure, make the outcome
neutral, and hide a regression in the skill being measured. The mitigation is
the precedence rule above: structured codes and probe evidence first, narrow
adapter-owned signatures second, and `unclassified` rather than a confident
guess. Receipt fixtures must preserve the raw bounded evidence needed to
correct a classification later.
