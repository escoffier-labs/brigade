---
status: active
proof: drive
surfaces: fleet serve
drive: hub-contract
last-drive: 2026-11-03 a6ca15f72
---

# Fleet Hub wire contract

The Fleet Hub (`brigade fleet serve`) is the HTTP service every node, the
`brigade fleet` CLI, and the Dot/Duet connectors talk to. It is due to be
rewritten in Rust, and clients must not notice the swap. This drive is the
acceptance suite for that swap: it starts a hub, drives it over HTTP only, and
checks the answers against the recorded contract. It never imports
`brigade.*`, so it runs the same way against any hub binary.

The full route inventory, the contract version, what counts as a breaking
change, and the hub behaviors recorded as observed live in
[`docs/phase-hub-contract.md`](../../../../docs/phase-hub-contract.md).

## Sub-features

- **Health.** `GET /health` answers `{"ok": true, "service": "brigade-fleet-hub"}`
  without a token. There is no version endpoint.
- **Registration.** `POST /nodes` `add` and `revoke`, `GET /nodes`, and
  `POST /message-principals` `add` and `grants`, all admin-only.
- **Heartbeat.** `POST /events` (run events, deduplicated) feeds `GET /status`,
  and `POST /sessions` `upsert` and `end` feed `GET /sessions`. Both reads hide
  terminal rows unless `?all=1`.
- **Claims.** `POST /claims` `acquire`, `renew`, `inspect`, `release` in holder,
  node, and force scope, plus `GET /claims` and `?all=1`. Generations start at
  1 and advance on every new holder, release, and expiry. A stale `generation`
  or a retired holder is refused with a `reason`. A node-scoped acquire
  supersedes only the dead run's lease, and a one-second lease lapses.
- **Messages.** `POST /messages` `send` (idempotent on `client_id`), `list` with
  the paging guarantee, `count`, `ack`, `sent` (by cursor and by ids),
  `sent_lookup`, `reply`, `identity` (with and without `peers`), and the
  station reads `who` and `claims`. 16 of the hub's 19 actions are covered;
  the three `events_*` actions are not.
- **Keys and sealed mail.** `keys_publish`, `keys_get`, and `manifests_get`,
  then sealed `send` and `reply` with envelopes stored and returned byte for
  byte, and every structural refusal: non-canonical text, a duplicate key, bad
  padding, a short digest, the wrong sender, a stale sender key, non-ASCII, the
  size limit, and a plaintext reply to a sealed parent.
- **Questions.** `question_create` (with retry, conflict, and the two-pending
  limit), `question_status`, `question_desktop`, and an operator reply that
  answers through the hub's trigger.
- **Long polls.** `wait_ms` bounds, an empty wait that holds about `wait_ms`,
  the per-recipient waiter cap, a send waking two parked waiters, and
  `question_create` waking the operator's waiters.
- **Owners.** `GET /owners` returns the `brigade.fleet_owners.v1` view.
- **Auth failures.** Missing, wrong, non-Bearer, revoked, and wrong-kind
  tokens. Also a node token on an admin route, the admin token on a node write,
  a node writing for another node, a message principal without a send grant,
  and a token in the query string.
- **Gates.** Worklore is off by default, so `/work/*` is a 404.
- **Refusal sweep (S1b).** 60 more request-validation refusals on the covered
  routes (`hub_contract_refusals.py`): field limits and formats on `send`,
  `enqueued_at` parsing and its expiry and future limits, key record
  validation, sealed envelopes without content or with a stale recipient key,
  question arguments, principal enrollment arguments, and node, session,
  claim and event fields. Each case pins its status; the golden pins its text.
- **Error text (S1b).** Every response at an expected 4xx status, from the
  core cases, the hot set, the sweep and the lifecycle cases, has its `error`,
  `code` and `reason` (or its body, when it is not JSON) compared with
  `hub-contract-golden.json`. The table is captured from the Python hub with
  `--write-golden`, never copied from the hub's source. The drive's random tag
  reads as `{tag}` and ISO timestamps as `{timestamp}`.
- **Lifecycle (S1b).** On a second hub that reads the drive's clock
  (`hub_contract_lifecycle.py`): messages past the 90-day retention leave
  `list`, `count`, `sent`, `sent_lookup`, `ack` and `reply`, with the
  boundary pinned to the second; a plaintext `reply_to` to a purged parent
  reads null while a sealed reply keeps its parent id; a sealed message
  acknowledged 8 days ago reads as its tombstone in `sent_lookup`, `sent` and
  `list`, one acknowledged 6 days ago and one never acknowledged stay sealed,
  and the 7-day boundary is pinned to the second; a `client_id` resend returns
  the first id on day 89 and makes a new message once the first has expired;
  a question times out at its deadline, records a late operator answer (but
  not one at exactly the deadline), stops counting toward the two-pending
  limit, and is gone after 90 days. The two
  tombstones must equal the bytes Python's own purge wrote, kept in the golden.

The drive runs 301 cases: 244 on the core hub over 14 routes and 16 message
actions, and 57 lifecycle cases. Per case it checks the status code,
`Content-Type: application/json`, the required keys and their JSON types,
exact text for fixed error messages and `code` values, and cross-response
guarantees: message ids ascend, `next_cursor` is the last id on the page,
`after` excludes ids at or below it, a retried send returns the first id, and
a long poll returns the new message well before `wait_ms`. Then 161 refusals
and 2 tombstones are compared with the golden.

## How to get to it (user POV)

Nobody calls the hub by hand. A user runs:

```bash
brigade fleet status --json
brigade fleet claims --json
brigade fleet msg send --to <node> --subject hi --body-file note.txt
brigade fleet msg list
brigade fleet msg ack <id>
brigade fleet owners show
```

and each one is one HTTP request to the hub. A Duet session asking for a fleet
digest runs `fleet status` and `fleet claims` underneath and lists the inbox.
If a rewritten hub moves a status code or drops a key, those commands start
failing in ways that are hard to trace back. This drive catches it first.

## Driving it with control-brigade

```bash
C=registry/skills/verify-brigade/control-brigade.py
$C hub-contract --dry-run   # the launch command and the covered routes, nothing started
$C hub-contract             # the in-repo Python hub, in a fresh temp state dir
$C evidence --label hub-contract
$C cleanup
```

No `--target` is needed. Without one the helper makes a fresh directory under
`<state-root>/targets/`, records it for `cleanup`, and keeps the hub's
`hub.db`, `admin-token` (mode 0600), `hub.log`, and a fake `home/` there. With
`--target "$TARGET"` the same files go under `$TARGET/hub-contract/<stamp>/`.

What the default run does:

1. Writes a throwaway admin token, picks a free loopback port, and starts
   `{brigade} fleet serve --host {host} --port {port} --db {db} --token-file {token_file}`
   with every `BRIGADE_*` variable dropped and `HOME` pointed at the fake home,
   so the hub cannot read an operator token or config.
2. Polls `GET /health` until the hub answers (`--startup-timeout`, default 30s).
3. Enrolls three throwaway nodes and one message principal with random
   suffixes, then runs every case. The hub also gets
   `BRIGADE_FLEET_OPERATOR_RECIPIENT=agent:contract-operator-<hex>`, which
   the question cases enroll.
4. Stops the hub (its whole process group on POSIX).
5. Starts a second hub under `lifecycle/` in the same state dir with
   `{python} {launcher} --clock-file {clock_file} -- fleet serve ...`, where
   `{launcher}` is `hub_clock.py` (the storage parity drive's clock seam).
   The clock starts at 2026-03-02T00:00:00Z and the drive moves it before
   each step, out to day 91. Then it stops that hub too.
6. Compares every refusal and both tombstones with `hub-contract-golden.json`
   and prints one JSON object.

Proof, from the helper's output:

- helper exit 0, `"ok": true`, `"mismatches": []`
- `"contract_version": 1`, `"cases_total": 301`, `"cases_failed": 0`
- `"lifecycle": {"ran": true, "cases_total": 57, ...}`
- `"golden": {"refusals_checked": 161, "tombstones_checked": 2, "written": false}`
- `"covered_routes"` lists the 14 routes the inventory marks `covered`
- `"covered_message_actions"` lists the 16 actions the inventory marks `covered`

A drift is helper exit 3, `"ok": false`, and one `mismatches` entry per
problem, each naming its `case` and `check` (`status`, `content-type`, `body`
with a JSON `path`, `invariant`, `error-text`, or `tombstone`). A hub that never answers `/health`, a
`--hub-cmd` without `{port}`, or an unsafe `--hub-url` is helper exit 1 with an
`error`.

Observed on main `31d5c549d`: exit 0, 77 cases, `"mismatches": []`, about one
second end to end. With the hot-set cases: exit 0, 184 cases,
`"mismatches": []`. The long polls and a one-second lease add a few seconds.
With S1b: exit 0, 301 cases, `"mismatches": []`, the same on a second run
with a new random tag. Through the Rust front (`brigade-hub` from
`engines/hub`, as the Hub CI job runs it): exit 0, 244 cases, 153 refusals
matching the golden, lifecycle skipped.

### Re-recording the golden

```bash
$C hub-contract --write-golden
```

It runs the in-repo Python hub only (`--hub-cmd`, `--hub-url` and
`--clock-hub-cmd` are refused with it), and writes the file only when every
case passed. Review the diff: a changed line is a change in what clients see,
so it needs the same review as a contract change.

### Against another hub binary

Give `--hub-cmd` a launch template. The placeholders are `{port}` (required),
`{host}` (the IPv4 loopback address), `{db}`, `{state_dir}`, `{token_file}`
(the admin token, one line), and `{brigade}` (the resolved Brigade command, as
its own word). The hub also gets the admin token as `BRIGADE_FLEET_TOKEN`.

```bash
$C hub-contract --hub-cmd "/path/to/brigade-hub --host {host} --port {port} --db {db} --token-file {token_file}"
```

`--hub-cmd` alone skips the lifecycle cases, because they need a hub that
reads the drive's clock, and the output says so in `lifecycle.skipped`. Give
that hub's launch template as `--clock-hub-cmd`, with `{clock_file}` (a file
holding one decimal epoch-seconds value, rewritten between requests) and
`{port}`. The same template is what `storage-parity --hub-cmd` takes:

```bash
$C hub-contract --hub-cmd "..." --clock-hub-cmd "/path/to/brigade-hub --clock-file {clock_file} --host {host} --port {port} --db {db} --token-file {token_file}"
```

To drive a hub you started yourself, pass its origin and name the variable
that holds its admin token. Only a loopback origin is accepted, because the
drive enrolls nodes and sends messages:

```bash
export BRIGADE_HUB_CONTRACT_ADMIN_TOKEN=<throwaway admin token>
$C hub-contract --hub-url "http://<loopback>:<port>" --operator-recipient agent:<fresh-name>
```

Start that hub with its default flags, an empty database, and
`BRIGADE_FLEET_OPERATOR_RECIPIENT=agent:<fresh-name>`. The drive enrolls that
principal, so pick a new name for every run. Nodes get fresh random names, so
a second run against the same hub still passes.

A hub whose database came from `brigade fleet restore` allocates new claim
generations above the claim recovery floor the restore wrote, at floor + 1
rather than 1. Pass that floor with `--claim-floor <floor>` (the `claim_floor`
in the restore's JSON report, or `hub_meta` key `recovery`) and the
claim-fencing cases expect every generation relative to it. Without the option
the drive expects a fresh hub.

### Memory profile (`--profile memory`)

`--profile memory` drives `POST /memory`, the hub memory route S13a adds under
contract v1 (D21), on a Rust hub launched with `--route-mode memory=native`.
The cases live in `hub_contract_memory.py`. They pin the status, keys, types
and `code` of every answer: the same card id in two scopes, bytes A then B
then A, a stale edit after retire and revival, a base revision under another
`log_epoch`, an admin put stored as `agent-unreviewed`, an import refused
without its grant and refused while the scan hook is unavailable, grants and
private scopes, idempotent retries and the `client_event_id` window, log
paging and visibility, and the refusal of a revoked node token. The drive
enrolls a fresh node and principal per run.

`--memory-absent` runs the one case a hub without memory answers instead:
`404 {"error": "not found"}`. The in-repo Python hub has no memory, so the
profile needs `--hub-cmd` (or `--hub-url`) unless `--memory-absent` is set.

```bash
$C hub-contract --profile memory --hub-cmd "/path/to/brigade-hub --host {host} --port {port} --db {db} --token-file {token_file} --route-mode memory=native"
$C hub-contract --profile memory --memory-absent --hub-cmd "/path/to/brigade-hub --host {host} --port {port} --db {db} --token-file {token_file}"
```

### Read-only production profile (`--profile prod-readonly`)

`--profile prod-readonly` runs an exact list of operations and nothing else:
`GET /health`, `GET /owners`, `GET /status`, `GET /sessions`, `GET /claims`,
and `POST /messages` `identity` (without `peers`), `count` and `keys_get`.
The list lives in `ALLOWED_OPERATIONS` in `hub_readonly.py`. A case outside
it, or `--only` naming an operation outside it, stops the run before the first
request. `list`, `sent`, `sent_lookup`, `ack`, `who`, `claims`, every
`question_*` action, `GET /policy/inventory` and the browser routes write on
main and stay off the list.

Without `--hub-url` it runs the audit that keeps the list honest. It launches a
throwaway hub through `hub_audit.py`, which puts a SQLite authorizer and a
statement trace on every connection the Python hub opens and keeps the
background sweepers from starting. It builds a fixture on a controlled clock
with every kind of overdue row (mail past retention, sealed mail acknowledged
more than 7 days ago, send-ledger rows older than a day, a question past its
deadline, an expired paging snapshot), then digests every table, the schema
and `user_version`, runs the profile, and digests again. Any recorded write,
any `BEGIN IMMEDIATE` or `BEGIN EXCLUSIVE`, or any digest change fails the run.
Retention has no exception. Afterwards the excluded operations run as
positive controls, and each must record a write.

```bash
$C hub-contract --profile prod-readonly
$C hub-contract --profile prod-readonly --hub-cmd "/path/to/brigade-hub --host {host} --port {port} --db {db} --token-file {token_file}"
$C hub-contract --profile prod-readonly --audit-mutation "GET /owners:write"   # must exit 3
```

With `--hub-cmd`, the front's managed Python child starts through the audit
launcher by way of `BRIGADE_HUB_PYTHON`, so the Rust front with every family
proxied is audited the same way. The hooks see only the instrumented child's
connections, so the audit also needs evidence that nothing else opened the
database. The child opens it through a private hard link, and an inotify watch
on the `--db` directory runs for the whole hub lifetime: any open through the
configured path fails the run, even a helper's connection that locks, rolls
back and closes before the response. The audit also fails when any process in
the hub's process group still holds the database open without the hooks. That
is where the Rust-side statement signals have to arrive once S5 to S8 give the
front its own connections. Where the watch cannot run (not Linux, no inotify,
an overflowed queue, a replaced or extra-linked database file), the run
reports the front as not certified and exits 3. `--audit-mutation
OPERATION:KIND` adds a `write`, a `noop` UPDATE that matches no row, a `begin`
that takes and releases the write lock, or a `helper` process that does the
same through its own connection, to `GET /owners`, `GET /status` or
`POST /messages count`.

With `--hub-url` it probes a running hub using a node token from
`BRIGADE_HUB_PROBE_NODE_TOKEN` and the token of an operator-enrolled probe
principal with no send grants from `BRIGADE_HUB_PROBE_PRINCIPAL_TOKEN`
(`--node-token-env` and `--probe-token-env` rename them). It never enrolls.
This is the only drive run allowed against production; the runbook is
`docs/runbooks/fleet-hub-proxmox.md`.

Observed on main `ff46d78f2` plus this change: the audit exits 0 on the Python
hub and on the Rust front in managed proxy mode, with zero window findings, no
digest change and all four controls writing. Each of the seven mutation kinds
(`write`, `noop`, `begin`, `begin-semicolon`, `begin-comment`, `pragma-checkpoint`,
`helper`) exits 3.

## Gotchas

- **Serve flags the drive leaves alone.** It launches `fleet serve` with only
  `--host`, `--port`, `--db`, and `--token-file`. It does not exercise
  `--deck-config`, `--trust-forwarded-proto`, or `--trust-tailscale-identity`,
  which change deck and proxy-auth behavior outside the wire contract.
- **The drive needs default hub flags.** `--allow-admin-writes` turns the
  admin-write refusals into successes, `--require-sealed` rejects the
  plaintext sends, and `BRIGADE_WORKLORE_ENABLED=1` makes `/work/items` answer.
  Each one shows up as mismatches that point at the flag.
- **The lifecycle hub is a second hub.** The core cases need real time (a
  one-second lease, long polls), so the stepped clock gets its own hub and
  database. `--hub-url` cannot step a running hub's clock, so it runs the
  lifecycle cases only with `--clock-hub-cmd`.
- **The purge batch stays under 32.** Python tombstones at most 32 sealed
  rows per read (`PURGE_BATCH_SIZE`). The lifecycle sequence keeps two
  eligible rows, so a hub that projects every eligible row (the Rust plan)
  passes the same cases. A backlog above 32 is a recorded difference, tested
  on Rust alone in S8.
- **Refusals the sweep does not reach.** Quota refusals (1,000 unread
  messages, 1,000 sends a day, sealed storage), refusals behind non-default
  flags (`--require-sealed`, storage limits, `--allow-admin-writes`),
  credential-generation races mid-request, the `events_*` actions, the
  teammate-role refusal, and database failures (`hub database error: ...`).
- **Long polls park on threads.** The cap case starts two `wait_ms` polls
  and probes with `wait_ms: 1` until the hub refuses a third, because nothing
  else shows that a poll is waiting. Timing checks are loose on purpose: an
  empty poll must hold at least 0.25s of a 0.3s wait, and a woken poll must
  return within 15s of a 20s wait.
- **Never point it at a shared hub.** It enrolls nodes, revokes one, and leaves
  messages behind. `--hub-url` refuses anything that is not loopback, and the
  launched hub never sees the operator's `BRIGADE_*` variables or home.
- **Seventeen hub quirks are pinned as observed**, including two auth-failure
  envelopes, the admin token on `/messages` getting the events wording, and a
  no-op force release that still advances the claim generation.
  The list is in the inventory doc. A hub that fixes one fails the drive until
  the golden and `CONTRACT_VERSION` move with it.
- **Not everything is compared.** Timestamps, ids, tokens, and key order are
  never checked, and extra response keys are allowed. A shape spec lists only
  the keys a client reads or that the contract promises.
- **The capture redacts tokens.** Every exchange lands in
  `captures/<stamp>-hub-contract.json`, with any `token` key replaced by
  `[redacted]` and no request headers recorded. `hub.log` stays in the state
  dir, which `cleanup` removes.
- **The golden is not derived from source.** Text that formats a value inline,
  such as a node id or an acquired-at time, is recorded as the hub printed
  it, with only the run's tag and timestamps replaced. The lifecycle names
  (`lifecycle-a`, `lifecycle-b`, `agent:parity-operator`) are fixed, so their
  text needs no placeholder.
- **Coverage is checked three ways.** The drive's routes must equal
  `COVERED_ROUTES` in `hub_contract.py`, and its `POST /messages` actions must
  equal `COVERED_MESSAGE_ACTIONS`. The test compares both to the `covered`
  rows in the inventory's route and action tables. Adding a case on a new
  route or action means updating all three.
- **Browser routes are out of scope.** The Command Deck and dashboard pages
  return HTML and no client calls them. The inventory lists them as `not yet`.

## Ops (S9, additive v1 routes)

`hub_contract_ops.py` drives `GET /metrics` on the Rust front. It issues real
node and principal credentials and requires 401 for each, a missing token,
an invalid token, query-token authentication and cookie authentication. An
admin bearer receives metrics text with writer values explicitly unavailable.
`engines/hub/tests/ops.py` runs these cases and audits the enabled shadow
route on a managed Python hub for housekeeping commits.

`hub-shadow-rules.json` is the shared comparator policy. Values are exact
unless the route declares a clock pointer or generated-ID namespace. Health
is the only enabled shadow family in S9. Other native handlers and their
housekeeping audits land in S5 to S8 before shadow can be enabled.
