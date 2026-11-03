---
status: active
proof: drive
surfaces: fleet serve
drive: storage-parity
last-drive: 2026-11-03 dc80cd01e
---

# Fleet Hub storage parity

The wire contract ([hub-contract.md](hub-contract.md)) checks what the Fleet
Hub answers. This drive checks what it stores. During the v2 cutover a Rust
hub and the Python hub take turns on one SQLite file, so a row one of them
writes must read correctly in the other: same text format for timestamps,
same JSON separators, same fencing generations and expiries, same digests.
A difference there never shows on the wire until the other hub reads the row.

The drive runs one scripted request sequence against a launched hub, stops
it, reads every table of the file (the trigger-maintained ones included), and
compares the rows exactly with a golden the in-repo Python hub produced. Like
the contract drive it never imports `brigade.*`; it reads the file with the
standard `sqlite3` module. The design behind it is the S2 row and the
"Schema, migrations and data compatibility" section of
[`docs/phase-v2-hub-architecture.md`](../../../../docs/phase-v2-hub-architecture.md).

## Sub-features

- **A stepped clock.** The hub reads its wall clock from a file the drive
  writes (`{clock_file}` in the launch template). `hub_clock.py` is the Python
  hub's adapter: it loads the pure-Python `datetime` and replaces
  `time.time`, so every timestamp, lease expiry and retention cutoff comes
  from the drive. Monotonic clocks stay real. The clock moves before every
  request, alternating whole and quarter seconds, so stored timestamps use
  both of Python's `isoformat()` shapes (with and without a fraction).
- **Three phases with a restart between each.** Phase 1 (the start) enrolls
  three nodes and two message principals, posts run events, upserts a session,
  runs claims through acquire, refusal, renew, release and re-acquire, sends
  mail and a reply, asks a question the operator answers, and pages `who` and
  `claims` so the paging snapshots exist. Phase 2 (two days in) publishes keys,
  sends, acknowledges and answers sealed mail, replies to phase-1 mail, asks
  one question answered in time and one left open, re-acquires a lapsed lease
  with a new holder, and revokes a node, which the snapshot visibility
  triggers and the total trigger follow. Phase 3 (91 days in) is past the
  7-day sealed lifetime after the phase-2 acknowledgement and past the 90-day
  retention for phase-1 mail: its reads tombstone the sealed message, delete
  the old mail, detach the reply that pointed at it, trim the send ledger,
  time out the open question, and take an operator reply that lands late.
- **Every table, exact values.** All 76 tables of a fresh hub are dumped,
  empty ones included. Each cell keeps its storage class (`i:5` is not
  `r:5.0`). Timestamps become their exact offset from the run's start plus
  their exact text shape, such as `<T+172803.75|YYYY-MM-DDTHH:MM:SS.ffffff+00:00>`.
  REAL epoch values near the run's start become `<E+offset>`.
- **Id mapping.** Columns that hold random values (token digests, snapshot
  ids, event ids, subscription ids, principal epochs) map to first-seen
  placeholders per id space, numbered in the table that defines them, and the
  mapping is applied inside every other column too. A question bound to the
  wrong node's token therefore shows up as a different placeholder in
  `fleet_questions.generation`. The rules live in
  `storage-parity-rules.json`, which a Rust comparator can read as is.
- **Relations, without the golden.** Node and principal token digests equal
  SHA-256 of the issued tokens. `envelope_bytes` equals the envelope length.
  A sealed row's `payload_hash` equals SHA-256 of its envelope, and a
  tombstone equals the sent envelope without `ct` and `recipients`, in
  canonical form, with the original digest kept. Plaintext digests are
  recomputed from the stored columns. Claim and session expiries equal the
  renewal or heartbeat time plus the lease. Reply links point at a message
  from the other side. No retained message is past retention. Each JSON column
  is re-encoded with the encoder Python uses for it (compact, canonical, or
  `json.dumps` defaults).
- **Paging bindings, recomputed.** `fleet_read_snapshots.query_hash` is
  derived, not random, so the golden's placeholder would hide a hub that binds
  paging differently and then rejects the other engine's cursors. Before the
  placeholder is applied, the drive recomputes every stored hash as
  `sha256(json.dumps([action, all, filter.casefold(), credential, role]))`
  (Python's default separators). The drive records, for each paging request it
  sends, the caller, the action, `all` (default false), `filter` (default
  empty) and the clock reading, and matches a snapshot to its request by
  caller and `last_access`. `credential` is the caller's stored digest and
  `role` is read from the dumped tables the way the hub classifies an
  identity: an `agent:` name takes the `role` and `token_hash` of its
  `fleet_message_principals` row, any other name is a node, role `node`, with
  its `nodes.token_sha256`. Revocation is not checked, because the request
  already succeeded while the caller was active. A wrong hash, or a snapshot no
  recorded request made, is a `query-binding` entry in `relation_failures`
  with `column: query_hash` and the snapshot's normalized id.
- **Profiles.** `defaults` runs the hub with its default flags, as the contract
  doc requires of a hub under test. `outbox` sets
  `BRIGADE_FLEET_EVENTS_CALLBACK_HOSTS` to the loopback address and allows
  loopback HTTP, subscribes the agent principal to its inbox through a local
  sink that answers the verification challenge, and waits for each webhook
  outcome before the clock moves, so the outbox, delivery, attempt and
  verification rows compare too. `migrated-v34` and `migrated-v22` start from
  the schema and seed rows of the v0.30.0 and v0.27.0 releases
  (`storage-parity-fixtures/`), installed 30 days before the run, so the hub
  upgrades the file at startup before the sequence runs.
- **Mixed engines.** `--engines a,b,a` runs phase 2 on the hub `--other-hub-cmd`
  starts, on the same file. The result must equal the Python-alone golden.

## How to get to it (user POV)

Nobody runs this by hand during normal work. A user who upgrades the hub,
or later switches `fleet serve` to the Rust engine, keeps their existing
`fleet-hub.db`, and every `brigade fleet` command reads rows the other engine
wrote:

```bash
brigade fleet claims --json
brigade fleet msg list
brigade fleet status --json
```

If the Rust hub stores an expiry one second late or a JSON column with
different separators, the Python hub sorts and compares those strings
differently after a rollback, and those commands go wrong without an error.
This drive is where that shows first.

## Driving it with control-brigade

```bash
C=registry/skills/verify-brigade/control-brigade.py
$C storage-parity --dry-run                      # the engines and profile, nothing started
$C storage-parity                                # defaults profile, Python hub, compared with the golden
$C storage-parity --profile outbox
$C storage-parity --profile migrated-v34
$C storage-parity --engines a,b,a --other-hub-cmd "<template>"
$C cleanup
```

No `--target` is needed. Without one the helper makes a fresh directory under
`<state-root>/targets/`, records it for `cleanup`, and keeps `hub.db`,
`admin-token` (mode 0600), `clock`, one `hub-<phase>-<engine>.log` per phase,
and a fake `home/` there. With `--target "$TARGET"` the same files go under
`$TARGET/storage-parity/<stamp>/`.

The default template is
`{python} {launcher} --clock-file {clock_file} -- fleet serve --host {host} --port {port} --db {db} --token-file {token_file}`.
`{python}` is the interpreter behind `--brigade`, and `{launcher}` is
`hub_clock.py`. Another engine's template must contain `{port}` and
`{clock_file}`; `{host}`, `{db}`, `{state_dir}`, `{token_file}` and
`{brigade}` work as in the contract drive.

Proof, from the helper's output:

- helper exit 0, `"ok": true`, `"differences": []`, `"step_failures": []`,
  `"relation_failures": []`, `"rule_problems": []`
- `"dump_digest"` equals `"golden_digest"`
- `"tables": 76`, `"steps": 52` (54 for `outbox`)

A difference is helper exit 3 with `differences` entries naming the table,
row, column, and both normalized values, or `relation_failures` entries naming
the check. A step that got an unexpected status is in `step_failures`. A hub
that exits or never answers `/health` is helper exit 1 with an `error`.

To record a new golden after a reviewed Python schema or behavior change, run
the profile with `--write-golden` (refused together with `--hub-cmd` or
`--other-hub-cmd`, so the golden always comes from the in-repo hub alone),
then commit `storage-parity-golden.json`.

Observed on main `dc80cd01e`: all four profiles exit 0, about 2.5 seconds
each. The mixed `a,b,a` run gives the same digest as the Python-alone run.
The migrated goldens differ from `defaults` only in the eight install-time
seed stamps.

## Gotchas

- **The golden is deliberately strict.** Any change to what the Python hub
  stores, an added column included, fails this drive until the golden is
  rewritten in the same PR. That is the lockstep the cutover needs.
- **The outbox run waits on the sink.** Deliveries run on the hub's dispatcher
  thread every half second. The drive counts webhooks at the sink and waits
  until no delivery is pending before the clock moves, because event
  retention (one day) removes delivered rows by phase 3.
- **Subscriptions lapse after 7 days.** The outbox run subscribes again in
  phase 3; the phase-1 subscription and its rows are gone by then.
- **Fixtures are generated, not hand-written.** Each one is the `iterdump()`
  of a database that release's `init_db` created under `hub_clock.py` at
  30 days before the run, plus its `user_version`. `fixture_sql()` in
  `storage_parity.py` writes that form.
- **Without the sequence context, one digest is unverifiable.** Retention
  clears `reply_to` on a reply whose parent expired, but the digest still binds
  that parent. The drive knows it; a check on a bare file skips that row.
- **Never point it at a shared hub.** It launches its own hub on loopback,
  with every `BRIGADE_*` variable dropped and `HOME` pointed at a fake home.
