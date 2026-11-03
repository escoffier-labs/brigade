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

The drive runs 184 cases over 14 routes and 16 message actions. Per case it
checks the status code, `Content-Type: application/json`, the required keys
and their JSON types, exact text for fixed error messages and `code` values,
and cross-response guarantees: message ids ascend, `next_cursor` is the last
id on the page, `after` excludes ids at or below it, a retried send returns
the first id, and a long poll returns the new message well before `wait_ms`.

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
4. Stops the hub (its whole process group on POSIX) and prints one JSON object.

Proof, from the helper's output:

- helper exit 0, `"ok": true`, `"mismatches": []`
- `"contract_version": 1`, `"cases_total": 184`, `"cases_failed": 0`
- `"covered_routes"` lists the 14 routes the inventory marks `covered`
- `"covered_message_actions"` lists the 16 actions the inventory marks `covered`

A drift is helper exit 3, `"ok": false`, and one `mismatches` entry per
problem, each naming its `case` and `check` (`status`, `content-type`, `body`
with a JSON `path`, or `invariant`). A hub that never answers `/health`, a
`--hub-cmd` without `{port}`, or an unsafe `--hub-url` is helper exit 1 with an
`error`.

Observed on main `31d5c549d`: exit 0, 77 cases, `"mismatches": []`, about one
second end to end. With the hot-set cases: exit 0, 184 cases,
`"mismatches": []`. The long polls and a one-second lease add a few seconds.

### Against another hub binary

Give `--hub-cmd` a launch template. The placeholders are `{port}` (required),
`{host}` (the IPv4 loopback address), `{db}`, `{state_dir}`, `{token_file}`
(the admin token, one line), and `{brigade}` (the resolved Brigade command, as
its own word). The hub also gets the admin token as `BRIGADE_FLEET_TOKEN`.

```bash
$C hub-contract --hub-cmd "/path/to/brigade-hub --host {host} --port {port} --db {db} --token-file {token_file}"
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
proxied is audited the same way. The audit also fails when any process in the
hub's process group holds the database open without the audit hooks, which is
where the Rust-side statement signals have to arrive once S5 to S8 give the
front its own connections. `--audit-mutation OPERATION:KIND` adds a `write`,
a `noop` UPDATE that matches no row, or a `begin` that takes and releases the
write lock to `GET /owners`, `GET /status` or `POST /messages count`.

With `--hub-url` it probes a running hub using a node token from
`BRIGADE_HUB_PROBE_NODE_TOKEN` and the token of an operator-enrolled probe
principal with no send grants from `BRIGADE_HUB_PROBE_PRINCIPAL_TOKEN`
(`--node-token-env` and `--probe-token-env` rename them). It never enrolls.
This is the only drive run allowed against production; the runbook is
`docs/runbooks/fleet-hub-proxmox.md`.

Observed on main `ff46d78f2` plus this change: the audit exits 0 on the Python
hub and on the Rust front in managed proxy mode, with zero window findings, no
digest change and all four controls writing. Each of the three mutation kinds
exits 3.

## Gotchas

- **Serve flags the drive leaves alone.** It launches `fleet serve` with only
  `--host`, `--port`, `--db`, and `--token-file`. It does not exercise
  `--deck-config`, `--trust-forwarded-proto`, or `--trust-tailscale-identity`,
  which change deck and proxy-auth behavior outside the wire contract.
- **The drive needs default hub flags.** `--allow-admin-writes` turns the
  admin-write refusals into successes, `--require-sealed` rejects the
  plaintext sends, and `BRIGADE_WORKLORE_ENABLED=1` makes `/work/items` answer.
  Each one shows up as mismatches that point at the flag.
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
- **Coverage is checked three ways.** The drive's routes must equal
  `COVERED_ROUTES` in `hub_contract.py`, and its `POST /messages` actions must
  equal `COVERED_MESSAGE_ACTIONS`. The test compares both to the `covered`
  rows in the inventory's route and action tables. Adding a case on a new
  route or action means updating all three.
- **Browser routes are out of scope.** The Command Deck and dashboard pages
  return HTML and no client calls them. The inventory lists them as `not yet`.
