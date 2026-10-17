# Grok Bot T3 dispatch pack

Status: approved by the owner, 2026-10-16.

## Goal

Let Grok Bot bots launch and steer T3 Code threads on the operator's three fleet hosts through Codex, Claude Code, and OpenCode seats. Bots get a free-form dispatch connector: they write the prompt, pick the repository, host, and seat, and get woken when a thread finishes or asks a question.

This replaces the Builders' old execution path, where each job spawned a Grok Bot cloud agent and held the bot for about an hour. Execution moves to our own subscriptions through `t3-fleet`. The bot becomes a coordinator.

## Decisions already made

- Default seat is DeepSeek V4.1 Flash through OpenCode, with GLM 5.3 Flash second, and the Codex and Claude seats available on request (owner, 2026-10-16). It was the most-used model on OpenRouter for the week ending 2026-10-15 (37.1T tokens, first of all models), and its OpenCode free tier keeps no data.

- Free-form dispatch, chosen by the owner on 2026-10-16. There is no queue job, no approval label, and no envelope. Any bot in the account can call the connector, because Grok Bot connectors are account-wide.
- The pack builds on Brigade's Grok Bot connector-pack system (`grokbot_packs.py`, role-scoped listeners, bearer plus Cloudflare Access, `RequestGate`). The Miller dot bridge (`dot-t3-mcp`) is not reused or changed.
- Execution goes through the local `t3-fleet` client only, following the `t3-fleet-dispatch` skill contract. The pack never talks to a T3 server, SSH, or a worktree directly.
- Public hostname: `t3-dispatch.brigade.tools` on a new Cloudflare tunnel, `cloudflared-grokbot-t3`. All fleet endpoints stay under `brigade.tools` (owner decision 2026-10-16).
- Merging stays with the Codex merge lanes. Dispatched threads open PRs but do not merge.

## Architecture

```text
Grok Bot (any bot)
  -> https://t3-dispatch.brigade.tools/mcp
     Cloudflare Access service token + origin bearer
  -> cloudflared-grokbot-t3.service (new tunnel)
  -> brigade-grokbot-t3-dispatch.service, 127.0.0.1:8778 (new pack listener)
  -> t3-fleet submit/status/wait/followup/answer/approve/interrupt (subprocess)
  -> T3 thread on host-a | host-b | host-c, seat dsflash | glmflash | sol61 | luna6 | opus55 | sonnet55

brigade-grokbot-t3-watch.timer (every 1 min)
  -> reads the pack ledger, runs t3-fleet status for open requests
  -> on finished or needs-input: POST wake to the calling bot's webhook routine
```

## Pieces

### 1. Pack `t3-dispatch` (Brigade)

- New module `src/brigade/grokbot_t3/` with `contracts.py` (argument validation), `fleet.py` (the only `t3-fleet` subprocess wrapper), `ledger.py` (SQLite), `tools.py` (tool handlers), `watch.py` (wake watcher).
- Register in `grokbot_packs.py`: pack id `t3-dispatch`, version `1.0.0`, `CONNECTOR_DEFAULT_BINDS["t3-dispatch"] = "127.0.0.1:8778"`, public route `""` (the registry rejects overlapping routes and `obsidian-operator` owns `/mcp`; this pack gets its own hostname instead). The registry stays closed.
- Instance config `.brigade/grokbot/packs/t3-dispatch.json` adds one key to the shared set, `runtime_path`, pointing at an owner-only runtime JSON that holds these keys:
  - `t3_fleet_bin`: absolute path to `t3-fleet`.
  - `repositories`: allowlist of t3-fleet repository ids. Default: every repository in the t3-fleet config.
  - `seats`: allowlist. Default `["dsflash", "glmflash", "sol61", "luna6", "opus55", "sonnet55"]` in that preference order. Default seat `dsflash`, second choice `glmflash`. When a `dsflash` submit is refused with `policy_rejected` / `model-admission-denied` (the free model is not admitted), the pack retries once on `glmflash` and reports the substitution in the result. A rate limit that hits inside a running thread surfaces as a failed or stalled thread, not as a submit error, and is handled by the bot after the wake. The Codex and Claude seats are used only when the bot names them.
  - OpenCode seats are limited to free or Go models whose provider keeps no data and does not train on prompts, per the OpenCode models page on 2026-10-16. New roster seats: `dsflash` = `opencode/deepseek-v4.1-flash-free` (200K context, free for a limited time, no end date published) and `glmflash` = `opencode/glm-5.3-flash-free` (free for a limited time). `opencode-go/deepseek-v4.1-flash` (Go subscription, 1M context, $80 a month cap) is not a seat in this version. Excluded because the provider may train on or log prompts: `muse13` (Meta contributor tier), Big Pickle, LL Alpha, Exo, MiMo free models, and the NVIDIA trial Nemotrons.
  - `hosts`: allowlist. Default: every fleet host configured in `t3-fleet` (for example `["host-a", "host-b", "host-c"]`).
  - `runtime_mode`: fixed `full-access`. Not a tool argument.
  - `fleet_enrolled`: `false` today. When false, the pack always sends an explicit host and seat and no override reason. When true, the pack still picks an explicit allowlisted host from capacity for `host: "auto"`, and each submit carries an override reason. The host allowlist remains authoritative.
  - `max_open`: default `6` open requests across all bots. `daily_limit`: default `30` submit and followup attempts per UTC day, including timeouts, refusals, and fallback launches.
  - `prompt_max_bytes`: default `16384`.
  - `ledger_path`, `wake_path` (see piece 3).
  - `enabled`: kill switch. When false, submit, followup, answer, and approve return `dispatch-disabled`. Interrupt stays available to stop active work, and read tools remain available.

### 2. Tools

| Tool | Arguments | Does |
|---|---|---|
| `t3_capacity` | none | `t3-fleet capacity`, projected to host, active, limit, state. Read-only. |
| `t3_submit` | `repository`, `prompt`, `title`, `caller`, optional `host` (default `auto`), `seat`, `base_ref`, `interaction_mode` (`default` or `plan`) | Validates against allowlists and limits, resolves `base_ref` (default the repository's default branch) to a 40-character SHA with `git ls-remote` in the controller's checkout, picks an allowlisted host when `auto`, including in enrolled mode (most free capacity among allowlisted eligible hosts), prepends the dispatch header, runs `t3-fleet submit` in machine mode with one `t3-fleet.request.v1` JSON object on stdin carrying the prompt and `runtime_mode: full-access` (the prompt never reaches argv), records the request in the ledger, returns `request_id`, host, seat. |
| `t3_status` | `request_id` | `t3-fleet status`, projected: state, `needs_attention`, `pending_request` (as `t3-fleet` bounds it), thread id, seat, last update. |
| `t3_wait` | `request_id`, `timeout_s` (max 60) | Bounded `t3-fleet wait`. |
| `t3_followup` | `request_id`, `message` | `t3-fleet followup` in machine mode, with the message and `runtime_mode: full-access` on stdin. |
| `t3_answer` | `request_id`, `input_id`, `answers` | `t3-fleet answer` for the pending user-input request named by `input_id` (from `pending_request.id`). |
| `t3_approve` | `request_id`, `approval_id`, `decision` | `t3-fleet approve` for the pending approval named by `approval_id`; `decision` is `accept`, `acceptForSession`, `decline`, or `cancel`. |
| `t3_interrupt` | `request_id` | `t3-fleet interrupt`. |
| `t3_recent` | `caller` optional, `limit` (max 20) | Ledger rows for this pack: request, caller, repository, host, seat, state, times. Read-only. |

`caller` is the bot's own name (for example `Builder`). It sets the title prefix `[grokbot:<caller>]` and selects the wake target. Tools that take `request_id` only accept ids in this pack's ledger, so a bot cannot steer threads it did not launch through this connector.

The dispatch header prepended to every prompt:

```text
Dispatched by Grok Bot "<caller>" through Brigade t3-dispatch.
Owner-authorized: implement, test, commit, push a branch, and open a pull request.
Do not merge. Stop and ask only for spending, new external accounts, publication outside the GitHub PR flow, destructive data loss, or credentials.
```

### 3. Ledger and wake watcher

- Ledger: SQLite at `ledger_path`, table `requests(request_id, caller, repository, host, seat, title, prompt_sha256, created_at, last_state, notified_state, notified_at)`. Prompt text is never stored or logged; only its SHA-256.
- Wake config `wake_path` (0600): `{"targets": {"<caller>": [{"url": "...", "key_file": "..."}], "default": [...]}}`. Same POST contract as the existing Builder wake: `Authorization: Bearer <key>`, `X-Automation-Key: <key>`, JSON body, 8 s timeout, one try per tick.
- `python -m brigade.grokbot_t3 watch --target <queue-workspace>` runs once per timer tick. For each ledger row whose state is not terminal, or terminal but not yet notified, it runs `t3-fleet status`. It wakes the caller when the state changes to `needs_attention` (question or approval pending), `unknown`, or a terminal state (`succeeded`, `failed`, `interrupted`). Body: `{"request_id", "state", "repository", "title"}`. The bot reads PR details through `t3_status` or GitHub. No thread content. A `default` target list is required. Wake POSTs refuse redirects and environment proxies. Delivery successes are persisted per target for the current state, so only failed targets retry on the next tick. After 30 failed delivery ticks for the same request and state, mark it notified and log `gave-up`. After 30 consecutive status failures, record `unknown`, stop polling that row, and send one notification with the same delivery cap. Successful status calls reset the failure count. State changes clear delivery successes and attempts. Results append to `wake-notify.jsonl` beside `wake_path`.
- Service write access must explicitly include the t3-fleet state directory in `writable_paths`. SSH `known_hosts` and `ControlPath` directories may also require writes. Empty `writable_paths` produces a doctor warning. Doctor also checks wake-config loading and ledger writability.
- Units use `After=network.target`: `brigade-grokbot-t3-dispatch.service`, `brigade-grokbot-t3-watch.service` (oneshot), `brigade-grokbot-t3-watch.timer` (`OnUnitActiveSec=1min`), rendered by `pack install-service` (listener) and `python -m brigade.grokbot_t3 install-watch` (watcher).

### 4. Edge

- New remotely managed tunnel `grokbot-t3` in the `brigade.tools` account, token file `~/.config/grokbot-t3/cloudflare-tunnel.token`, unit `cloudflared-grokbot-t3.service` with `Wants=brigade-grokbot-t3-dispatch.service`. Ingress `t3-dispatch.brigade.tools -> http://127.0.0.1:8778`.
- Cloudflare Access application on `t3-dispatch.brigade.tools` with a service-token policy and "Return 401 Response" set, so unauthenticated probes get 401 like the other edges.
- Listener `--allow-host t3-dispatch.brigade.tools`, origin bearer in `~/.config/grokbot-t3/origin.bearer` (0600).
- The existing `cloudflared-grokbot-dispatch` tunnel is not touched; it carries Miller's bridge.

### 5. Grok Bot side

- Add one connector named `t3-dispatch` through the chat plus secure-card flow with three headers: `CF-Access-Client-Id`, `CF-Access-Client-Secret`, `Authorization`.
- Builder and Builder 2 routines: poll and wake routines are rewritten to "decide work, call `t3_submit`, end the run". On a wake: `t3_status`; answer or approve when the question is inside the owner authorization above; when succeeded, check the PR and CI and report through operations-relay. Routine text is saved in the operator's private notes before it goes into the app.
- Each bot that dispatches gets its own webhook routine, registered as a `targets` entry under its `caller` name.

## Prerequisite: T3 version gate

All three hosts run T3 0.0.46 nightlies (audit 2026-10-16). The installed `t3-fleet` accepts only 0.0.30 to 0.0.40, 0.0.43, 0.0.45 and 0.0.46, and upstream is now at 0.1.x. A host that updates to 0.1.x drops out of dispatch. Before acceptance, either widen the `t3-fleet` gate for 0.1.x (wire protocol is still 2) or hold host T3 updates. `t3_capacity` surfaces an ineligible host as is; the pack adds no version logic of its own.

## Failure handling

- A `t3-fleet` refusal returns the closed public code `fleet-error`, with the `t3-fleet` code in `fleet_code`, its reason in `fleet_reason`, and the request id when one exists. The public `error.code` vocabulary stays closed so bots can rely on it; `t3-fleet` codes (`host_unavailable`, `policy_rejected`, `invalid_request`, and the rest) are read from `fleet_code`. The pack adds `seat-not-allowed`, `host-not-allowed`, `repository-not-allowed`, `prompt-too-large`, `open-limit`, `daily-limit`, `dispatch-disabled`, `unknown-request`.
- Submit timeout 60 s, other calls 30 s. On a submit timeout the pack returns `submit-unknown` with the request id when known and never resubmits. Recovery follows the skill: `t3-fleet recover`.
- A watcher failure never touches a thread. A listener crash leaves running threads running; the ledger lets the next start resume wakes.
- `base_ref` is a branch name only, resolved under `refs/heads/`. Git tags and SHAs are not resolved as revisions. An unresolved branch fails before anything launches.
- Every submit and followup attempt is persisted separately from successful launches. A followup reopening a terminal request must pass `max_open` before launch.
- An error envelope with a request id, or a submit result with an unrecognized state, records that request as `unknown` and returns `submit-unknown` with its id. Unknown outcomes must be inspected and recovered by the operator through `t3-fleet` before retrying.

## Security

The owner accepted free-form dispatch: an outside bot writes prompts that run with `full-access` on owner machines. The controls are the repository, host, and seat allowlists; `max_open` and `daily_limit`; the kill switch; Access service token plus origin bearer; Host and Origin allowlisting; the ledger restricting steering to the pack's own requests; and a prompt hash audit trail. Prompts never reach logs, the ledger, wake bodies, or the Fleet Hub.

## Testing

- Unit tests with a fake `t3-fleet` executable that records argv and returns canned JSON: argument validation for every tool, allowlists, limits, host auto-pick, `base_ref` resolution, header prepending, prompt never on argv, error passthrough, `unknown-request`.
- Ledger and watcher tests with a fake webhook server: wake on `needs_attention` and terminal states, no wake on unchanged state, retry after a non-200, `notified_state` written only after 200, no prompt text in the body.
- Pack registry tests in `tests/test_grokbot_packs.py` extended for the new pack id, bind, and route.
- `pack canary --pack t3-dispatch`: anonymous rejection and exact tool inventory.
- External proof from the controller host per the connector proof recipe: authenticated `initialize`, `tools/list`, then `t3_capacity`.
- Live acceptance: one bot dispatches a docs-only task to `canary-repo` on each host, once per provider family (OpenCode `dsflash`, Codex `sol61`, Claude `sonnet55`), and receives a wake for each. If an OpenCode seat cannot launch through T3 on a host, it is removed from that host's effective allowlist and reported.

## Later phases (combined plan)

Phases 2 and 3 add Fleet Hub message tools to this pack (an `agent:grokbot` inbox that can message `agent:miller`) and let the watcher wake the Conductor on new hub messages. Phase 4 moves Miller's bridge into Brigade as a `miller` pack that imports `grokbot_t3.fleet`, so `fleet.py` must stay free of Grok-specific assumptions: no caller names, wake logic, or Grok Bot headers in that module.

## Out of scope

- Changes to the Miller dot bridge or `cloudflared-grokbot-dispatch`.
- Fleet Hub enrollment of the controller (`fleet_enrolled` covers it when it lands).
- The `t3-fleet` local queue and `tick` timer.
- Merge automation and the Merge Shepherd bot.
- The legacy implementation-worker queue path. Its code stays; Builders stop using it.
