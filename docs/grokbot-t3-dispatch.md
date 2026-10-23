# Grok Bot T3 dispatch pack

## Platform support

The dispatch runtime requires POSIX owner-only files and no-follow opens. The
original implementation plan rejects non-POSIX owner-file reads
([plan, line 652](superpowers/plans/2026-10-16-grokbot-t3-dispatch.md#L652)).
On Windows, runtime reads, ledger creation, wake-log writes, and lifecycle
commands fail before accessing private storage with
`unsupported-platform`: "T3 dispatch is unsupported on Windows. POSIX owner-only
storage is required". Parsing and dispatch state logic are platform independent.
Native Windows storage can follow once the shared private-storage work in #1750
lands and the dispatch runtime integrates it, including SQLite sidecars.

## Purpose

Let Grok Bot bots launch and steer T3 Code threads on allowlisted fleet hosts through configured seats. Bots get a free-form dispatch connector: they write the prompt, pick the repository, host, and seat, and get woken when a thread finishes or asks a question.

## Runtime JSON

The pack instance config stores one extra key, `runtime_path`, pointing to this
owner-only JSON file. The base keys below are required. Optional keys are marked
in the reference table. Unknown keys are rejected.
Use mode `0600` for the runtime file. It must be a regular file owned by the
listener user, with no symlink or group/other permissions.

Save the runtime as `$HOME/.config/grokbot-t3/runtime.json`. Expand `$HOME` in
all JSON paths to an absolute path before saving. The loader does not expand
environment variables. Replace the example repository remote and executable
paths with your configured values.

```json
{
  "schema": "brigade.grokbot.t3-dispatch-runtime.v1",
  "enabled": true,
  "t3_fleet_bin": "$HOME/.local/bin/t3-fleet",
  "git_bin": "/usr/bin/git",
  "repositories": {
    "example-repo": {
      "remote": "https://example.com/team/example-repo.git",
      "default_branch": "main"
    }
  },
  "hosts": ["host-a", "host-b", "host-c"],
  "seats": ["dsflash", "glmflash", "sol61", "luna6", "opus55", "sonnet55"],
  "default_seat": "dsflash",
  "fallback_seat": "glmflash",
  "fleet_enrolled": false,
  "max_open": 6,
  "daily_limit": 30,
  "prompt_max_bytes": 16384,
  "ledger_path": "$HOME/.config/grokbot-t3/ledger.sqlite3",
  "wake_path": "$HOME/.config/grokbot-t3/wake.json",
  "writable_paths": ["$HOME/.local/state/t3-fleet"]
}
```

| Key | Meaning |
|---|---|
| `schema` | Must be `brigade.grokbot.t3-dispatch-runtime.v1`. |
| `enabled` | Boolean kill switch for submit, followup, answer, approve, inbox acknowledgement, reply, and send. Interrupt stays available. |
| `t3_fleet_bin` | Absolute path to the local `t3-fleet` executable. |
| `git_bin` | Absolute path to `git`, used to resolve `base_ref`. |
| `repositories` | Nonempty map of allowed repository ids to `remote` and `default_branch`, up to 50 entries. Remotes must be HTTPS Git URLs ending in `.git`. |
| `hosts` | Nonempty, unique host allowlist, in tie-breaking preference order. |
| `seats` | Nonempty, unique seat allowlist. |
| `default_seat` | Seat used when the bot omits `seat`, must be in `seats`. |
| `fallback_seat` | A different allowed seat, or `null` to disable fallback. |
| `fleet_enrolled` | Boolean controlling submit override reasons. Host selection always uses the allowlist. |
| `max_open` | Open requests across all callers, 1 through 20. Example: 6. |
| `daily_limit` | Submit and followup attempts per UTC day, including timeouts, refusals, and fallback launches, 1 through 200. Example: 30. |
| `prompt_max_bytes` | Prompt byte limit, 1024 through 32768. Example: 16384. |
| `ledger_path` | Absolute path to the pack-owned SQLite ledger. |
| `wake_path` | Absolute path to the wake config below. |
| `writable_paths` | Up to 8 additional absolute paths permitted by the service write sandbox. |
| `hub_token_file` | Optional absolute path to a Fleet Hub message principal token file, for example `/srv/t3/config/principal.token`. Must be a regular file owned by the service user, mode `0600`, with no symlink. Omit it to leave all five Hub tools closed with `hub-not-configured`. |
| `hub_wake_target` | Optional wake target name, default `default`. Must match a key in the wake config when Hub polling is enabled. |
| `hub_recipients` | Optional map of up to 16 node-name aliases to node UUIDs, default `{}`. Example: `{"host-a":"00000000-0000-4000-8000-000000000001"}`. Node ids and `agent:example` pass through unchanged. |

The service sandbox permits writes to the ledger and wake directories plus
`writable_paths`. Include the actual t3-fleet state directory explicitly, such
as `$HOME/.local/state/t3-fleet` after expansion. SSH `known_hosts` and
`ControlPath` directories may also need write access. The unit renderers do
not add these paths automatically. Doctor warns when `writable_paths` is empty.

Hub messaging uses the existing Fleet configuration's `hub_url`, the direct
Fleet message client, and the explicit principal file. No extra Hub URL or
shell command is needed. Node names resolve through `hub_recipients`, using
the same alias logic as Dot. The connector does not read broader Fleet policy
credentials to resolve names. Provision the principal and its send grants on the
Hub before enabling it. The token file contains only the token, with no trailing
newline. Do not use an admin token. An invalid file closes messaging with
`hub-error` and `hub_code: token_file_invalid`, while dispatch remains available.
The underlying Fleet client keeps queued sends and replies in a spool scoped to
the normalized Hub endpoint and credential together. A Hub switch selects a
separate queue, and replay verifies that the queue matches the captured endpoint
and credential. Older token-only or node-only queues have no proven endpoint.
They remain untouched for operator reconciliation and are never replayed using
the current Hub configuration.
Each enabled watcher tick attempts a drain after request notifications through
the existing client, whose network drain deadline is 12 seconds. Lock acquisition
waits at most 1 second, capped by the remaining 300-second unit budget after
reserving the drain deadline and 30 seconds for shutdown. If that budget is
exhausted, the tick records a failure and leaves queued mutations for retry.
A disabled connector leaves queued mutations untouched. Drain errors do not stop
inbox or request notifications.
Permit that spool directory in `writable_paths` if the service sandbox otherwise
makes it read-only. The connector never adds credential values to tool results,
request ledgers, wake payloads, logs, or subprocess arguments.

Submits and follow-ups always send `runtime_mode: full-access`. This is fixed
internally and is not a runtime JSON key or tool argument. Calls use machine
mode with argv `[t3_fleet_bin, <action>]` and a single JSON object on stdin
containing `schema: t3-fleet.request.v1`, `action`, and the action arguments.
The prompt travels on stdin as `prompt`.

If the bot omits `seat` and the default seat is rejected with `policy_rejected`
and reason `model-admission-denied`, the pack retries once on `fallback_seat`
and returns `fallback_used: true`. A bot-named seat never falls back.

With `fleet_enrolled: false`, every submit sends an explicit host and seat
without `override_reason`. For `host: auto`, the pack chooses the allowlisted
`eligible` host with the most free slots (`limit - active`). Ties follow
`hosts` order. With `fleet_enrolled: true`, the pack still picks an explicit
allowlisted host from capacity, and every submit carries `override_reason: grokbot t3-dispatch seat <seat> for <caller>`.

## Setup

Use a dedicated public hostname for this listener. Its default bind is
`127.0.0.1:8778`, and its registry `public_route` is empty. Put Cloudflare
Access and an origin bearer in front of the listener. Prepare the runtime,
wake config, bearer file, and wake key before running setup.

```bash
brigade run cloud grokbot pack setup --id t3-dispatch --target <queue-workspace> --bearer-file <file> --runtime-path <file> --allow-host <public host> --apply
brigade run cloud grokbot pack install-service --id t3-dispatch --target <queue-workspace> --out ~/.config/systemd/user
python -m brigade.grokbot_t3 install-watch --target <queue-workspace> --out-dir ~/.config/systemd/user
```

Replace each `<file>` with the corresponding absolute bearer or runtime path.
The listener unit is `brigade-grokbot-t3-dispatch.service`. The watcher uses
`brigade-grokbot-t3-watch.service` and `brigade-grokbot-t3-watch.timer`, with a
one-minute interval.

## Wake config

Save this JSON at `wake_path` with mode `0600`. The schema is
`brigade.grokbot.t3-dispatch-wake.v1`. Expand `$HOME` in `key_file` before
saving, and keep each key file owner-only with mode `0600`.

```json
{
  "schema": "brigade.grokbot.t3-dispatch-wake.v1",
  "targets": {
    "default": [
      {
        "url": "https://example.com/hooks/t3-wake",
        "key_file": "$HOME/.config/grokbot-t3/wake.key"
      }
    ]
  }
}
```

The caller name selects its target list, with `default` used when no matching
caller entry exists. The `default` target list is required. Add caller-specific
lists alongside it when needed. Each list accepts 1 through 4 targets. Wake requests use
HTTPS POST, `Authorization: Bearer <key>` and `X-Automation-Key: <key>`, an
8-second timeout, no redirects or environment proxies, and one attempt per
pending target per timer tick. A redirect counts as a failed wake. The JSON body
for request notifications contains only `request_id`, `state`, `repository`,
and `title`.

The watcher wakes on `needs_attention`, `unknown`, `succeeded`, `failed`, or
`interrupted`. A state change clears `notified_state`, so a second question
after a running stretch wakes again. A wake counts as delivered only when
every target for the caller returns HTTP 200. Only failed targets retry on the
next tick. Successes and attempt counts persist across watcher restarts and
reset on a state change. After 30 failed delivery ticks for a request and state,
the watcher marks that state notified and logs `gave-up`. After 30 consecutive
status failures, it records `unknown`, stops polling that row, and sends one
notification, subject to the same delivery retry cap. A successful status poll
resets the failure count. Results append to `wake-notify.jsonl` beside `wake_path`.

When `hub_token_file` is configured, the existing one-minute watcher also checks
the principal's unacknowledged inbox. Each target independently fetches at most
3 pages of 100 messages per tick, with a 1-second transport timeout per page and
a 3-second polling budget. At most 4 targets means at most 12 pages and 12
seconds of inbox polling per tick. New message ids trigger the target list
named by `hub_wake_target`.
The wake body contains exactly `kind: "hub-inbox"`, `count`, `latest_id`, and
`sender_ids`. It contains no subjects or message bodies. For example:

```json
{"kind":"hub-inbox","count":1,"latest_id":42,"sender_ids":["host-a"]}
```

Each target's last notified id persists in the ledger and advances only after
HTTP 200. Each target also retains at most 300 pending message ids and sender
ids, committed before delivery. A full pending batch pauses that target's scan
until delivery succeeds. Healthy targets continue discovering later messages.
Successful targets suppress unchanged notifications across restarts. Failed
wakes retry on the next tick even if another consumer acknowledges those
messages or the Hub is unavailable. Cursor advancement and removal of delivered
metadata share one transaction. A crash after HTTP 200 but before that transaction
can repeat the wake. Pending batches have no request wake retry cap. The cursor
is scoped to the effective Hub endpoint and credential value together, so a token
rotation or a Hub endpoint change starts a fresh scan, even for the same principal.
A trailing slash on the Hub URL does not change the scope. The watcher checks that
scope before and after each page fetch, before retaining metadata, and immediately
before delivery. A change stops delivery for that scan. Pending metadata from the
old scope stays in that scope, and the next tick scans under the new scope.
Cursors recorded by an earlier release under a credential-only scope are ignored,
so one upgrade can repeat a wake for messages that were still unacknowledged.
The watcher never acknowledges messages. Hub or
log-write failures do not stop later targets or request wakes. A missing configured
target reports a failure and waits for configuration to be corrected.

## Tools

| Tool | Arguments | Does |
|---|---|---|
| `inbox_list` | Optional `unacked_only` (default `true`), `limit` (1 through 50, default 20) | Read the principal inbox. Marks message content as untrusted. Scans the oldest up to 300 retained ids within 3 seconds. Returns fewer messages when the requested limit or the 131072-byte public JSON limit requires it, with `has_more: true`. `unacked_total` is exact when the scan completes and `null` when it is incomplete. Orders unacknowledged mail oldest first. With `unacked_only: false`, incomplete scans report `order: "newest first within oldest fetched window"`. |
| `inbox_ack` | `ids` (1 through 100 unique positive integers) | Acknowledge inbox messages within a shared 10-second deadline, including recount. Missing ids appear in `not_pending`. |
| `inbox_reply` | `msg_id`, `body` | Reply without acknowledging the original message. Body limit: 8192 UTF-8 bytes. |
| `message_send` | `to`, `subject`, `body`, optional `ref`, `priority`, `thread`, `kind`, `client_id` | Send within principal grants to a node name such as `host-a`, a node id, or `agent:example`. Subject limit: 200 UTF-8 bytes. Body limit: 8192 UTF-8 bytes. Priority: `low`, `normal` (default), `high`, `urgent`. Kind: `message` (default), `question`, `completion`. `operator_question` is reserved for the hub and rejected here. Supply a stable `client_id` when retrying the same send. |
| `message_status` | `ids` (1 through 50 unique positive integers) | Read acknowledgement status for sent messages. Omits bodies. |
| `t3_capacity` | none | `t3-fleet capacity`, projected to host, active, limit, state. Read-only. |
| `t3_submit` | `repository`, `prompt`, `title`, `caller`, optional `host` (default `auto`), `seat`, `base_ref`, `interaction_mode` (`default` or `plan`) | Validates against allowlists and limits, resolves `base_ref` (default the repository's default branch) to a 40-character SHA with `git ls-remote` in the controller's checkout, picks an allowlisted host when `auto`, including in enrolled mode (most free capacity among allowlisted eligible hosts), prepends the dispatch header, runs `t3-fleet submit` in machine mode with one `t3-fleet.request.v1` JSON object on stdin carrying the prompt and `runtime_mode: full-access` (the prompt never reaches argv), records the request in the ledger, returns `request_id`, host, seat. |
| `t3_status` | `request_id` | `t3-fleet status`, projected: state, `needs_attention`, `pending_request` (as `t3-fleet` bounds it), thread id, seat, last update. |
| `t3_wait` | `request_id`, `timeout_s` (max 60) | Bounded `t3-fleet wait`. |
| `t3_followup` | `request_id`, `message` | `t3-fleet followup` in machine mode, with the message and `runtime_mode: full-access` on stdin. |
| `t3_answer` | `request_id`, `input_id`, `answers` | `t3-fleet answer` for the pending user-input request named by `input_id` (from `pending_request.id`). |
| `t3_approve` | `request_id`, `approval_id`, `decision` | `t3-fleet approve` for the pending approval named by `approval_id`; `decision` is `accept`, `acceptForSession`, `decline`, or `cancel`. |
| `t3_interrupt` | `request_id` | `t3-fleet interrupt`. |
| `t3_recent` | `caller` optional, `limit` (max 20) | Ledger rows for this pack: request, caller, repository, host, seat, state, times. Read-only. |

A later-page outage preserves messages already fetched and sets `has_more: true`.
Credential rotation, a Hub endpoint change, or an invalid credential closes the
whole foreground scan with `hub-error`, discarding earlier rows.
A first-page failure returns the closed `hub-error`. Acknowledge fetched oldest
unacknowledged messages to progress through a larger backlog. If acknowledgement
succeeds but its recount has a transport failure or remains incomplete, `inbox_ack` still returns
`acked` and `not_pending`, with `unacked_total: null` and `has_more: true`. Repeating an acknowledgement of
an existing message is idempotent and reports it in `acked`. `not_pending` covers
messages the Hub reports missing. Each acknowledgement gets at most 5 seconds
and the time remaining in the batch's 10-second deadline. If that deadline
expires or a later acknowledgement has a transport or grant failure, the result preserves confirmed
`acked` and `not_pending` ids and adds `incomplete: true` and `remaining_ids`.
The remaining ids include an uncertain acknowledgement, which is safe to retry.
Incomplete batches skip recount and return `unacked_total: null` and
`has_more: true`. A first-call authentication or grant refusal stays a closed
`hub-error`.

Each acknowledgement batch and its recount stay bound to the initial Hub endpoint
and credential. Rotation, a Hub endpoint change, or an invalid credential closes
the whole call with `hub-error`, stops further acknowledgements, and omits partial
results and counts. Earlier acknowledgements may already have committed. Confirm
the intended principal and Hub before retrying those ids. The operation captures its endpoint, credential and node identity once. Nested
pages, acknowledgements, recounts and replay steps receive that same immutable
authority. The transport compares it with current credentials immediately before
each request, including after lock waits. Current settings never replace the
captured authority. Public page iterators capture when called, before iteration.

Hub tools use Dot's argument validators and message projections. A send without
`client_id` derives one from its content and a ten-minute time bucket. Reusing
an explicit id preserves the Fleet client's idempotency and offline queue
behavior. Replies derive a stable id from the original message id and body.
Hub refusals return the closed `hub-error` plus a recognized `hub_code`, such as
`message_recipient_not_permitted` or `message_sender_recipient_quota`. Raw Hub
exception text is omitted. A grant refusal means nothing was sent or queued.

`caller` is the bot's own name and selects its wake targets. The title prefix
is `[grokbot:<caller>]`. Tools taking `request_id` accept only requests in this
pack's ledger. `base_ref` is a branch name only, resolved under `refs/heads/`.
Git tags and SHAs are not resolved as revisions.

Followups count toward `daily_limit`. Reopening a terminal request also checks
`max_open`. A submit with a known request id but an error or unrecognized state
is recorded as `unknown` and returns `submit-unknown` with that id. Submit
attempts without a recoverable id still count toward `daily_limit` and hold
one `max_open` slot for two hours, because the thread may be running. Do not
resubmit an unknown outcome. Ask the operator to inspect and recover it through
`t3-fleet`.

## Kill switch

Set `"enabled": false` in the runtime JSON, then restart the listener because
it reads the runtime once at start:

```bash
systemctl --user restart brigade-grokbot-t3-dispatch.service
```

Submit, followup, answer, approve, inbox acknowledgement, reply, and send then
return `dispatch-disabled`. Inbox reads, sent status, and wake polling remain
available.
`t3_interrupt` stays available while disabled so it can stop active work.
Read tools remain available. For an immediate hard stop:

```bash
systemctl --user stop brigade-grokbot-t3-dispatch.service cloudflared-grokbot-t3.service
```

## Security

The owner accepted free-form dispatch: an outside bot writes prompts that run with `full-access` on owner machines. The controls are the repository, host, and seat allowlists; `max_open` and `daily_limit`; the kill switch; Access service token plus origin bearer; Host and Origin allowlisting; the ledger restricting steering to the pack's own requests; and a prompt hash audit trail. Prompts never reach logs, the ledger, wake bodies, or the Fleet Hub.

## Issue intake

The optional top-level `intake` object enables a one-shot pass that submits open
GitHub issues carrying an approval label. Omitting it disables intake. All other
runtime keys stay required, and unknown keys are rejected. For example, add this
object to the runtime JSON:

```json
"intake": {
  "label": "grokbot-build-approved",
  "queued_label": "grokbot-build-queued",
  "repositories": ["example-repo"],
  "caller": "Builder",
  "gh_bin": "/usr/bin/gh",
  "max_per_pass": 1,
  "headroom": 2
}
```

Each listed repository must already appear in the runtime's `repositories` map
with an HTTPS GitHub remote such as `https://github.com/example/example-repo.git`.
Repository ids must be unique. Both labels must be different, begin with a
letter or digit, contain only letters, digits, `.`, `_`, `:`, or `-`, and be at
most 50 characters. `caller` follows the existing caller rules. `gh_bin` must
be absolute. `max_per_pass` accepts 1 through 5, and `headroom` accepts 1 through
`max_open - 1`. Intake and the listener run in separate processes, so their slot
checks can race; a headroom of at least 1 keeps that race inside `max_open`.

Run a pass or write its systemd user units:

```bash
python -m brigade.grokbot_t3 intake --target <queue-workspace>
python -m brigade.grokbot_t3 install-intake --target <queue-workspace> --out-dir <unit-directory>
systemctl --user daemon-reload
systemctl --user enable --now brigade-grokbot-t3-intake.timer
```

Create both labels in each repository and authenticate `gh` as the service user
with permission to list issues and edit their labels. The timer starts five
minutes after boot and runs every fifteen minutes. Its oneshot service has a
900-second timeout and the listener's filesystem hardening. Installation writes
`brigade-grokbot-t3-intake.service` and `.timer`; use `--force` to replace changed
units.

Intake uses no Grok calls. It fetches labeled open issues oldest-first, up to 100 per repository,
processes repositories in configured order and issues by ascending number, and
submits at most `max_per_pass`. It checks headroom before each submit and stops
the pass when fewer than `headroom + 1` open slots remain.
The existing dispatch limits and default/fallback seat rules
still apply. One `t3-seat:<seat>` label, read from the snapshot's current labels, selects an
allowlisted seat. Multiple seat
labels or an unknown seat produce `bad-seat-label` without submitting.

The worker receives a snapshot of the title, body, and comments from owners,
members, and collaborators written before the latest approval label event.
Comments by others, added after approval, or edited after approval are dropped.
Timestamps have one-second resolution, so a comment, body edit, or title rename
stamped in the same second as the approval counts as after it.
An issue whose body was edited or title renamed after approval is refused with `edited-after-approval`;
re-apply the label to approve the new text. Missing approval events produce
`approval-not-found`, and failed or malformed snapshots produce `snapshot-failed`.
The snapshot rechecks that the issue is still open (`issue-closed`) and that the
approval label is still on it (`approval-revoked` if it was removed after listing), and an issue with more than
100 comments or more than 100 labels is refused with `too-many-comments` or
`too-many-labels` rather than truncated. A pass starts new work only while the
worst case for one more issue still fits before the unit timeout, and otherwise
ends with reason `deadline`, so systemd never stops it partway through an issue.
The worker follows the snapshot without fetching the issue or comments again,
implements and tests the change, and opens a pull request without merging.
Issue titles, bodies, and comments never enter intake's ledger or summary.
The existing wake watcher reports the thread result to `caller`.

After a successful submit, intake removes the approval label and adds the queued
label. A failed label swap leaves a persistent claim, shown as `held` on later
passes, to prevent another launch. An uncertain submit also retains its claim
and stops the pass. Claims do not expire. Before clearing one, an operator must
inspect the request and issue labels, then call `Ledger.release_intake` with its
`<repository-id>#<number>` key. Clearing an unresolved claim can launch the same
issue again. Definite submit errors release the claim; capacity, daily-limit,
unavailable, disabled, and `no-eligible-host` errors stop the pass.

The command prints a JSON summary with `reason`, `submitted`, `held`, and
`errors`. GitHub failures report stable codes without command output. A failed
label swap adds `"label_swap": "failed"` to the submitted record. Exit status is
1 when `errors` is nonempty and 0 otherwise, including held claims and failed
label swaps. Invalid configuration exits 2. Setting `enabled` to false also
disables intake.
