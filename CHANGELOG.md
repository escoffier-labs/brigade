# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.31.0] - 2026-11-14

- Add explicit opt-in Duet dispatch with seven fleet tools, repository and prompt bounds, and atomic open-request and daily-attempt limits in private state. Approval, answer and doctor relays remain unavailable; default discovery stays at 22 tools.

- Answer a Dot-family `tools/call` with out-of-range or unsupported arguments (for example `inbox_list` with `limit` above 50) with a JSON-RPC `-32602` error that names the argument, instead of an opaque `request_denied`, and record the allowlisted argument summary in the call log.
- Push up to three memory card hits into Dot and Duet `inbox_list` and `fleet_digest` responses as an optional `memory_hits` field: card path plus wrapped title and tags, no bodies, with the query derived from the item shown and logged only as an HMAC. Turn it off per connector with `"memory_push": false` in the runtime JSON.
- Add diagnostic-only `memory reflect preflight` reports for retained candidates, with bound policy and snapshot hashes, supporting-proof checks and explicit holds for missing promotion authority. Automatic promotion remains disabled.
- Guard can use an optional local `brigade-guard` PCRE2 JIT binary for large ASCII inputs, with Python regex fallback and unchanged policy decisions.

- Add a `grokbot-memory` connector pack for Grok Bot on the Dot connector code. It registers only `memory_search`, `memory_card` and `handoff_write`, has its own runtime schema and Hub principal `agent:grokbot-memory`, and runs no inbox poll. See `docs/grokbot-memory-connector.md`.
- Stop the content guard from blocking code-shaped hostname lookalikes: when the dotted value starts with a well-known JavaScript receiver (`args`, `process`, `this`, `options` and similar), an indented `key: value,` object member ending the line or a `delete`, `typeof`, `void` or `await` operand ending a statement is code. The `home-path` rule no longer matches a known tool config directory rendered directly under a sandbox home root with no username segment. Real hosts in YAML, shell and prose, and real user home paths including `.`-segment and hidden homes, stay detected.
- Fence new key publication and sealed admission to an installed Hub identity, retaining per-signer sequence history, paired migration readback and exact retained retries. Older Hub binaries must not rewrite identity metadata after history exists: inconsistent signer state refuses new mesh writes and requires operator recovery from a consistent backup without discarding signer history.
- Allow writing and frontend task roles to use explicit Hub policy seat fallbacks and optional host-local instructions files, with bounded reads and fail-closed dispatch.
- Add a UTF mode to the optional `brigade-guard` engine for Hub scanning (PCRE2 UTF and UCP, character and byte offsets, linked PCRE2 and Unicode versions in the response). Twelve default rules that differ from Python `re` on Unicode text are listed with reasons in `brigade.guard.utf_parity`; the local guard still sends only ASCII to the engine, and ASCII responses are byte-identical.
- Stop the Fleet Hub's 8-connection per-source handler cap from capping the whole fleet behind a local HTTPS front. On a loopback bind, a loopback peer such as Tailscale Serve is exempt and bounded by the 32-handler global cap, of which long-poll message waiters hold at most 8. Other peers, including loopback peers of a non-loopback bind, keep the 8-connection cap (#2229).

- Add `brigade work import bd` to mirror a Beads (`bd`) project's issues and dependency edges into the task ledger, keyed by bd id, so `work ready` and `work claim` honor bd blockers. It runs only `bd version --json` and `bd export`, pins the bd 1.3.x export schema, and fails closed on anything else. Add `brigade work export bd-outcomes` to write closeout receipts and PR links per bd issue id as JSONL. The `--from-bd-jsonl` inbox import now uses the same version pin (#2209).
- Point package homepages to https://brigadestack.com for the next release, retaining documentation links on brigade.tools.
- Add the verify-brigade `storage-parity` drive for the Fleet Hub: one scripted sequence on a stepped clock, every table dumped and compared exactly with a checked-in golden (timestamp shape and offset, id mapping, recomputed digests, sealed tombstones past the 7-day lifetime), with defaults, events-outbox, migrated-fixture and mixed-engine runs.

- Use `brigade components list|discover|verify` as the catalog command. Keep `stations` as a supported alias hidden from root help, with unchanged `station.json` manifests, schemas and JSON keys.

- Keep reflection duplicate groups intact after reviewed claim anchoring, while preserving literal suffixes in excluded Markdown and original source hashes.
- Add the bundled `fleet-hub-upgrade` skill: a generic Hub-first upgrade checklist covering the node-to-Hub payload and `SCHEMA_VERSION` pre-flight diff, snapshot-and-switch Hub deploy with rollback, and node verification (#2167).

- Add a read-only `grokbot-memory` connector pack for Grok Bot on the Dot connector code. It registers only `memory_search` and `memory_card`, has its own runtime schema and Hub principal `agent:grokbot-memory`, and runs no inbox poll. Its results carry `content_trust: "untrusted"`, and a prompt-load scan (guard default rules, invisible and bidi characters, instruction-override heuristics) replaces any hit with `{card_id, blocked: <rule id>}`. Dot and Duet `memory_search` matches and `memory_card` results now carry `content_trust: "untrusted"` too. See `docs/grokbot-memory-connector.md`.
- Refuse memory proposals whose retained source cards point `superseded_by` at a source deleted by a duplicate merge, including during live review and apply validation.
- Removed Jules cloud provider
- Count a T3 thread and its precisely linked Claude or Codex presence once in Fleet session lists and machine totals. Publish presence from the session root and stop renewing legacy tool-attributed checkout records. Clear stale provider links after completed lookups while retaining them when lookups are unavailable.

- Add `context handoff --json` for body-free, locally bound Codex instruction accounting, complete source hashes, and selection observations.

- Document native T3 result closeout and recovery of reviewed work notes after a session ends.
- Verification plans now validate argv-array checks without crashing, reject embedded NUL arguments, and emit runnable POSIX-shell suggestions retaining selected checks, the target and the original manifest selector.
- Fleet Hub Overview shows an idle machine's last run report age beside its separately labeled telemetry age, including `never` and `unknown` history.
- Dot session views mark reported task content as untrusted data and warn against treating it as instructions.
- Fleet attention expires abandoned unknown and stale runs using the configured history horizon, excludes revoked nodes and expired failures, and compares failure lookback and newer successful runs by elapsed time across timestamp offsets.
- Add an opt-in telemetry heartbeat for proven-open local Claude Code and interactive Codex sessions, reusing their last published presence while the owning process lives.
- Add an opt-in telemetry heartbeat for proven-open local Claude Code and interactive Codex sessions, reusing their last published presence while the owning process lives. Repeatable `--worktrees-of` discovery selects wired worktrees with presence sidecars, newest first, within the shared target cap and work budget.

- Prevent run archive import/export from modifying their own inputs when paths overlap, and retain existing archives when a forced export fails validation or promotion.
- Evidence query commands accept leading engine flags, and code graph commands and search aliases preserve literal `--target` arguments after `--` without changing the selected repository.
- Use linked list and detail panes for roster, policy, and machines, including selection without JavaScript.
- Classify writing and frontend tasks locally and, with a configured Fleet Hub, route them through effective policy roles without seat fallback. Preserve explicit worker selection and ordinary standalone routing, and retain categorical task evidence through health refusal.

- Antigravity seat health runs `agy models` once per probe for both the authentication and model checks, allows it up to 10 seconds inside the per-seat deadline, and reports an expired listing as a timeout. Authentication errors, nonzero exits, empty or incomplete listings, and a missing requested model still fail the seat.

- Fleet Hub capacity views count live interactive agent sessions per machine, and a machine with running sessions but no managed jobs reads `working` instead of `idle`.

- Dot policy reads report unavailable required sources, and Fleet digests distinguish partial data from complete results.

- Fleet Hub HTTP routes register through a route table (`fleet_hub_routes`): each route module adds its own methods, exact paths and prefixes, an exact path wins over the longest matching prefix, and a second claim on one route is refused. The bearer JSON API moves to `fleet_hub_api_http`. Responses are unchanged.

- Runbook cancellation cleans up owned process groups without hanging on blocked captures, and binary pin checks resolve each step's first command token while accepting equal-content path changes.

- Dogfood config round-trips Windows paths while retaining legacy unquoted values.
- Native component readers accept Brigade Code Graph and Evidence identities alongside legacy names, preserve recorded asset coordinates for rollback, and reject duplicate families or conflicting binary overrides.
- `brigade dogfood` reads the TOML literal strings it now writes without applying escapes, so Windows paths in `.brigade/dogfood.toml` keep their backslashes.

- Add `brigade bug diagnose` to compare a selected project's MCP catalog with one harness configuration, with explicit local finding export or report preparation.
- Security TOML literal strings preserve Windows path backslashes while double-quoted strings continue to decode escapes.
- Claude Stop hooks attribute shell writes to each affected worktree and accept verification receipts there, without requiring a receipt in an unchanged session directory.
- Fleet Hub empty states show a short title and one line of page-specific guidance, such as the command that fills the section, through a shared `empty_state` helper (#2056).

- Claude work-loop hooks retain explicit source-write attribution beside routed verification and Brigade run commands, while preserving internal logging and final handoff behavior.
- Native null-device redirects are excluded from Claude hook write detection on Windows as well as POSIX.
- Final handoff writes preserve the source-verification boundary when verification output also streams through Brigade-internal log pipelines.

- Claude work-loop hooks no longer treat descriptor duplication (`2>&1`, `>&2`), `/dev/null`, or redirects to files outside the repository as repository writes, so read-only commands stop re-arming the closeout verification gate.

- Use Brigade Stack in current core guides and starter guidance. New default and packaged starter rosters use `orchestrator`, with `orchestrator_local` as the budget fallback. Existing `chef` seats, flags, storage fields, commands, and integrations remain compatible.

- Add dormant task launch claims with immutable dispatch bindings, strict task-version checks, and retained consumption through checkpoint recovery and redaction (#2046).
- Fleet preference settings accept `--orchestrator` as the preferred flag, retaining `--chef` as an alias and the existing `chef` storage key.
- Target durable steer and interrupt requests by task and optional attempt, with selector-aware replay, checkpoint validation, and task redaction.

- MCP servers include `resultType: "complete"` in every successful result for protocol 2026-07-28 requests (declared via `params._meta`, the `MCP-Protocol-Version` header, or a stdio `initialize`), so current Claude Code can fetch tools from `memory serve-mcp` and `skills serve-mcp`. Older revisions keep their exact result shape.
- Reviewed memory evidence operations accept scalar citations and count equivalent evidence references once, while retaining the original citation spellings and excluding unresolved support (#1959, cases 8 and 15).
- Fleet actions and bug diagnostics launch the installed Brigade package with isolated Python imports, preventing ambient working directories, Python paths, and user-site startup code from selecting another package (#1950).
- `brigade fleet work handoff show <work-id> [--json]` reads bounded item, ownership, checkpoint, report, and recorded run references from one database snapshot. It exposes context under existing fleet read trust, with attribution and freshness limits, and performs no ownership or execution action.

- `brigade run --require-fleet-claim` and `repo_claim(require_claim=True)` refuse work without a confirmed Hub claim and enforce bounded cancellation on claim loss, including credential rejection. Default local fallback remains available.

- Codex instruction-chain doctor reads a bounded user-base `project_doc_max_bytes`, labels configuration provenance and unresolved overrides, and reports exact bytes with secondary KiB. Invalid configuration remains unevaluated, zero disables inspection, and over-ceiling caps are never clamped (#2088, slice 1).
- Brigade Stack bridge packages add `brigadestack` install names and commands backed by the existing stable Brigade 0.30.0 packages, preserving current commands and workspace formats.

- Successful manifest-backed card outcomes feed memory reflection with receipt evidence and original verification timestamps. Capture, reflection, and reviewed proposals reject support whose card bindings or retained receipts have changed, without editing canonical cards.

- Message replies can bound spool-lock acquisition while preserving the default blocking behavior.
- The ownership map keeps charter metadata within its response budget and reports omitted owners; claim loading fetches a bounded active page instead of materializing stale claim history (#1799).
- Patrol refuses corrupt persisted action status, scope, or idempotency keys before applying changes, so malformed history cannot silently discard retry fences (#1955).
- Patrol exits after its shared Hub inspection deadline even when a claim transport is still blocked, while preserving completed observations and bounding concurrent inspections (#1957).
- Patrol prioritizes this node's Hub claims within the report limit, so foreign display rows cannot hide a missing local run; omitted rows still report truncation (#1964).
- Windows care install plans create and binary-pin the selected patrol runbook before printing scheduler commands; dry runs leave the workspace unchanged (#1958).
- Deck sidebar telemetry follows the Hub's stale verdict for enrolled machines while preserving the freshest age and unknown-time display (#2054).
- Message identity can report mutual peer authorization and independent enrollment and grant epochs for up to 16 external agents (#2070).
- Patrol returns its documented error status when action history exceeds the receipt size limit, preserving the last durable receipt (#1956).
- Store sealed Hub messages with retry lookup, post-ack content purge, storage quotas, and optional required sealing (#2045).
- Pre-push history scans exclude commits verified already published on the actual destination for existing branches as well as new branches, and reject stale or unverifiable destination evidence.

- Add internal Windows handle-relative owner-only storage primitives with protected DACL creation and verification, not yet used by any command. Model-roster LKG and audit replay remain fail-closed pending lock fencing, publication ordering, and native qualification. (Refs #1739)
- Patrol detects suspected mass environment faults before repairs and reports failure for unforced apply passes, including escalation-only passes (#1975).
- Grok can opt into Hub messaging to configured aliases while T3 tools remain available independently.
- Serve public mesh key history, live directory keys, and admin-supplied signed Hub identities and manifests without a crypto backend.

### Added

- Add database-backed teammate roles, reciprocal send grants, node grant administration, reduced directory reads, and admin-only audit paging. Grant and revocation audit rows commit with their mutations (#2041).
- Add offline Fleet key initialization, public inspection and export, and local rotation with explicit scope and fixed diagnostics. These commands perform no publication or trust acceptance.

- Add offline client peer-key acceptance, rotation history, and local trust checks. Bound peer admission, recheck revoked continuity dependencies, and refuse far-future key dates. CLI/MCP encryption remains unfinished.

- Add an internal POSIX Hub identity store with signed history, endpoint bindings, and rollback floors. Preserve primary write failures in identity and trust snapshots. Identity migration and message opening/sealing remain unfinished.

- Add an internal POSIX trust snapshot for manifest anchors, signed membership and revocations, rollback floors, and attributed conflicts. Separate budgets protect authenticated conflict evidence from unsigned claims. Message opening and sealing remain unfinished.

- Add a standalone Fleet audit store with append-only rows, caller-owned transactions, structured detail validation, and bounded paging. Hub authorization and invite integration remain separate (#2039).
- Add internal POSIX key custody and signed fleet-statement validation for future encrypted clients. Client opening and sealing remain unimplemented.

- `brigade model trial run|resume --retry-transient` re-runs `adapter_error` and timeout-reason `execution_error` cells as new attempts (caps: 2 and 1), keeping each failed attempt in the cell's `prior_attempts` record. Deterministic results are never retried.
- `brigade model trial drain <output-dir>` and the first SIGINT or SIGTERM to a running trial stop it from launching new cells while in-flight cells finish and record results. Unlaunched cells are recorded as `not_run` (not a failure), `resume` launches them, and an incomplete trial exits 4.
- A structured provider quota or capacity response (`capacity-exhausted`) during a model trial is recorded as the cell's `failure_reason` on an `adapter_error` cell, not scored as a model failure and never retried as transient. The runner launches no more cells for that seat and records them as `not_run`.

- A programmatic assignment journal records stable task and attempt identities, classified result references, and result-consumption acknowledgements under the existing run owner. It does not dispatch work or retry uncertain attempts (#2049).
- Live appserver controls accept explicit task and attempt IDs, keeping assignments on the same seat independently addressable. Ambiguous seat-name controls refuse before contacting a worker. Ordinary dispatch continues using legacy seat controls until task identity propagation is connected (#2049).
- Add `brigade roster ready` to inspect an explicit worker pool, concurrency, route, and isolation. It reports blocked or unverified operations without dispatching work or reserving capacity. Live admission and T3 readiness evidence remain unverified in this initial command (#2051).

- Add the optional `mesh` crypto core with frozen `bm1` hybrid encryption and signed envelopes. Fleet messaging integration follows in later slices.

- Fleet MCP tools advertise read-only and destructive annotations, and send/reply accept optional `client_id` keys for duplicate-free replay. The opt-in Executor app recipe documents browser approval, credential boundaries and manual compatibility checks (#1887).
- Fleet message credentials can read their unread total without fetching bodies through `brigade fleet msg count`, the hub `count` action, or the optional `fleet_inbox` MCP argument `count_only`. Counts exclude expired and acknowledged mail and do not run retention writes (#1715).

- Memory care config accepts `read_only_paths` globs for policy or operator-owned cards. Scan, `plan-fixes`, and `backfill` skip matching cards, `memory proposal create` and `apply` refuse them with an error naming the pattern, and `memory care status` and `doctor` list the active patterns (#1976).
- Bound campaign member self-assignment with operator offers, per-node concurrency, expiring generation-fenced ownership, and idempotent takes. Member detail responses now link back to their campaign.

- Native source builds add `brigade-code-graph`, `brigade-code-graph-mcp`, and `brigade-evidence` entry points over the existing engines. Legacy commands, archive locations, protocol identities, and release assets remain compatible.
- Native release bundles include canonical engine executables alongside legacy assets. Default setup retains legacy identities, with complete checksums and source-bound attestation checks.

- Add client APIs to observe signed Hub identity, import supported membership/revocation bundles, and publish an existing durable local public key with exact readback recovery. Bundles containing key manifests are refused before import. Active principals can read their own public key records.

- Add explicit offline admission of signed key-manifest evidence with rollback floors and retained conflicts. Manifest entries do not accept peers, and transport bundles containing manifests remain unsupported.

### Fixed

- Content guard recognizes a whole hostname expression after a completed quoted-subscript f-string field, while retaining literal URL hostname detection (partial #1983).
- Preserve backslashes in TOML literal strings on Python 3.10, so Windows paths and trailing separators load correctly in local configuration, including arrays and inline tables. Generated TOML strings now round-trip through both supported parsers.
- Corrupt local verification receipts no longer crash history, planning, or captured verification runs. Both local receipt readers handle encoding and decoder failures and reject JSON deeper than 128 containers; exports still report malformed inputs as failures and preserve the original files.

- Concurrent local Repository Scout admissions refresh directory enumeration under the queue lock, so a waiting producer sees the preceding job and preserves the one-active-scout limit.

- Fleet Hub refuses plaintext replies to retained sealed messages, including retries of pre-upgrade plaintext replies. Exact detached retries remain available after parent expiry, and invalid retries do not reveal whether a foreign message id is retained.

- Content guard detects and fully redacts Windows home paths with forward or mixed separators, while preserving quoted spaces and surrounding prose (#1913).

- Managed Cursor command hooks with an explicit command type are recognized without duplicate callbacks.
- Incomplete runs without infrastructure-only attribution score -1 during outcome capture, matching the scoring policy.
- Skill validation accepts folded and literal YAML block scalars, including chomping and explicit indentation indicators, while continuing to reject unsupported nesting.
- Fleet Hub moves contextual destinations into section tabs, bounds Overview activity previews with full evidence in disclosures, and leads Repositories with observed work and claim cards.

- Fleet admin pages group navigation into Overview, Work, Capacity, Repositories and Settings. Overview leads with actionable subjects, Alerts keeps evidence ages and repair preparation together, and secondary diagnostics use disclosures. Capacity distinguishes configured slots from unverified usability without collecting new metrics.

- Fleet Machines and Repos boards use the shared Deck sidebar, navigation, and Resources while preserving board filters and pivot query state in dark and light themes. Wide tables scroll within their wrappers, and board refresh timing stays synchronized with its label.
- Live run controls no longer advertise an unusable Unix socket while starting a loopback TCP transport for long paths or platforms without Unix sockets. Loopback control metadata is published after the endpoint binds (#1767, slice 4).

- Duet binds its authenticated Hub credential and endpoint until restart, keeps maintenance out of the shared node queue, and holds queued sends and replies while messaging is disabled. Doctor and watch verify the active principal. Watch reports polling outages without webhooks. Skill reads enforce both registry IDs and directory names.

- Duet keeps its Hub inbox mirror, reconciliation and pending-send flush current while event webhooks remain disabled. Startup verifies the message credential's authenticated principal before registering MCP tools. Replies obey the messaging switch and share the 20-message hourly budget. New messages require an exact configured recipient alias. `brigade fleet msg identity` provides the read-only credential identity probe.
- Control crosswalk version 3 narrows SOC 2 rationales to recorded artifact properties, limits EU AI Act approval and archive evidence, uses Commission article sources, and adds explicit nonmatches for Articles 19, 50 and 111(4). CSA AICM 1.1.1 mappings cover eight controls in the OSP layer with partial evidence and explicit gaps for monitoring, service BOMs and model signing (#2033, #2034, #2035).
- Fleet MCP directory and claim reads return bounded pages with continuation cursors, so callers can walk a roster or claim snapshot that exceeds one response. Legacy `who` requests without `limit` return at most 200 nodes and 200 principals with `truncated: true` when more remain. New clients retry older hubs once without paging arguments and report `paging_continuation_unsupported` for truncated legacy results. Expired cursors return `paging_cursor_expired` so callers can restart. Global snapshot saturation reclaims walks idle for more than 60 seconds across callers, retaining HTTP 429 when all walks are active.
- Fleet Hub keeps one idle database connection until shutdown to remove the per-request last-connection checkpoint, WAL deletion and recreation, and about 3 of 4 syncs per HTTP write request. Independent review measured 0.76 s per request (3 WAL syncs) before this change and 0.26 s (1 WAL sync) after it with a 250 ms WAL-sync delay. Slow disk flushes on the Windows runner remain the likely shared cause of the timeout family: this PR's own CI took 92.65 s for 1,024 claims and 191.41 s for 1,603 stats stores on persistent connections, versus 0.84 s and 1.30 s on Linux, about 90-120 ms per commit under `synchronous=FULL`. This is a partial mitigation and does not fix those slow flushes.
- Fleet Hub rejection responses drain at most 1 MiB of late upload data within 250 ms to prevent oversized and late-upload resets, including the policy-route WinError 10053. Plain TCP half-closes before draining. TLS skips that early raw half-close and drains before normal server closure, whose stdlib shutdown behavior is unchanged. Zero-length and fully read bodies skip draining, and error responses carry one `Connection: close` header. The HTTP/1.0 roster timeout and credential-test timeouts remain unexplained. The buffered 516-byte roster form does not support a keep-alive explanation, and `leaked_descendants=1` appears on nearly every Windows test file and is not a diagnostic signal. (#2014)
- Invalid Cursor hook event names no longer let a project configuration suppress the user presence callback.
- Preserve campaign execution bindings after the 50-link retention window, including against trusted observations and cross-item relinks. Hub schema 42 adds an indexed history lookup without rewriting stored events.
- Index campaign take event-key lookups without scanning Worklore history. Hub startup upgrades existing databases to schema 41 and preserves stored events.
- Fence campaign execution and attempt writes to the current holder grant, refuse member specification drift, and reserve take capacity for progress and release. Campaign operators can recover a stranded reservation with explicit quiescence acknowledgement and an audited evidence reference.

- Daily status preserves local work counts when an optional health snapshot times out. Windows pytest artifacts include flushed test-phase timings and stack dumps, and timeout summaries identify the last test or session phase.
- Cursor project initialization and user-scope harness sync install all three presence lifecycle hooks into versioned hook documents, preserve foreign entries, validate the whole project document before it suppresses the user hook, and migrate hook ownership by event.
- Brigade self-invocations isolate Python imports from workspace files and `PYTHONPATH`, using the running installation with `-I` so `pip --user` installs keep working. This covers hooks, update refreshes, detached runs, dispatch, dashboard access, embedded scanners, and generated service commands. Agent hook commands skip update notices and refreshes entirely.
- Work briefs read only the newest release candidate and release run instead of decoding every bundle, and the harness-wiring scan skips its per-ancestor worktree check for files that are not wiring documents. On a checkout with a large release history the brief ran about 3x faster with 60% lower peak memory.
- Work briefs limit Codex Cloud inventory to the newest page and check registered tasks missing from that page individually, preserving stale-task warnings without full inventory pagination.
- Handoff drafts use consistent LF line endings on Windows for both creation and forced replacement.
- The Command Deck separates counted queue exclusions from blocked jobs. Blocker and attempt-limit counts retain alerts, while routine backlog stays in queue details. Malformed exclusions remain actionable, and absent telemetry stays unknown. Hub snapshots report optional integration readiness coverage explicitly.
- Deck verdicts use plain colored text. Provider icons use transparent, consistently sized marks, including the official Jules mascot and a neutral fallback for unknown agents.
- Deck Activity separates managed jobs from reported agent sessions. Ready queue counts avoid duplicate totals and fold routine exclusions. Shared repository names across machines show neutral parallel activity, while reported shared modified paths retain warnings.
- Retention diagnostics use the held run lock when a detached worker checkout and custom output directory hide the original workspace after a malformed or unreadable receipt. Stale recovery retains the saved failure detail.
- The content guard recognizes host-related assignment keys behind shell declaration prefixes (`export`, `readonly`, and Windows batch `set`), so `export HOSTNAME=<host>` is detected like its bare assignment; arbitrary keys, code expressions, and call arguments stay skipped.
- The content guard no longer flags a private hostname in a formatted URL when an f-string replacement field holds only that expression, such as `f"https://{cfg.db.host}/path"`; every other URL, prose, string, and configuration hostname is still detected.

- Fleet claim renewal and release follow node-token rotation on the bound hub, including orphan-release retries, while retaining the last node credential when its token file is temporarily unavailable.
- The content guard detects bare AWS temporary access keys, Google API keys, and GitLab personal access tokens while preserving assignment and Bearer detector precedence.
- Content guard matches and redacts complete quoted root-home paths with spaces in directory or file names, preserving the quotes and surrounding prose.
- The content guard detects and redacts private hostnames in equals-form host options, including single- and double-hyphen options and compound host-related keys, while preserving code-expression and arbitrary-assignment skips.
- The content guard recognizes host-related keys after YAML sequence markers, including indented items and compound keys, while preserving its code-expression and public-suffix skips.
- Content guard detects host-key assignments after batch `@set` and `set /a`, shell declaration flags, `declare` and `local`, and earlier assignments on the same line.
- The content guard keeps JavaScript URL template fields such as `https://${cfg.db.host}/path` code-shaped while still detecting literal hosts in quoted expressions, escaped fields, comments, and ordinary strings.


- Reviewed duplicate memory merges validate block links against their final card destinations and configured indexes, preserving unrelated anchors and refusing missing claim blocks (#1959).

### Added

- A `duet` connector pack serves Claude Duet from the Dot connector code with its own runtime schema, Hub principal `agent:duet`, state, Access audience and handoff inbox. It exposes 22 memory, skills, inbox and read-only fleet tools; dispatch and T3 fleet tools are absent from discovery. `message_send` has its own `messaging_enabled` gate (optional for Dot) and a 20 per hour limit for Duet, and the call log records each client's name. See `docs/duet-connector.md`.
- Fleet MCP can ask the configured operator a question, with optional desktop entry, inbox replies, and hub-recorded answers or timeouts bound to the caller credential. Authenticated operator replies return the exact answer with its identity binding. Caller-reported desktop answers retain their unverified source.
- Live Fleet Hub inbox delivery (#1887 slice 3): new unread mail reaches running Claude Code sessions through the managed `UserPromptSubmit` and `PostToolUse` hooks, and Codex sessions through `brigade work presence-hook --harness codex --stdin --context fleet-inbox`. Messages are fenced as untrusted peer content, shown once per session from a private cursor file, never acknowledged by delivery, and fetched with one 2 second hub request behind a 60 second failure backoff. See `docs/fleet-inbox-delivery.md`.
- The fleet MCP station serves MCP Events (`events/list`, `events/subscribe`, `events/unsubscribe`) behind `brigade fleet mcp --events`, with subscriptions stored on the Fleet Hub (schema 39) and signed Standard Webhooks delivery for `message.created`, `claim.expired` and `run.failed`. The hub keeps events off until `BRIGADE_FLEET_EVENTS_CALLBACK_HOSTS` names exact callback hosts. The shared MCP transport now accepts protocol version `2026-07-28` (`server/discover`, `Mcp-Method` and `Mcp-Name` header checks) and the dot pack reuses the shared signing and callback code. See `docs/fleet-mcp-events.md`. (#1887)
- Add an operator CI board with exact-head required checks, PR rollups, node-only relay pulls and budgeted on-demand reconciliation. Hub refresh queues work without outbound calls, and private repository names stay operator-only.

- Claude work-loop hooks track explicit file reads and record unseen callers before Edit, Write, or NotebookEdit. Checks warn without denying edits, and unavailable or incomplete graphs record a null result with a reason.

- Antigravity sessions publish Fleet Hub presence through silent, detached hooks installed by `brigade init --harnesses antigravity`; Stop refreshes presence until the session expires by TTL.
- The `t3-dispatch` connector can read, acknowledge, reply to, send, and check Fleet Hub messages through an optional owner-only principal file. Its one-minute watcher wakes configured targets with message counts and sender ids, retries failed wakes, and keeps message bodies and subjects out of notifications.

- `brigade memory serve-mcp --http` and `brigade skills serve-mcp --http` serve the read-only MCP tools over a stateless loopback Streamable HTTP endpoint (`POST /mcp`, `GET /healthz`) for MCP hosts that cannot spawn a stdio process. The server binds only `127.0.0.1` or `::1`, requires a bearer token from `BRIGADE_MCP_HTTP_TOKEN` (32 characters or more, never a flag), rejects non-loopback `Host` and `Origin` headers, and caps request bodies at 1 MiB. `--stdio` is unchanged.
- MCP registry metadata for the two read-only servers: `mcp-registry/memory/server.json` (`io.github.escoffier-labs/brigade-memory`) and `mcp-registry/skills/server.json` (`io.github.escoffier-labs/brigade-skills`), with matching `mcp-name` markers in the README. `scripts/version_sync.py` keeps both manifests on the release version.
- Scoped per-alert repair actions with explicit browser operator activation, registered destinations, fenced broker jobs, and separate delivery and verification receipts.
- Optional metadata collectors for native T3 Code conversations and recent OpenClaw sessions. Publication uses the enrolled node's existing credential and requires an explicit flag.
- Brigade agent plugin for Claude Code and Codex (`plugins/brigade/`): memory MCP tools, the `brigade-work` skill, a non-blocking SessionStart brief (Claude Code only), and an opt-in `enforce` skill.
- Memory MCP tools `memory_status`, `work_brief`, and `write_handoff`; card tools now span the repo store and a user store under `BRIGADE_HOME`. Cards are served only when their real path stays inside the store, and `write_handoff` caps content at 256 KiB and facts and evidence at 50 items each.
- `brigade memory serve-mcp --http` and `brigade skills serve-mcp --http` serve the MCP tools over a stateless loopback Streamable HTTP endpoint (`POST /mcp`, `GET /healthz`) for MCP hosts that cannot spawn a stdio process. The skills tools are read-only; the memory server also has `write_handoff`. The server binds only `127.0.0.1` or `::1`, requires a bearer token from `BRIGADE_MCP_HTTP_TOKEN` (32 characters or more, never a flag), rejects non-loopback `Host` and `Origin` headers, and caps request bodies at 1 MiB. `--stdio` is unchanged.
- MCP registry metadata for the memory and skills servers: `mcp-registry/memory/server.json` (`io.github.escoffier-labs/brigade-memory`) and `mcp-registry/skills/server.json` (`io.github.escoffier-labs/brigade-skills`), with matching `mcp-name` markers in the README. `scripts/version_sync.py` keeps both manifests on the release version.
- A `brigade-cli` console script alias, so `uvx brigade-cli memory serve-mcp --stdio` works without a global install.
- A root `Dockerfile` that runs the memory MCP server over stdio as a non-root user, plus `glama.json`; the image installs the latest `brigade-cli` release from PyPI (`--build-arg BRIGADE_VERSION=X.Y.Z` pins one), so MCP directories that build and introspect a repo image can list the servers.

### Changed

- `brigade model trial run|resume` behavior changes for callers that set no new option. Check wrappers and CI that key on trial exit codes:
  - A provider capacity response with a nonzero seat exit used to score as `execution_error` (exit 1). It is now `adapter_error` with `failure_reason: capacity-exhausted` (exit 3), and the per-seat stop is on by default, so the seat's remaining cells become `not_run`.
  - `run` and `resume` now install SIGINT and SIGTERM handlers for the whole trial. The first signal drains instead of stopping the process. A second signal hard-stops it.
  - Exit code 4 is new: the trial ended with `not_run` or `running` cells and no measurement failures.

- The memory and skills MCP tools describe what they return and when to use them, and their input fields carry descriptions, so MCP clients and directories can pick the right tool.
- The read-only MCP servers negotiate the protocol version (`2024-11-05`, `2025-03-26`, or `2025-06-18`) instead of always answering `2024-11-05`, answer `ping` with an empty result, return `-32600` for malformed requests, and report the Brigade version in `serverInfo`.
- The npm package `@escoffier-labs/brigade@0.30.0` bundles the published `brigade-cli==0.30.0` wheel. Its launcher runs `brigade.cli:process_main`, the same entry point as the `brigade` console script, so npm installs keep exit status 130 when a verification is cancelled.
- The memory MCP server reports `serverInfo.name` `brigade-memory` (was `brigade-memory-readonly`), and `serve-mcp --target` defaults to the nearest directory above the working directory holding `.brigade/config.json` or `.brigade/memory-care.toml`, never the home directory. An explicit `--target` is still used as the repo store, and one that is not an existing directory exits 2.
- The memory `work_brief` tool builds its git, session, and next-task keys from local helpers only, so it never contacts the fleet hub. An unexpected exception in any memory tool is returned as a tool error instead of ending the server, and `write_handoff` writes to the repo inbox even when an invalid memory-care config drops the repo store from reads.
- `brigade handoff draft` creates draft files with mode 0600 (previously the umask default).
- The beta channel moves to the 0.31 preview line. `brigade update --channel beta` resolves `0.31.0.devYYYYMMDD` wheels again, instead of older 0.29.0 development builds from before the 0.30.0 release.

### Fixed

- T3 inbox acknowledgement batches stop on credential rotation or an invalid credential, including during failed requests and recount. Each request checks the credential loaded for transport against the original batch scope.
- Work readiness, claim, and explicit task runs fail closed on missing blockers or ancestor parents. JSON separates unfinished and missing dependencies, and explained output names edge-removal repairs. (Fixes #1857)
- Readiness repair commands include the repository target and preserve literal edge endpoints when a live task shares their prefix. Campaign views omit member repair commands that use synthetic IDs. (Refs #1857)

- Scanner children run with `BRIGADE_NO_UPDATE_CHECK=1`. The detached update refresh a child started outlived its throwaway sandbox and recreated it when writing `update-notify.json`, leaving one `brigade-scanner-child-*` directory in the temp dir per scanner run.
- Claude hooks retain the latest skill attribution across stale concurrent state saves, preserve deliberate capture changes, and carry the retained artifact into verification guidance and receipts (#1924).
- Reviewed memory proposals preserve Markdown structures when anchoring claims, including list-contained quotations, code and headings. Exact duplicate comparison retains literal anchor tokens in excluded blocks.
- Reviewed memory consolidation derives each card's final cutoff from accepted evidence and preserves its existing cutoff across operation order.
- Run readers honor verified durable outcomes, including reopened histories, and inventories skip unreadable historical records while retaining diagnostics and hard runs-root access errors.

- Add project-only Codex instruction diagnosis under `brigade bug diagnose --component instructions`, with body-free modeled selection, explicit finding export and local report preparation.

- Add an inactive transactional model for external Worklore leases, with owner fencing and conflict checks. No external registration route is enabled.

- Skill proposals preserve behavioral contracts and reject changed packages or baselines. Fleet skill review binds explicit copies and mutation commands to exact reviewed package identities.
- Paired skill trials can plan, replay and validate five receipt-backed scenarios in disposable targets. Replay results remain offline and cannot promote a skill. Live execution requires a tool adapter that is not implemented yet.
- Concurrent skill updates share a mutation lock so reviewed approvals cannot overwrite a newer baseline. Paired replay refuses redirected output paths, binds the replay transcript and reports offline apparatus outcomes explicitly.
- Dot session trust labels preserve the existing session and report data budget near the response limit.

- Unscoped Worklore histories omit external ownership events before pagination, while preserving legacy event fields and cursors.

- Keep external Worklore scopes reserved across later legacy events, and refuse expiry cleanup of unsupported ownership histories.

- Explain that older Fleet Hubs need an upgrade when they reject unread-count requests as an unsupported action.
- Bound the optional local full verification gate to one run per user per host with at most four shard workers. The admission slots live in an owner-only directory under `/tmp`, shared across linked worktrees and separate clones, and `verify-fast` now delegates to the same gate. Required CI is unchanged and is not throttled.
- Add node-authenticated Fleet reservation acceptance (`reservation-accept`) with current target, policy, expiry, bound work eligibility and model-retirement checks. A work item that was blocked, held or deleted after routing is refused without accepting the reservation. Exact retries preserve the original reservation and expiry.
- Fleet reservation acceptance now replays an already accepted reservation without re-checking machine capacity, so a retry no longer fails once the accepting launch reports as running.
- Add yolo work mode (`brigade init --yolo`, `brigade work mode set yolo|enforced`). It keeps the work brief, memory recall, Fleet Hub messages and handoffs on request, and drops the raw-verification deny, the failed-verification nag and the Stop receipt gate in the Claude and Grok hooks. Adoption reports a yolo repo as `yolo`, never `bypassed`. Add a separate safety-rails switch (`brigade work rails on|off`), on by default in both modes. With rails off, the `hooks/pre-push` template skips the content guard and history scans. Guard policies and baselines are left unchanged. The rails-off choice lives in a per-user record outside every repository, keyed by the repository's real path, so no committed file can turn the rails off. An in-repo `work.rails` value is ignored and reported. `brigade status`, `brigade doctor` and adoption show both settings. Existing `hooks/pre-push` copies keep scanning until refreshed from the template.
- Yolo mode now follows the directory a verification command runs in. A session that starts in an enforced workspace and runs `cd <yolo-repo> && pytest -q` is no longer denied, while a session in a yolo repo that runs a check in an enforced repo still is. A check whose directory cannot be resolved (a variable, `cd -`, a subshell or `||` around the `cd`, or an unwired directory) keeps enforcement. Stop-gate behavior for writes into enforced repos is unchanged.
- Retire the `chef` role key in favor of `orchestrator`, with one release of read aliases and a hidden deprecated `--chef` flag. Hub schema 48 preserves preference rows while renaming the column. Deploy the Hub first. Rollback requires restoring a consistent pre-upgrade database backup. Frozen v1 preference responses retain `chef` for older clients.
- The Rust hub front starts its Python upstream with a per-start key, uses the front's client address throughout the upstream, disables upstream automatic WAL checkpoints, derives message-list deadlines from the body, and probes readiness outside the proxy pool.
- Upstream mode now runs interim Python checkpoint maintenance until S4b takes ownership, preventing unbounded WAL growth while automatic checkpoints are disabled.
- Invalid upstream header keys are rejected at startup without exposing their value.
- Proxied message-list waits allow 10 seconds of header slack for Python write-lock waits before and after polling. The front retains a parsed upstream header key and rejects an empty managed key.
- Setup installs canonical engine names with verified legacy compatibility links or copies. Release manifests pin canonical assets while releases continue publishing both asset sets through v0.32.
- Before downgrading an upgraded home to v0.30, run v0.31 `brigade setup --rollback`. For a fresh v0.31 home, move `installed.json` aside before v0.30 setup. The published v0.30 setup otherwise exits with `invalid installed state`. Legacy data remains readable. Fresh canonical data is invisible to v0.30 and is never merged automatically.
- The Rust hub front validates credentials before admitting bodies, isolates credential, browser and anonymous proxy lanes, caps body reservations per credential, forwards large unvalidated bodies by headers only, and buffers bounded JSON responses before sending headers.
- Trusted forwarded HTTPS now reaches enrollment origin and Secure cookie checks while enrollment retains anonymous admission. Native database and token-file reads expand home-relative paths consistently with Python.
- Legacy engine entry points, environment overrides, the legacy timeout config key and hidden CLI aliases now print stderr deprecation notices naming their replacements and removal in v0.33.0. Canonical calls remain silent, and version banners and MCP identities still follow the invoked executable name.
- Remove the retired `check`, `refire`, and `taste` skills from Brigade's shadcn registry. `npx shadcn@latest add @brigade/check` (and `@brigade/refire`, `@brigade/taste`) no longer resolves. Use the Superpowers replacements instead: `superpowers:verification-before-completion`, `superpowers:systematic-debugging`, and `superpowers:test-driven-development`. The `reduce` skill now points at those skills. Skills already installed in a project keep working.
- Add bounded Rust hub reader and CPU pools, Python schema compatibility checks, and independent WAL maintenance with disk-pressure admission for event and session writes. Route answers still come from Python.
- When the Rust hub manages the Python upstream, Rust owns checkpoints and the Python checkpoint thread stays off. Credential admission uses the shared reader pool, and the default database path comes from Python even when HOME is unset. An external upstream keeps its own checkpoint owner.
- Accept the exact optional schemas created by MCP reports, model inventory, skills review, project boards, and policy migration without changing Python startup or fresh storage snapshots. Refuse changed optional structures and unknown tables.
- Accept complete Python-migrated schema variants, including legacy nullable message columns, while refusing unrecorded hybrid layouts. Lockstep now covers historical schemas, release captures and legacy regression builders.
- Add an unwired Rust hub writer with FULL group commits, command savepoints, bounded admission, hub and memory write turns, and storage fault tests.
- Connect writer startup to the shared audit and structural schema check, use shared change detection, and provide bounded maintenance sweep admission while leaving checkpoints with the maintenance owner.
- Rename the evidence engine Go module to its canonical Brigade module path.
- Add a development-only hub load harness with open and closed loop modes, per-attempt JSON reports, and a disposable Python and Rust proxy matrix with verified 1 vCPU / 512 MiB server scopes.
- Use canonical engine names throughout current documentation, shipped skills and templates. Move the MCP wiring guide to its canonical path with a compatibility redirect.
- Require reviewed path and literal exceptions for retired engine names, and fail at version 0.33.0 while removable shim allowances remain.
- Update active skillet skill citations to fix-findings and repo-audit while preserving recorded evidence and historical IDs.
- Add v2 hub route modes, read-only shadow comparison, admin-authenticated metrics text and structured request logs.
- Expose pending writer and queue measurements as explicitly unavailable until their owning slices wire them.
- Add the Python hub findings ledger with atomic report reservations, stable report keys, canonical HMAC fingerprints, and explicit principal grants.
- Add charter findings selectors, open findings in owners holdings, and an 18-case findings contract profile. Upgrade the required hub schema from 48 to 49.
- Add opt-in native hub registration, credential validation, node enrollment and revocation, and message principal enrollment, grants and generation checks through the durable group writer.
- Fence finding resolution against live reservations and expired former holders. Normalize charter aliases, separate fingerprint and report HMAC keys, publish the key file atomically, and preserve URL port zero.
- The memory hits pushed on `inbox_list` and `fleet_digest` are now scanned with the local guard default rules and the invisible and bidi character check, and a failing hit is dropped instead of returned.
- The Rust hub's `/metrics` now reports real values for the writer queue depth, batch size, apply, commit and lock-wait times, aborted batches, and the reader and CPU pool queue depths. The proxy front starts no writer, so its writer metrics stay `NaN` with a help line that says so.
- The S6a load harness fills its `queue_depth`, `batch_sizes` and `checkpoint_stalls` telemetry columns from the `/metrics` samples it collects, and decodes metrics text. Report `schema_version` stays 1.
- Pin the native guard's PCRE2 crates exactly (`pcre2 =0.2.11`, `pcre2-sys =0.2.10`) and force the bundled PCRE2 10.46 build from the repository `.cargo/config.toml`, so the guard regex engine no longer depends on the host's system libpcre2. A unit test fails if the linked PCRE2 or Unicode database version drifts from the pin.
- Run the native guard benchmark in CI as a bounded smoke job (`scripts/benchmark-guard-native.py --smoke`) and upload its numbers as the `guard-native-benchmark-smoke` artifact. No timing threshold gates the job.
- Added `brigade fleet backup`, which writes integrity-checked online copies of the hub's SQLite files into a 0700 directory, keeps 48 hourly and 14 daily copies, and refuses a copy that would leave the volume under its free-space floor.
- Added `brigade fleet restore`, which restores a copy and advances the message sequence, roster and policy revisions and a claim recovery floor past anything an earlier restore from the same copy issued, using a recovery ledger kept outside every database.
- Claim generations are now allocated above the recovery floor, and the hub schema is version 50 so a hub that predates the floor refuses a restored file.
- `BRIGADE_FLEET_FINDINGS_KEY_FILE` supplies the findings HMAC key as a read-only secret file.
- `brigade fleet serve` now exits cleanly with status 0 on SIGTERM, as `docker stop` and a host shutdown send, and logs "brigade fleet hub stopped cleanly".
- `brigade fleet restore` publishes the restored file with a call that never replaces an existing name (`os.link` on POSIX, `os.rename` on Windows), checks the target's `-wal`, `-shm` and `-journal` files again just before publishing, holds a lock on the staging name so two restores to one target cannot delete each other's files, and creates a missing ledger file only under `--assert-no-prior-restore`.
- Backup manifests and recovery ledger records name files relative to the backup directory instead of by absolute host path. Older records with absolute paths still parse.
- Add opt-in native hub claims (`--route-mode claims=native`): acquire, renew, release and inspect with generation fencing, retired holders, lease expiry and claim.expired events, plus the claims listing, through the shared durable writer.
- The Rust hub has a memory v2 store in its own SQLite file (`fleet-hub-memory.db`): an
  append-only event log, cards keyed by scope and id with per-card revisions, compare-and-set
  on revision guarded by `log_epoch`, idempotent writes by `client_event_id`, per-scope grants,
  and trust that only an import with a clean scan can raise. Every memory commit runs under
  the hub write lock after re-reading the caller's credential, and the power-cut tests cover
  the memory file and that protocol. No route serves it yet.
- The Fleet Hub Compose recipe publishes no port: a holder container owns the network namespace, the hub and the HTTPS front join it, and the GPU embedder runs only under the `gpu` profile on an internal network.
- `ops/fleet-hub/bin/hub-setup.py` defaults to BM25 mode and probes a local GPU with an R1 model, then a remote endpoint, only with explicit hybrid opt-in, writes the config and both secrets, and switches modes either way.
- The recipe ships a front wrapper and a host watchdog that restart a container left with loopback only, a boot unit, hourly backup and watchdog timers, reserved slice templates, and `checks/s31_checks.py` for the S31 host acceptance items.
- New runbook `docs/runbooks/fleet-hub-dogfood.md` and `ops/fleet-hub/HOST-ACTIONS.md`. The Proxmox runbook records the LXC hub's 256 MB swap and 8 GB root disk.
- `brigade.slice` now carries the I/O latency target on the database device, since the kernel applies `io.latency` only between peers and agent work runs in its peers `user.slice` and `system.slice`. `s31_checks.py restart --case hub-kill` now SIGKILLs the hub's serve process inside the container instead of running `docker kill`, so the restart policy is what brings the hub back, and it restores service when recovery fails.
- `s31_checks.py restore --drive` reads the claim recovery floor from the restored database and passes it to the contract drive as the new `hub-contract --claim-floor`, so the claim-fencing cases expect a restored hub's first generation at floor + 1 instead of 1. Without the option the drive still expects generation 1 on a fresh hub.
- `brigade-hub --route-mode memory=native` serves `POST /memory` from the new memory store.
  A hub without a memory route mode still answers `404 {"error": "not found"}`, and a memory
  store that cannot open answers `503` with reason `memory-unavailable` while the rest of the
  hub keeps serving. `/metrics` reports memory batches, hub-lock residence and the residence
  bucket.
- The verify-brigade hub contract drive has a `--profile memory` mode with the S13a memory
  cases, and `--memory-absent` for a hub without memory.
- Every memory refusal now carries its memory error code. A chunked body gets `411`, a declared
  length over 8 MiB gets `413 memory_card_too_large`, and an incomplete or timed-out body gets
  `memory_request_invalid`. The `405` for a method other than `POST` sends `Allow: POST`.
- Under disk pressure, the memory `card`, `log` and `status` reads keep answering. Memory writes
  still get `503 disk-pressure`.
- `--route-mode memory=native` with `--upstream` now refuses to start. In external upstream mode
  nothing checkpoints or sweeps the memory file.
- A memory retention sweep keeps running bounded 256-row batches until the expired
  idempotency rows are gone. Before, it stopped after one batch a minute.
- The memory drive profile adds a per-run suffix to its card ids and repository scopes, so it
  passes when run twice against the same `--hub-url`.
- The memory drive profile starts its log checks at the `seq` from the run's first `status`
  call and follows `next_seq` for the principal check, so it passes against a reused
  `--hub-url` with more than 5,000 earlier events.
- A memory retention sweep whose `COMMIT` fails now rolls the transaction back. Before, the
  open transaction made every later memory write fail with `storage-error`.
- Fix a race in the run-lock stale claim that could rename a live lock out of sight, which surfaced as a FileNotFoundError on the task-ledger lock and could let two writers hold it. The claim now takes only the lock it judged stale.
- Serialize stale-lock recovery behind an OS file lock beside each lock (`.<name>.recovery-gate`), so two recoverers of a dead lock can no longer move the winner's freshly published lock, strand it under a claim name, or give a claim-mode reaper a second holder. A Windows sharing violation on the claim rename now backs off instead of raising, and a failed restore no longer escapes as an OSError.
- Add a checkpoint-attempt counter to the hub upstream checkpoint thread so tests wait for a checkpoint instead of sleeping.
- The hub's pinned-reader checkpoint test now checks that a checkpoint never stalls the writer, instead of passing or failing on timing.
- A busy stale-recovery gate no longer makes a dead run-lock owner look active: `brigade runs recover` and run acquisition wait up to two seconds for the other recoverer, then report "stale lock recovery in progress". A recovery gate that cannot be opened or locked now fails with an error that names the gate file and how to clear it.
- When stale-lock recovery gives up on a busy gate, the "stale lock recovery in progress" message now names the recovery gate file and says the gate may be held by a stopped process or sit on a slow filesystem, and on Windows that it may never lock (for example on a share without byte-range locks), with how to clear each. `brigade runs recover` no longer reports a dead run's owner as still active when another recoverer took over the lock for a different run during the wait.
- Fixed a port race in the verify-brigade hub launchers: a hub that exits because its port was taken is now relaunched on a fresh port, up to five attempts within the startup timeout.
- The hook hang test no longer latches the hook off when its baseline run is slow on a loaded runner.
- The dogfood hub Compose recipe runs the Rust hub, `brigade-hub`, in managed mode in front of the Python hub, with `--route-mode memory=native` and the memory file in the hourly backups.
- `ops/fleet-hub/checks/s31_checks.py memory` checks that `POST /memory` with the admin token answers the `status` action with 200.
- `brigade-hub` now sends its managed Python upstream SIGTERM after the drain and kills it only if it is still running 10 s later. It exits 0 only when the upstream exited 0, so the dogfood recipe's `stopped cleanly` line means both hubs shut down cleanly.
- A stop signal during `brigade-hub` startup (the descriptor-limit re-exec, the upstream wait, the bind and the store opens) no longer kills it by the default action: it ends the upstream the same way and exits 2.
- The dogfood backup job copies `fleet-hub.db` and `fleet-hub-memory.db` in separate `brigade fleet backup` calls, the memory copies under `memory/`. A missing memory file is logged and skipped instead of failing the hub file's backup.
- The fleet hub Containerfile's stage defaults match the Compose pin, so `--target dogfood` builds without build args. The Quadlet build and the container smoke pass their own pin in full.
- `brigade-hub` starts its managed Python hub in its own process group with an empty signal mask, so a Ctrl-C or a signal to the whole group stops the Python hub once, through `brigade-hub`. The Python hub also ignores a second stop signal while its shutdown runs, and it gets SIGTERM if `brigade-hub` dies first.
- A startup error after the Python hub is ready, such as a port that is already taken, now stops it with SIGTERM instead of SIGKILL.
- The container entrypoint starts `brigade-hub` with SIGTERM and SIGINT blocked, so a stop that lands during the spawn waits for its handlers.
- `hub-setup.py init` takes `--brigade-version` with `--brigade-ref` and writes `BRIGADE_VERSION`, so the image's version label follows the ref.
- The S31 `backups` check fails when the memory file exists on the state volume but no memory copy was ever made, and the backup job exits 128+N when a call dies by signal N.
- The dogfood image's default `BRIGADE_REF` moves to `d3b38d4b6`, the commit with these fixes, so the default build runs the fixed `brigade-hub`.
- `POST /memory` with `action: "search"` now answers on a hub with `--route-mode memory=native`,
  where it used to return `501`. It scores with BM25 (k1=1.2, b=0.75) over the live cards in
  the scopes the caller can read, using the local scorer's tokenizer and card fields, and
  breaks ties by `cards/<card_id>.md`. A `min_seq` waits up to 500 ms for the index and
  otherwise answers with `retrieval.stale: true`. `status` reports `indexed_seq`.
- The hub builds its lexical index from the memory file at start, updates it after every
  commit and rebuilds it in the background. If the index falls behind a commit, searches
  answer from the last good snapshot marked stale until the rebuild finishes. `/metrics`
  reports the indexed and committed `seq`, the lag, the stale flag and rebuild counts.
- `engines/hub/tests/memory.py` checks lexical parity (gate R0) with the local `bm25` scorer
  on `evals/memory-retrieval`, and the `--profile memory` drive has `memory-search` cases.
- A card whose front matter nests brackets more than 200 deep no longer crashes the hub while it
  is indexed or at restart. The hub keeps the raw text, as Python does past its own limit.
- Front-matter lists now match Python for `0x`, `0o` and `0b` integers, lone surrogate escapes
  (shown as U+FFFD), trailing `#` comments and integers over 4,300 digits. A `\N{name}` escape
  still keeps the raw text, a difference the hub README documents.
- A search with no word characters reads at most 1,000 cards or 4 MiB of card text and reports
  `retrieval.truncated: true` when it stops early. A query over 4,096 bytes as sent gets its own
  refusal message, apart from the 256-character limit after normalization.
- The index also rebuilds past 64 delta segments or once a quarter of its documents are dead,
  streams cards from the store while it builds, and retries a failed rebuild with a backoff
  instead of waiting for the next write.
- A literal search reads the visible cards in answer order and stops at `limit` matches, so a
  truncated answer holds the same cards as a full scan's first `limit` and does not change
  after a rebuild. `match_count` is a lower bound when `retrieval.truncated` is true.
- Small commits merge their delta segments in tiers of 8 instead of rebuilding the whole index
  every 65 commits. A full rebuild starts only past 10% delta documents or a quarter dead.
- The rebuild retry loop ends when the memory writer closes its channel after a failed reopen,
  a new index failure cuts a rebuild's backoff short, and closing the store joins the rebuild
  thread and interrupts a load in progress.
- A literal hub memory search reports every match in `match_count`, as the local scorer does, when the visible cards fit the scan budget, and sets `retrieval.truncated` only when the budget stops the scan.
- Fleet Hub (Rust): `--route-mode messages=native` serves `POST /messages` `send`, `reply`, `ack`, `list` with native long polls, `sent`, `sent_lookup`, `count`, `identity`, `keys_get`, `manifests_get` and the `question_*` actions, byte for byte with the Python hub. Native reads never return sealed ciphertext past its 7-day lifetime. `who`, `claims`, `keys_publish` and `events_*` stay on the Python upstream.
- Fleet Hub (Rust): a native long poll is watched before its first read, so a message another process commits before the poll registers wakes it at once instead of at its deadline.
- Fleet Hub (Rust): native long polls count only waiters against the waiter caps, so idle keep-alive connections no longer refuse a wait with `message_wait_capped`.
- Fleet Hub (Rust): a native long poll whose principal gets a new token while it waits ends with `message_sender_inactive`. Python's `list` checks only that the principal is active.
- Fleet Hub (Rust): a waiting native `list` releases its request body and body-byte reservation once parsed.
- Fleet Hub (Rust): native message writes and the retention sweep delete at most 1,024 expired messages per command, so a large retention backlog drains without holding writes at 503.
- Fleet Hub (Rust): an expired sealed row with a corrupt envelope reads as `content_expired` with no envelope instead of returning its stored bytes.
- Fleet Hub (Rust): with more expired messages than one native command deletes, the recipient and sender-to-recipient unacked quotas and the sealed storage quotas count only messages inside the retention window, so a send no longer gets a false `429` while the backlog drains.
- Fleet Hub (Rust): a native send whose `client_id` matches an expired message that is still stored deletes that message and inserts a new one, as Python does after its prune, instead of answering `409 message_client_id_conflict` or replaying the expired message.
- Fleet Hub (Rust): a waiting native `list` keeps only `after`, `unacked` and `wait_ms` once its body reservation is released, so padded keys are freed before the wait.
- Fleet Hub (Rust): when the external-commit watcher's query fails, the next tick reads the same rows again, so the waiters for those messages still wake.
- Fleet Hub (Rust): a native `reply` retry whose expired parent is still stored behind a retention backlog finds the accepted reply and answers `201` with it, as Python does, instead of `404 message not found`.
- `brigade memory hub status|pull|push|import|card` reads and publishes hub memory on a Rust hub. A `[hub]` table in `.brigade/memory-care.toml` sets the mode (`off` by default, `read`, `sync`) and the publish scope (S14).
- In `read` and `sync` mode, `memory search` fuses hub and local matches, labels where each came from, and falls back to local cards and the per-host replica when the hub is unavailable. `off` mode output is unchanged.
- Writes the hub cannot take right now are spooled and replayed with the same `client_event_id`. A conflicting offline edit becomes a local proposal instead of overwriting the hub. A spooled write replays only into the current `[hub] scope` and only onto the same readable, public card at the same path. A write spooled under another scope becomes a local proposal that is never sent, and so does a write for a card that turned private, was renamed, moved, re-identified, duplicated, deleted or made unreadable. Dot and Duet memory tools follow the same mode.
- Hub memory reads stay inside the workspace's read scopes: `[hub] read_scopes`, which defaults to `[scope]`, limits every hub search, card read and the replica, so Dot and Duet can no longer read other scopes the credential can read. The client uses only the node token and never falls back to the admin token.
- The replica belongs to the principal the hub's `status` names, which now reports `principal`, `credential` and, for a `scopes` list, `readable_scopes`. A different principal rebuilds the replica, and a scope that is no longer readable is dropped at the next pull.
- A refused hub search (401, 403 or another coded answer) answers from local cards only and never from the replica. Redirects, 408 and 429 count as unavailable, so writes spool instead of becoming proposals, and a 401 at the push preflight spools too. A write-only credential can push.
- `memory hub proposals list|drop` lists open proposals and closes one after review. `drop` needs `--keep-local` (the next push writes the local card over the hub head) or `--keep-hub` (the local card file takes the hub head), and refuses unless the replica is current for this hub, principal and `log_epoch` and holds the card head the hub's `card` answers now, so a replica pulled before the conflicting write is refused. `--keep-hub` finds the local card by id, rewrites a moved card where it is, and refuses when the card file is not UTF-8. Both refuse when two files hold the id or the proposal's scope is not the current `[hub] scope`, and `--keep-local` says when the next push sends nothing. `--keep-hub` refuses a private local card. A dropped or resolved proposal moves to `.brigade/memory/hub/proposals/dropped/` with its text. A proposal also resolves on its own when the local card takes the hub head the conflict reported, the replica agrees, and the hub's `card` answers that head under the replica's `log_epoch` at push time. Spool entries for another hub no longer block cards or count against the cap.
- `push` and proposal changes hold a lock on the workspace's hub state, which now lives in its own `.brigade/memory/hub/` directory. `pull` holds its own lock and takes the state lock only while it changes the replica, so a long pull no longer blocks a push, and a run that waits too long for a lock exits 75 saying another push or pull is running. Pulls check the epoch on every card answer and again before saving, write `heads.json` a few times per rebuild instead of once per page, and fetch cards left out for the cap once space frees.
- Dot and Duet `memory_card` take an optional `scope`, so a card held in two read scopes can be opened. Search reports the hub's `match_count`, warns when `--limit` exceeds the hub's 20, and keeps the same bytes in two scopes apart.
- `status`, `pull`, `push`, `import`, `card` and `proposals drop` refuse when the hub reports that the node-token slot holds its admin token, so Dot and Duet `memory_card` refuse it too. Search makes no `status` call and cannot tell. `memory hub card --all` refuses to join chunks from two `log_epoch`s or text that does not match the card's `content_hash`, and while the hub is unavailable `memory hub card` and Dot and Duet `memory_card` read the card from the replica, never from a read scope the hub refused the card in during that read, and not at all after a `status` 403, a `log_epoch` move during the read, or an answer in the read that names another principal or `log_epoch` than the replica's.
- A spooled write sends the card as it is now. When the card changed while the write waited, the spooled text is dropped and the current text goes out in the same push on the same base, so a line the user removed offline never reaches the hub. If the old write already committed, the current text becomes a proposal. A write that reached the hub cannot be taken back.
- Push, spool replay and import withhold a card unless its `visibility` is absent or `public`. A byte-order mark, an inline comment, another casing or a duplicate `visibility` line no longer lets a private card through, an unknown or empty value is skipped as `visibility-unrecognized`, and so is a `visibility:` line in a leading block that does not parse as frontmatter.
- `proposals drop --keep-local` after another writer retired the card keeps the retired head's revision as the base, so the next push revives the card with the local text instead of conflicting again.
- When the hub refuses a scope's `card` read during a pull, the pull drops every head already pulled in that scope and does not mark it readable.
- Push adopts a replica head as a card's base only when the replica was pulled under the hub's current `log_epoch`, and skips the card as `replica-stale` otherwise. `import` names a replica base only when the replica belongs to the principal and `log_epoch` the hub reports now.
- A pull refused every read grant discards the hub's replica, a scope refused during a pull is saved at once, and a card published under an older `log_epoch` is sent again after a restore instead of counting as synchronized.
- Gate merges on the Rust hub suite. `ci.yml` now calls `hub.yml` as the `hub-rust` job and `ci-gate` requires it, so a PR that touches hub paths can no longer merge with a red hub job. A PR that touches no hub paths may skip it, and the merge queue always runs it. `hub.yml` is now callable only and no longer triggers on its own.
- The S31 `access` check now probes only the hub's own listeners, found by the
  hub processes' socket inodes, so the front's tailscaled peerapi in the shared
  namespace no longer fails it.
- A reachable host loopback listener at a hub port still fails `access`. An
  operator can exempt one with
  `--allow-loopback-listener PORT=EXE_REALPATH//REASON` when running the check
  as root. The exemption applies only when a complete census of the host's
  processes, taken while none vanished, finds that every holder of the socket
  runs exactly that executable and is not the hub, and the hub's network
  container publishes no port. The record keeps the path, the reason, the census result, each
  holder's pid, executable and command name, and the listener's status line
  and headers.
- The S31 checks append to `--record` without following a symlink, refuse a
  non-regular file, and as root refuse a record file owned by any account other
  than root or the one that ran `sudo`.
- Scan hub memory writes with the pinned native guard. Block unsafe content without echoing it, store warnings as flagged, and require a clean current scan for imports and internal push hits. Prompt recall replaces findings with rule-only placeholders. Builds without the scanner keep writes pending and refuse trust raises.
- Preserve all line boundaries and normalize whitespace for every injection rule. Fold full-width ASCII and strip default-ignorable characters in scan copies. Block hidden selectors and bidi marks, match ASCII word boundaries locally, refuse per-input engine errors without storing bytes, and withhold prompt hits with empty rule lists.
- Bound cross-line salutation matching and accept supported-size memory corpus inputs. Input-specific scan refusals report `retryable: false` and become local proposals without automatic retries.
- The salutation rule bound (`.{0,256}`) also applies to the shared untrusted-text rules used by connectors.
- Bound shared memory scan patterns to prevent long near-matches from stalling connectors or exhausting the hub match limit.
- Keep over-bound secrets blocked with bundle v5, including large JWTs and PEM keys. Guard redaction now covers the full token run in Python and native paths, so the content guard and evidence redaction leave no secret tail.
- Reduce Python connector scan cost with address prefilters and indexed assignment digits. Test prefilter parity and separate measured scan time from the subprocess safety timeout.
- Keep PEM secret findings when expanded bodies overlap other detectors. Cap unterminated PEM masks at contiguous body lines and preserve paragraphs after header-only mentions.
- Load adversarial test helpers by file path for individual Windows test runs. Anchor the reserved-email prefilter at the domain start and give Windows scan timing checks measured headroom.
- Memoize PEM END searches and skip disjoint PEM overlap comparisons. Mask truncated bodies with consistent indentation, quote prefixes, escaped newlines or inline spaces. Keep later detector decisions independent of secret masking policy.
- Reduce folded memory scan cost by skipping raw copies without required bundle literals and avoiding assignment indexes without a prefix. Preserve Unicode phone boundaries and filter parity.
- Mask unterminated encrypted PEM bodies after consistently prefixed metadata and escaped JSON metadata. Accept literal `\r\n` separators and reject partial inline base64 tokens. Track the remaining overlap-check cost in #2310.
- Check PEM header scan scaling across an 8x input step and memory corpus scaling across its existing 4x step, with separate hang caps for slower CI runners.
- Add opt-in native heartbeat routes for event batches, run status and interactive
  session presence through the shared `FULL` writer. Add wire and storage parity
  checks, load measurements and a conductor-run deployment procedure.
  Preserve Python fallback for unusual event integers, stored JSON and damaged
  status storage. Reserve writer bytes for durable claims and messages, include
  the commit histogram sum and log heartbeat configuration at startup. Make the
  outbox parity check independent of dispatcher retention timing.
- Batch queued claims, messages and registration ahead of ordinary heartbeat
  work, so a claim behind 256 concurrent 1,000-event posts no longer waits
  past the claim client's timeout. Add 1,000-event and multi-node options to
  the S6 load harness, with claim latency in the mixed summary.
- Bound ordinary writer work under sustained durable load. An expired heartbeat
  or sweep at the head of the ordinary queue is answered `503 writer-timeout`
  at the next pick, and one that has used half its budget runs first in the
  next batch. The writer queue depth gauge now counts commands taken off
  the channel during a batch.
- The native hub messages test for padded waiters no longer fails on slow runners. The waiters now
  run to the 20-second wait maximum until a send wakes them, and a waiter refused by a probe that
  briefly held a waiter slot is started again, so a probe refusal always means two waiters are parked.
- Add `brigade memory hub mirror --out DIR` to export readable hub cards as a disposable Markdown folder with stable filenames, scan and privacy filtering, a scope index, atomic writes, dry runs and owned-file cleanup.
- Include scan state, writer and update time in hub card reads and preserve them in S14 replicas.
- Add the hub memory embedding transport with endpoint policy, secret-file authentication, outgoing scanning, batching, deadlines and a query breaker with a background probe.
- Add explicit lexical retrieval configuration and retrieval mode, effective mode and state in memory status and metrics.
- Add leased embedding jobs, vector validation and storage, shared-vector lineage collection, and a pull-mode memory embed-worker.
- Add segmented dense retrieval with RRF, safe lexical and hybrid switches, coverage reporting, and idempotent memory reindex with targeted drop.
- Stop storing embedding lease replies in memory idempotency, so retired card text cannot be replayed.
- Retry failed embedding documents alone with backoff, park them after five attempts, and add the embed_failed call for pull workers.
- Count only embeddable cards toward hybrid coverage, and embed only clean text.
- Queue embedding jobs from put and retire, replay dense changes from a vector log, and back the scheduler off when idle.
- Refuse dropping the active embedder, validate drop ids, and drop in bounded writer turns.
- Refresh hybrid coverage when a lease parks text, back the pull worker off while its endpoint is down, and back off documents a worker already retried alone.
- Report hybrid status as degraded with `all-embeddings-failed`, and answer search lexically, when every embeddable card has parked on an embedding failure and none has a vector.
- Refuse a `memory.embedder.pull` value that is not a boolean, and report a malformed embedding reply as an endpoint fault instead of stopping the pull worker.
- Load the dense matrix on its own connection at startup so a slow or failed first load leaves the hub serving lexically with a `dense-load-failed` reason, report `dense-not-loaded` instead of hybrid until a matrix exists, and show memory hub status coverage over eligible cards.
- Retry the first dense load without holding the writer lock, leave that load to startup and the scheduler instead of the writer thread, and skip the embedding call when the installed matrix has no rows.
- Hub memory can use its own hub. Add a `[memory_hub]` table with `hub_url` and `node_token_file` to `~/.brigade/fleet.toml`, or set `BRIGADE_MEMORY_HUB_URL` and `BRIGADE_MEMORY_HUB_TOKEN_FILE`, and memory commands use that hub and token while `brigade fleet` keeps using `[fleet]`. The override refuses a fleet node or admin token, a URL with a user name or password, and a relative token path.
- A workspace `[hub]` table that sets `hub_url` or `node_token_file` is refused, so a workspace file never chooses where a credential goes.
- `brigade memory hub status` reports which hub memory uses and where it came from (`env`, `[memory_hub]` or `fleet`), and never shows the token or URL credentials.
- Dot and Duet pass `BRIGADE_HOME` to the `brigade` commands they run, so those commands read the same `fleet.toml`. A Dot service that sets `BRIGADE_HOME` now keeps its local memory store under that directory instead of `~/.brigade`.
- Hub memory scan bundle `brigade-memory-scan.v6` adds a `private` class for network addresses, ports, local host names, root, home and Windows paths, email addresses, phone numbers, U+034F and U+200C. Private cards are stored, imported, searchable and loaded into local prompts, and every connector path refuses them with `memory_content_private`. The model providers behind the operator's own local harnesses are inside the boundary. The publication guard keeps its severities.
- Memory schema version 3 lets scan state `private` be stored, on top of version 2 (the S15b embedding tables). A version 1 or version 2 memory file migrates on open with every row kept.
- Embedding queues and leases a private card only when the configured endpoint is loopback (push mode, where the hub embeds in process). For any other endpoint a private card is unembeddable in `coverage`, neither eligible nor failed, and a lease-time rescan that finds private text parks the job as `content-private`, which is not transient. `embed-worker` accepts a `private` receipt only for a numeric loopback endpoint and reports anything else as `content-private`.
- U+FE0E or U+FE0F after an emoji base, and U+FE0F in a keycap, no longer block. U+FE00 to U+FE0D block after any character.
- Local prompt recall strips U+034F and U+200C from the fields it renders. Stored bytes and content hashes keep them.
- `classic-injection` matches "exfiltrate" only as an order: at a line or sentence start (after markdown marks, a checkbox, a list number, a non-ASCII symbol such as an emoji, or urgency words), after a short address or an if/when/for clause and a comma, addressed to the agent ("make sure to", "you are required to"), with a destination such as a bare domain, or as a compound order that opens a line with an imperative verb. `scan_untrusted` and the handoff injection lint use the same pattern. Its repeats cannot overlap, so a run of Unicode spaces no longer exhausts the native match limit and fails the scan. The compound order stays on one line in the line copies. `scan_untrusted`, the handoff injection lint, `learn` and the security scan search a copy with Unicode spaces folded and indents stripped, so an order set off by a no-break space, U+3000 or an indent flags on any line, as it does in the hub.
- A memory migration step that another hub process already applied is skipped.
- The S15a embedder sends private text only to an endpoint whose pinned address is loopback, and refuses any other endpoint with `content-private` before a lookup or connection. A stored `private` version embeds only for a loopback endpoint.
- `memory hub mirror` exports cards whose scan is private.
- Pull mode no longer lets the hub's query endpoint decide the private gate, because documents go to the worker. Private cards are never queued or leased to a pull worker unless `[memory.embedder] pull_private = true`, which asserts that every worker holding the embed grant runs on the operator's own machines. Without it they count as unembeddable in `coverage`. The key must be a bool, needs `pull = true`, and shows in `memory status`.
- A worker that reports `content-private` or `content-blocked` parks the job at once instead of going through the retries that lease the text again.
- Dot and Duet `memory_search` and `memory_card` now run the same prompt-load scan as Grok Bot memory. A card with private-network, path, email or phone text returns `{card_id, blocked, code: "memory_content_private"}` with no text, and a blocked or injection-shaped card returns `{card_id, blocked}`. `memory_search` still lists a refused match by id (and hub scope) without its title, summary or tags, so the card can be opened locally.
- `brigade memory hub import` takes folders as well as files and plans before it sends. `--dry-run` needs only read on the scope, lists exactly what the real run appends and writes nothing. It refuses when the hub's scan ruleset differs from this client's.
- Import keeps a frontmatter card id the hub accepts and otherwise derives a stable id from the note's path relative to one root: `--root`, else the nearest `.obsidian/` ancestor, else the common ancestor of the arguments. It skips dot folders, any folder at or under the hub mirror's marker, symlinks and `--exclude` globs, and skips `visibility: private` notes with the push rules.
- Import deduplicates by the hub's content hash and reports each note as `import`, `unchanged`, `duplicate`, `changed`, `revive`, `unknown-head`, `private`, `visibility-unrecognized`, `blocked`, `flagged`, `collision` or `error`, with rule ids only. A second run appends nothing, and `--update` overwrites a changed card from its current revision and brings back a retired one. A dead hub scanner stops the run after three retryable refusals.
- Hub memory scan bundle `brigade-memory-scan.v7` adds an NFKC scan copy beside the v6 full-width fold copy, in the Python scanner and in the native hub (`unicode-normalization =0.1.26`). Math alphanumerics, ligatures, superscripts, circled letters and full-width forms now match the same rules as their plain spellings. NFKC runs only on code points Unicode 14.0.0 assigns, so Python 3.11 and later and the crate agree. Keeping the fold copy makes every verdict at least as severe as v6. The ruleset digest changes.
- The test suite resets the once-per-process engine deprecation notice before each test, so receipts tests that use a legacy command spelling no longer depend on test order and assert the exact notice on stderr.
- `memory search` and the other memory-care readers decode `.brigade/memory-care.toml` as UTF-8 on every platform instead of the locale encoding, and ignore a leading byte-order mark. Bytes that are not UTF-8 now fail with `invalid memory-care config` on Windows as well. The vault config reader accepts a byte-order mark too.
- The managed Claude hook wrapper no longer passes a crashed handler's exit status through to Claude Code. Any non-zero status from `brigade-hook` now becomes exit 1 with a stderr warning, so a pipx reinstall racing the hook no longer shows up as a blocked prompt (exit 2). Run `brigade work hooks update --scope user` to refresh the installed script.
- Dot, Duet and Grok Bot memory tools no longer send absolute local paths to the remote connector. `memory_search` returns the workspace directory name and workspace-relative match paths, and `memory_card` returns `memory/cards/<name>.md`. A match path that cannot be shown relative to the workspace is refused as `malformed-match`.
- Hub fields in those payloads are checked before they leave. `source`, `trust`, `scope`, `revision` and `sha256` keep only known values and become `null` otherwise, `retrieval` keeps only its documented keys and values, and `warning` is one of three codes instead of the CLI text.
- Fixed a port race in the hub transport tests: the fronts now start on port 0 and report the bound port, and the stub upstreams hold their socket, so no test releases a port before using it.
- The hub's first dense matrix load now replays vectors committed while it scanned, so they rank without waiting for another write.
- Memory status reports `effective: lexical` while the loaded matrix has no live row, and search skips the embedder query call in that case.
- Hub memory accepts a `collection:<name>` scope for imported note collections, such as an
  Obsidian vault. Node tokens get no default access to it and the `*` and `repo:*` grants never
  match it, so only an admin grant naming the exact scope opens read, import, put or embed.
  `[hub] scope`, `read_scopes` and `memory hub import --scope` accept the form, and
  `docs/memory-operations.md` lists each scope form with its default access.
- `scan_text` now finds overlapping detector spans with a sorted sweep instead of comparing every pair, and joins redaction edits in one pass, so scan time grows linearly with the number of findings. Findings and redacted output are unchanged.
- Restore the task module namespace after replay reload tests so later native T3 contract tests retain matching task class identities.
- Add a same-process regression that runs replay reload tests before native T3 intent validation.
- Add repeatable `brigade memory hub mirror --exclude-scope` to omit exact scopes and remove their previously generated notes and index sections. Dry-run and JSON reports include excluded scopes and skipped card counts.
- `brigade memory hub import` skips a folder holding the vault projection marker (`.brigade-memory-projection.json`), checked in the argument's folder, every folder above it and its resolved real path, and counts it under `projection_dirs` in the discovery line. The projection writer now creates that marker, so a vault's Brigade Memory folder is never imported back into the hub.
- `handoff_write` and `fleet_policy` now return workspace-relative paths for connector profiles with the prompt-load scan, and `null` when a path cannot be made relative or does not scan clean. Profiles without the scan keep the absolute paths.
- The Grok Bot memory profile now lists memory_search, memory_card and handoff_write, reads hub memory under its own principal when hub memory is on, and files handoffs to the hub review queue instead of a file inbox.
- A new grokbot-inbox pack (message_send, inbox_list, inbox_ack, inbox_reply) runs under its own principal, and a send outside its allowlist returns message_recipient_not_permitted.
- The vault-backed cerebro-memory pack, its five cerebro_* tools and the --cli-executable and --workdir setup flags are removed.
- The Grok Bot memory profile never serves a local card, pins its memory calls to the credential its listener authenticated at startup, and serves offline results only from a replica its own principal pulled.
- Add reviewed hub memory proposals with whole-submission validation, per-target scope authorization, pinned blocks, atomic multi-card decisions, and separately reviewed rollback proposals.
- Upgrade memory schema v3 to v4 without changing existing cards or the log epoch. Automatic apply remains disabled.
- Require read access for proposal targets and a different reviewer for non-admin acceptance. Preserve opaque-block bytes, reject multiline metadata edits, and record held scan decisions and current rollback snapshots.
- Include scan rule ids in held proposal responses and decision events.
- Keep truncated PEM masking across bare quoted blank lines and body lines with trailing spaces, and stop escaped metadata at JSON string quotes.
- Add a negative test for ordinary text with the same layout shapes.
- Derive valid memory push ids with import's path helper, preserve valid legacy ids and refuse collisions.
- Exit 1 when memory push refuses writes or parks proposals, while publishing good cards and printing final outcome counts.
- Document push exit codes and partial failures in scheduled sync services.
- Compute hub proposal tiers from current scopes, trust, scans and pinned card inputs, ignoring submitted tiers and keeping uncertain edits at Tier 2.
- Add a digest-bound fleet policy memory section and record the current policy on every explicit proposal decision. Require a fresh decision after a policy change.
- Keep all tiers behind explicit review and refuse memory auto-apply policy settings until evidence attestation is available.
- Keep duplicate merges at Tier 2 when front matter differs outside aliases and evidence. Strip client tier claims before storing ops.
- Require the current expected policy digest before re-deciding a policy-stale proposal, with authorization and revision checks repeated before apply.
- Keep duplicate merges at Tier 2 when front matter contains line boundaries that disagree with the shared metadata reader.
- Escape reader line boundaries in every front matter JSON write and require lower-tier ops to preserve all protected metadata. Keep decoded boundary values in rewritten lists at Tier 2.
- Prevent storage parity checks from accepting a competing hub during startup.
- Make zenity timeout rounding checks independent of runner scheduling.
- Memory scan ruleset IDs now cover both scanner implementations, with scanner tests kept outside the pinned Rust source. CI checks source pins and shared Python and Rust verdicts.
- Add admin-only `memory rescan` to refresh current ruleset receipts in bounded batches without changing card bytes, trust or revisions. Update the client and hub together, then run rescan with `--until-done`. Failed blobs get non-clean receipts so the remaining hashes can drain.
- Rescan separates corrupt storage from scanner failures, logs corruption hashes, and reports a failed exit after a corrupt drain. Admins can use `--retry-failed` to retry scanner failures under the same ruleset ID.
- Prevent the Rust hub's Python helpers from importing modules planted in the working directory or PYTHONPATH.
- Preserve relative command-line paths while isolated Python helpers use a fixed working directory.
- Preserve relative deck configuration and findings key paths from the environment, with command-line deck configuration taking precedence.
- Keep the read-only audit launcher active with isolated Python startup and reject benchmark interpreters that load a different Brigade copy.
- Preserve relative interpreter paths, expand home paths in the findings key setting and treat a whitespace-only findings key setting as unset.
- Require isolated interpreter startup before the read-only audit can certify a hub.
- Add read-only hub Tier 0 preflight reports and recheck gates inside reviewed apply transactions.
- Enforce Tier 0 op-class and proposer policy allowlists while keeping automatic apply disabled.
- Remove the policy metadata test's dependency on timestamp clock resolution.
- Add the optional `memory-reflect` care schedule for hub memory proposals, with a persisted pause after three failures and status and recovery commands.
- Add hub-backed `memory reflect run` with zero hub writes during dry runs and `memory reflect rollback` for compensating proposals that need a separate decision.
- Add hub evidence attestation with exact workspace identities, freshness checks and live credential revocation checks.
- Add curation ownership policy, a persistent admin kill switch and audited Tier 0 automatic apply. Production apply remains held until retrieval evaluation is bound.
- Redact automatic replies and replays for callers without review on every target. Enforce clean evidence, attachment caps and admin tombstones, with card-wide contrary holds and independent principal sources for newly added evidence.
- Hub search collapses scoped supersession and merge chains, preserves reachable revision references, and sorts unreviewed automation after interactive results.
- The retrieval eval harness supports hub lexical and hybrid adapters, configurable RRF weights, R0 parity, and public R1 reports with stable index, scorer, policy, mode and candidate bindings.
- Lineage accepts trusted same-scope proposal links, preserves separate terminal heads and applies push eligibility to the complete chain. External evals bind the full scope and report coverage timeouts explicitly.
- Lineage requires trusted endpoints, resolves terminal cycles independently and preserves filename-like card ids. A 512-node budget falls back to uncollapsed hits and reports overflow.
- Add admin-recorded retrieval evaluations that bind hub Tier 0 automatic apply to the live index and policy, with zero head-write drift and 24-hour expiry. Automatic apply remains off by default.
- Report the persisted pending proposal state when an automatic attempt is held, and document how to record evaluations against the final policy revision before enabling apply.
- Refuse retrieval records with stale evaluation order or inadmissible timestamps, and recheck R0/R1-public metrics before automatic apply.
- Allow an optional policy fixture digest pin and document the global one-shot retrieval arm.
- Hub automatic memory apply accepts scoped content and membership drift up to min(5, floor(min(snapshot cards, live cards) / 50)), while holding targets changed since indexing. Default ranking also counts in-scope head changes, including trust flips, reverts and same-bytes recreates. Raw lexical ranking keeps unrelated trust-only revisions free. Writes outside the bound scope do not consume the budget.
- Retrieval evaluation records store member snapshots and evaluation timestamps. Newer valid reports can replace unreadable reports. Legacy records require a fresh evaluation after the schema 8 upgrade.
- Preserve hub lineage, source-demotion, and ranking details in `brigade memory search --json` results.
- Hub reflection selects scopes before card reads and uses bounds of 50,000 cards and 64 MiB of text per scope. Failures report specific codes and reasons without card text.
- The optional memory-reflect care entry accepts one configured scope and retains it across reinstalls. Operator setup documents a dedicated reflect folder, replica and user-level credential drop-in.
- Add `memory proposals list`, `show` and `decide` for reviewed hub proposals, with paged summaries, review-gated diffs and explicit policy digests. Credentials come from a private token file or explicit memory settings.
- Proposal pagination uses opaque hub-local HMAC handles. Accepting a proposal requires an explicit policy digest from operator inspection, while rejecting remains available without a pin.
- Reflection refuses oversized on-disk replicas before parsing and requires a dedicated replica for non-dry scoped runs. Existing scopes remain unchanged when that check refuses a run.
- Windows reflect task replacements preserve the installed scope. Global proposal listing requires an explicit `list_proposals` operator grant, and per-principal cursor quotas prevent cross-principal eviction.
- Reflection verifies stored counts with a bounded streaming scan before dictionary decoding, caps metadata reads, and enforces the 50,000-card replica bound on live cards once the log replay reaches its head, before fetching full bodies and before saving. Cards retired later in the log, including duplicates retired by an accepted proposal, no longer block a rebuild.
- Replica snapshots are written with untranslated newlines, so the stored snapshot digest matches the file on Windows.
- Replica saves name each heads snapshot by its digest and publish it by replacing the metadata that names it, so a save interrupted at any point leaves a consistent replica that the next scheduled reflection can load and update.
- A replica whose metadata names a missing heads snapshot is no longer read as an empty, caught-up replica: search, `card` and `status` treat it as unusable, and the next pull rebuilds it, including reflection's bounded pull, so a scheduled reflection recovers without an unbounded pull. Replica saves sync the directory after each rename on POSIX and report directory I/O errors instead of ignoring them, and the memory operations guide documents deleting the replica and replica.building directories before a downgrade.
- Bound hub proposal storage with configurable per principal and per scope pending and byte quotas, pending deduplication, and immutable terminal audits after seven days.
- Run automatic apply diagnostics in budgeted read snapshots before writer locks, recheck compact freshness bindings under the lock, and rate-limit repeated attempts. Automatic apply remains off by default.
- Bind hybrid retrieval evaluations to dense vector state and hold automatic apply when the vector snapshot changes.
- Redact review-only proposal hashes and diagnostics from historical scope-reader event payloads. Upgrade hub memory storage to schema 9.
- Let reviewers drain a proposal backlog that sits above a lowered or newly defaulted pending limit. Each quota dimension now passes when usage stays within its limit or does not grow, so admission stays strict and decisions still cannot push retained bytes past a limit.
- Bound reflect log replay work and connect to Task Scheduler before replacing Windows care tasks.
- Added `brigade memory anchor` to backfill claim anchors on local memory cards. It is a dry run by default and reports counts only. With `--apply` it anchors cards using the same function as proposal creation, so hub reflection and reviewed proposals can address their blocks.
- Scheduled hub reflection now also finds near-duplicate cards. Cards whose claim bodies reach a word 5-gram Jaccard threshold (default 0.9, set from 0.5 to 1.0 with `[reflect] near_duplicate_threshold` in `memory-care.toml`) get `supersede_card` proposals that keep the least recently written card, so a new card or a rewrite never becomes the keeper. These proposals are Tier 2 and always wait for a reviewer.
- Near-duplicate reflection opens at most 8 new proposals per run by default (`[reflect] near_duplicate_max_proposals`, 0 to 32) and queues exact merges first. Clusters take turns by least recently proposed and oldest first seen, keyed by card ids, so rewriting members cannot hold the same slots every run. Its index, pair expansion and verification work are bounded per run, and pair work is charged before dedupe.
- A full hub proposal quota no longer fails scheduled reflection or counts toward its pause. The run stops proposing and counts the unsent submissions as `proposal_quota`. A duplicate pending refusal now records the existing proposal instead of storing a refusal.
- Prune obsolete hub-reflection submission records so the scheduled state ledger remains bounded as cards change.
- Docs only: added the design and slice plan for memory verification, a reviewed reflect pass that checks hub cards against their cited evidence, and per-card read counts that set how often each card is checked (`docs/phase-memory-verify.md`, `docs/phase-memory-verify-plan.md`). The design puts a hub-enforced review floor on verifier credentials, isolates evidence per scope, and requires two independent pieces of evidence for any correction.
- Scheduled hub reflection now repairs broken `[[links]]` in anchored claim blocks. A link that names no live card in its scope is repointed with a `replace_block` proposal when exactly one live same-scope card is the clear target: the successor of an accepted supersede, or the one card whose id, alias, topic or title matches after case, whitespace and hyphen normalization. Alias, heading, folder prefix and extension are kept, and code spans and opaque blocks are never touched. Links with no target, several targets or only a cross-scope target are counted under `link_no_target`, `link_ambiguous_target` and `link_cross_scope_target` and left alone. A target or successor must be scan clean and share the source card's trust class, and malformed `aliases` metadata disqualifies a card, so those cases are counted and skipped. Titles, topics and aliases of retired cards follow the successor rule. Every proposal is Tier 2 and waits for a reviewer.
- Link repair opens at most 8 new proposals per run by default (`[reflect] link_repair_max_proposals`, 0 to 32) after exact merges, alternates with near-duplicate proposals so neither starves the other, takes turns by least recently proposed, and reports counts without card text. Reflect reports now leave out the replacement wording of each op.
- Fix verification receipt reuse after editing external or gitignored script arguments by binding file paths to SHA-256 hashes. Non-ignored in-tree directories remain covered by the tree fingerprint, and bare program names do not affect reuse. Unreadable files, uncovered directories, other non-regular files, and files over 16 MiB disable reuse. Reuse output names the source receipt and points to `--no-reuse`.
- `brigade handoff draft` now publishes each draft atomically. It writes a private `.handoff-<random>.tmp` file in the inbox, fsyncs it, then renames it to the final `*.md` name without replacing an existing file, so a concurrent reader or worktree cleanup never sees a half-written handoff and an existing draft is never overwritten. The temp file is removed on any failure. The contract for harnesses that write handoffs directly is documented in `docs/handoff-promotion.md`.
- Rust fleet hub startup now fails promptly when resolving `--host` times out or fails.
- Bound the front matter parse that card search, hub sync, proposals, memory care and the doctor lint share. A bracketed value longer than 16 KiB or nested deeper than 32 levels is now kept as plain text instead of being evaluated, and any evaluator failure (including `MemoryError` and `RecursionError`) does the same. One card with a huge or hostile list value no longer slows or crashes `memory search`. The memory hub's native search applies the same bounds, so both engines index the same fields. Normal tag and alias lists parse exactly as before.
- A scanner import is no longer rejected as "import file not found" when another process creates or removes a directory in the target or one of its parent directories while the import is being opened. Only a swapped path component still refuses the import.
- `brigade handoff lint` and `brigade handoff draft` now refuse a `create-card` handoff whose card id (the `id`, else the `topic`, else the file name, as `memory hub push` derives it) already belongs to another card. The error is `card-id-collision` and names the existing file, so a new card can no longer make every later push refuse both cards. `update-card` on the card that holds the id stays allowed. `brigade ingest` routes the same collision to the review inbox and does not write the card, and lint and ingest read the card ids once per run.
- Push card reads use a bounded, padded scope discovery budget so withheld copies cannot add revision and scan joins to a negative response. Scope-less reads of ids with more than 64 copies fail closed with the same 404 and require an explicit scope.
- `brigade work import bd` and the ledger mirror now accept all 12 bd 1.3.4 link types. `conditional-blocks` becomes a `blocks` edge, the eight non-gating types are kept as `metadata.bd.links` and counted in the receipt, and any other type still fails with `unknown_edge_type`. bd 1.3.4 is added to the tested releases.
- `brigade run cloud register`, `adopt`, `status` and `sync` now track Claude cloud sessions that a
  conductor launched through a one-time routine. New `--routine-id`, `--issue`, `--session-url` and
  `--observed-status` flags record the routine, the issue and the run state the conductor read.
  Routine rows classify from GitHub branch and PR state plus that report, and registering one needs
  no hub cloud lane. The `claude-cloud` source reports `conductor-routed` instead of
  `disabled-by-policy`, `doctor --provider claude-cloud` passes offline, and
  `launch --provider claude-cloud` refuses with a pointer to the conductor runbook in
  `docs/claude-cloud-capabilities.md`.
- Add `brigade run cloud land --provider codex-cloud --task-id ID --issue N` to apply a ready task in an isolated worktree and open a PR for conductor review. Refuse protected paths and apply conflicts, run content guard, and record the PR in cloud status and sync (#2473).
- Isolate landing Git hooks and drivers, apply one hashed task attempt, reject unsafe paths and shallow targets, and resume publication from the recorded commit after push or PR failures. Keep guard receipts outside proposal trees and expose pending landing states.
- Run landing Git operations in a private temporary repository fetched from canonical GitHub HTTPS. Preserve prepared commits there for publication retries. Use only operator global commit identity and one validated absolute credential helper, pin HTTPS and TLS verification, and reject unexpected effective config before Git operations. Landing ignores caller config, includes, object stores, and proxies. Proxies are unsupported. Landing refuses non-POSIX platforms with `unsupported-platform`.
- Skip tracked submodule gitlinks in content guard and publish checks while retaining fatal errors for unreadable regular files.
- Reconcile every private checkout before retrying preparation or publication. Reuse a registered checkout only when the recorded branch and commit match the pending step. Remove stale checkouts without following symlinks or overwriting a remote ref.
- Accept a credential helper through an absolute symlink to the pinned trusted `gh` executable while retaining the exact `auth git-credential` arguments.
- Read tracked symlink target text from its index blob across Git, publish, audit, and baseline scans without following the link. Keep unreadable regular and executable files fatal and reject unsupported index modes with `unsupported-index-mode`.
- Discard inherited guard, Brigade, Python, and legacy engine path overrides and exported shell functions during landing. Use the embedded Python scanner and pinned policy copies for hooks and publication text. Scan the title, final commit message, and PR body before committing, recheck saved publication text on retry, and refuse blocked text with `title-refused`. Guard directory overrides are unsupported. Strict text scans include lines containing allow markers.
- Refuse trusted-tool symlinks that resolve into any caller checkout or the private landing root.
- `python -m brigade.dot runtime-template --pack dot|duet` prints a placeholder runtime document whose
  `hub.recipients` lists a `conductor` alias, and the Dot and Duet guides name the alias, so a fresh
  install keeps a way to start a message to the conductor.
- `fleet_digest` carries an `unread` section: the connector's own pending inbox count, and its own sent
  messages that each recipient has not acknowledged in the last 7 days. Both come from the connector
  principal's own credential.
- `brigade fleet msg sent --undelivered-after MINUTES` marks each sent message `acknowledged`, `pending`
  or `undelivered`, and lists the ids that passed the deadline with no acknowledgement.
- `inbox_list` and `fleet_digest` add `sender_alias` to a message whose Hub-authenticated sender is a
  configured recipient. It authenticates provenance only.
- `inbox_list` and `fleet_digest` add a connector-computed `assignment` object to a plain, unthreaded,
  non-reply message from the configured `conductor` principal whose `ref` is exactly `owner/repo#N` with
  the repository listed in the new `assignments.repositories` runtime key (default empty). The Dot and
  Duet operating instructions let only that field authorize one branch, commits, a push and one pull
  request for that issue. Message text authorizes nothing.
- The connector `handoff_write` path now has an integration test over a temporary workspace: the
  real handoff lint refuses a bad handoff, a valid one lands in the profile's writer inbox and is
  never overwritten, and after `brigade ingest` the card is found by `brigade memory search`. The
  Dot and Duet connector docs describe the path.
- Message replies keep an immutable reply marker after retention cleanup removes their parent, so connectors cannot mistake an old conductor reply for a new assignment.
- Reject delivery deadlines that are too large to calculate.
- The Fleet Hub image now pins a Brigade revision with the Rust hub engine and hybrid retrieval support.
- Planned hub stops accept a managed Python child terminated by SIGTERM after the Rust front signals it, preventing false systemd failures on routine restarts.
- Correct the fleet hub runbook retrieval configuration and status description.
- Preserve operator Compose overrides during Fleet Hub setup and mode switches. Hybrid creates an operator-owned empty stub for later edits and refuses custom file selections before writes. Legacy migration requires an exact generated document. Rollback restores setup files and removes newly created stubs. S31 checks the effective Compose merge in both modes.
- The S6a dogfood bench probes now use their own credential (`--probe-token-env`) and a fresh claim holder per probe, release with the generation from the acquire reply, make the wake probe opt-in (`--wake-probe`), and report per-probe error counts in the summary.
- Allow bounded reflection to replay up to 5,000,000 events independently of retained replica bytes, and name the preseed recovery and its security tradeoff when the limit is reached.
- Restore the original Windows care task from its XML if replacement fails. If restoration fails, remove the placeholder and report both errors with the task name. Tolerate an absent task during fresh-install cleanup.
- Default Fleet Hub setup to BM25 (`lexical`) without embedder probes for 0.31.0 after the local GPU branch missed latency targets under host load. Hybrid requires `init --retrieval hybrid` or `switch --to hybrid`. Explicit hybrid init refuses without writes when no embedder passes its probe (#2507).
- Prevent concurrent commands from launching duplicate update checks by claiming each refresh before spawning it. Skip automatic refreshes when the cache cannot be written, and retry abandoned claims after one hour.
- `brigade run cloud land` now lints the generated PR body with an explicit Vale config. It is the repository's `.vale.ini` and styles read from the fetched default branch (never from the proposal). Without a base config it is the operator's own `$HOME/.vale.ini` when that is a plain file they own, and otherwise a minimal config that lints nothing. Only a strict subset of INI is accepted. Anything else, plus unsafe `StylesPath` values, include or import keys, duplicate keys, inline comments, symlinks, gitlinks and oversized style trees (over 64 MiB or 2000 files), is refused as `unsafe-vale-config`. A retry reuses the recorded base without fetching again, and the operator's global Vale config still merges. Style files are copied as they are, so rule fields that name files are trusted because they come from the base commit.
- Document the credential helper forms that `brigade run cloud land` accepts and how to fix unsafe custom helper paths.
- Pull requests record changelog entries as `changelog.d/<pr-number-or-slug>.md` fragments instead of editing `CHANGELOG.md`, so sibling PRs no longer conflict on the shared `Unreleased` hunk. `scripts/changelog_fragments.py` checks fragments, guards against `CHANGELOG.md` edits outside a release commit, and assembles fragments into `Unreleased` during the release cut.
- Pin every action in `publish.yml` and `hub.yml` to a full commit SHA, with the version tag in a trailing comment, and add a test that fails on any new tag reference in those two files (#2399).
- Every `handoff_unavailable` answer from the Dot and Duet connectors' `handoff_write` now carries a `reason`: `inbox_untrusted`, `inbox_missing`, `lint_state_unavailable`, `lint_unavailable` or `inbox_write_failed`. An untrusted or missing inbox is returned as a result with its reason. `inbox_untrusted` adds a `hint`: remove group and world write from the inbox and its parent folders, which a umask of 0002 commonly leaves on. The trust rule is unchanged, so a group-writable inbox is still refused.
- The Fleet Hub dogfood setup (`ops/fleet-hub/bin/hub-setup.py`) now also writes `secrets/operator-admin-token.txt`,
  a mode-0600 copy of the admin token that `brigade memory rescan` and `brigade memory reindex` accept as
  `--admin-token-file`. The container's 0444 copy is unchanged. Rerunning `init` adds the copy to an existing
  install, rewrites it after the admin token changes and resets a drifted mode.
- The default work mode is now called engineer mode: `brigade work mode set engineer|yolo`, and `brigade init --engineer` switches a yolo install back (it cannot be combined with `--yolo`). Engineer mode behaves exactly as the old `enforced` mode did, with verification on. `enforced` is still read from existing configs and accepted by `brigade work mode set`, so no config needs editing. JSON `mode` fields (`brigade work mode show --json`, `brigade work hooks status`, `brigade repos adoption --json` and the work brief) now report `engineer` where they reported `enforced`, so a script that tests for `enforced` should test for `engineer`. Hook wiring states such as the `enforced-idle` adoption state keep their names.
- On Windows, `brigade run cloud launch --provider claude-cloud` and every other command that
  resolves the user data, cache or config directory no longer starts a `ver` child process when
  the system's WMI query fails. Brigade now detects Windows from the interpreter.
- A Compose install of the Fleet Hub now reaches hybrid retrieval when setup picks it. `brigade fleet serve --engine rust` takes `--memory-config PATH` and passes it to `brigade-hub`, and `ops/fleet-hub/compose.yaml` names the `config/hub.toml` that `bin/hub-setup.py` writes. The hub logs one line at startup naming the memory configuration file it read and the retrieval mode. An image built from a `BRIGADE_REF` older than the flag keeps starting: `hub/entry.py` drops the flag with a warning and that hub runs in BM25 mode.
- The hub container's entrypoint copies the mounted embedder token to `/run/brigade/embed_token`, owned by the hub's user with mode 0400, on a tmpfs private to the container, and refuses a symlink at the mount. The hub accepts an embedder token only from such a file, and a bind mount keeps the operator's ownership. Setup writes that path into `hub.toml`, writes both embedder token files with mode 0444 inside the 0700 `secrets/` directory so the container can read them, and resets a local token an earlier setup wrote with mode 0400. Setup creates `secrets/` with mode 0700 under any umask and never changes the mode of an existing one. When group or other users can enter an existing `secrets/`, setup writes nothing and says to tighten the directory to 0700 and rotate the admin token, the findings key and the embedder tokens those users could have read. A mounted token the entrypoint cannot copy, such as an operator-owned mode 0400 file that the container's UID 10001 cannot read, stops the hub only when `config/hub.toml` selects hybrid retrieval and its embedder reads that copy. With a lexical configuration, or an embedder that reads another token file, the hub starts, removes any earlier copy and logs a warning that says to rerun `bin/hub-setup.py`.
- For the `api/embed` request shape, a reply that carries no model digest is now checked against the server's model listing at `/api/tags` on the same origin. The hub compares the listed digest with the pinned `model_digest` and refuses replies with `digest-mismatch` once a reading shows another digest or cannot vouch for the model. A document batch and the breaker's probe read the listing before the request and again after the reply, and the vectors are kept only when both readings report the pinned digest. Each reading has its own time limit, one fifth of the call's deadline and at most 2 s, so a listing route that stalls refuses such a reply with `embedder-timeout` without using up the deadline. An endpoint whose replies carry the digest is read once at most and then not at all, and a stalled listing does not stop it from embedding. A query takes one reading and reuses it for up to 30 s. A reply that carries a digest is decided by that digest alone. Before this, the hub refused every embedding from a local server that sends no digest.
- Dot and Duet `memory_search`, `memory_card` and `memory_hits` go to the memory hub set by `[memory_hub]` in `~/.brigade/fleet.toml` again, with that table's read credential. Since S23 they had gone to `[fleet] hub_url`, which serves no memory, so hub search found nothing and every hub card read answered `card_not_found`. Connector searches still count as reads and still land in the search log.
- A Dot or Duet host with hub memory on and no `[memory_hub]` now answers `memory_search` from local cards with `retrieval.reason: "no-memory-hub"`, and sends nothing to the fleet hub. The `BRIGADE_MEMORY_HUB_*` pair meant for the sync and mirror units no longer moves Dot or Duet to another hub or credential, and a relative `BRIGADE_HOME` gives them no memory hub instead of reading a `fleet.toml` under the working directory.
- A relative `BRIGADE_HOME` no longer lets the working directory choose the fleet hub or credentials. Before, `BRIGADE_HOME=.brigade` read `.brigade/fleet.toml` from whatever repository a command ran in, so that repository could pick `hub_url` and the node and admin token files sent to it. Now strict fleet commands, message principals, `brigade fleet keys`, `fleet ci` and `fleet projects sync` stop with `BRIGADE_HOME must be an absolute path`. Best-effort reporting reads no `fleet.toml`, keeps only the `BRIGADE_FLEET_*` environment values and logs one warning. The spool, node identity, model admission cache and Worklore receipts also refuse a relative home. An absolute `BRIGADE_HOME` and the default `~/.brigade` work as before.
- A relative `token_file` or `node_token_file` in `~/.brigade/fleet.toml` is now read from the directory that holds that `fleet.toml`, not from the directory a command runs in, so a repository can no longer supply the token. `~` still expands. On Windows a drive-relative (`C:node.token`) or root-relative (`\node.token`) token path is ignored.
- Memory sync and mirror no longer read `[memory_hub]` from a `fleet.toml` chosen by a relative `BRIGADE_HOME`, so a repository can no longer pick the memory hub and the token sent to it.
- Under a relative `BRIGADE_HOME`, `brigade run` takes no fleet claim, even with a hub set through `BRIGADE_FLEET_*`. It logs `fleet claim skipped for run directory ... (BRIGADE_HOME is relative)` and relies on the local run lock, as `--no-fleet-claim` does. `--require-fleet-claim` stops with the error instead, and Grok Bot leases are refused. `--no-fleet-claim` skips the claim key entirely. Model admission does not use its roster cache under a relative home. `brigade node --machine` and `brigade fleet claims --release --path` print the error instead of a traceback.
- Under a relative `BRIGADE_HOME` no claim request leaves at all, including an acquire, renew, release or inspect that reuses a stored key or names a node. The claim answers `relative-brigade-home`. A Grok Bot lease whose key was stored earlier is no longer acquired, renewed, released or reported against the hub.
- `brigade fleet serve --engine rust` no longer exits with `schema-changed` on a hub that Python upgraded across several releases. The schema gate now ignores whitespace that SQLite's ADD COLUMN leaves before a comma, and it accepts the `model_policy` column order produced by upgrading through v14, v15 and v21. Schemas with a missing column or a changed type are still refused.
- `ops/fleet-hub/bin/hub-setup.py` now writes the `[memory.embedder]` table the Rust hub parses for both hybrid paths: the full request URL and its allowlist entry, `token_file`, `shape`, `name`, `model_digest`, `dimensions`, `input_chars`, `query_prefix` and `query_prefix_version`. The per-model values come from `ops/fleet-hub/r1-models.json`, now schema 2, which also records that `qwen3-embedding:8b` passed the public-fixture half of R1. Before this, the hub refused every hybrid configuration setup wrote.
- The local GPU path gets its own embedder token file, `secrets/embed-local-token`, because the hub needs a token file for every endpoint. Both embedder token files are mode 0444 inside the 0700 `secrets/` directory, like the other mounted secrets. A probe that passes with a different remote token now replaces `secrets/embed-token`, and leaving the remote backend removes it. Setup removes the old token before it writes the configuration and installs the new one last, so `secrets/embed-token` never sits beside a configuration naming another endpoint, even if setup dies partway. If any step fails, setup restores every generated file. A rollback that itself fails leaves no remote token, so the hub answers in BM25 mode until setup is rerun. If the rollback cannot remove the token this run installed, it keeps this run's files in place, so that token stays beside its own endpoint. Probe failures no longer print the HTTP library's message, which could quote the token, and a token file whose token holds whitespace or control characters is refused before any request. The remote probe now reads the model digest where the hub reads it, the `model_digest` field or the `x-model-digest` header, and refuses an endpoint that reports two different digests.
- The S31 `setup` check now starts `brigade-hub` with `--memory-config` on the configuration after `init` and after each switch direction, and requires `/ready` and the expected retrieval mode, with `degraded` for a hybrid embedder the host cannot reach. It fails when no hub binary is available. Pass `--hub-binary` and `--hub-python` to choose them.
- Add `brigade-hub memory-restore-epoch --memory-db <file>`, which starts a new memory `log_epoch` on a `fleet-hub-memory.db` restored from a copy, before the hub serves it. Clients then rebuild their replicas on the next pull, and a write based on a revision from before the restore is refused with `409 memory_epoch_changed` instead of overwriting a reissued revision. The command refuses, changing nothing, for a symlinked path, while a process holds the file, when a stale `-wal` or `-journal` sits beside it, for a file that is not a memory file or has a newer schema, and when SQLite's `quick_check` fails. The dogfood runbook's restore steps now cover the memory file.
- Hub push search now ranks and counts over push-eligible cards only. Corpus
  size, average card length and term frequency leave out private, pending,
  flagged and other writers' unreviewed cards, so adding or retiring a withheld
  card no longer moves the scores, order or `match_count` a connector sees.
- A push `card` read for an id with no card now does the same store lookup as
  one whose copies are all withheld, so the time of a 404 does not tell them
  apart.
- On Windows, message, Dot, Duet and memory hub node token files are now opened without following a symlink or reparse point swapped in after the path check. A link at the token path is refused instead of read.
- Give the hub manifest binding churn test a large writer deadline so a slow CI runner no longer answers 503 on `propose` before the retained budget refusal is reached (#2411).
- Raise the recall time budget in the dispatch memory 2,000-card bounds test so a slow CI runner no longer answers with no packet before the bounds are checked. The budget keeps its own tests.
- Test only: the abandoned recall worker cap test holds every worker in a stub search, so a thread that starts after its 0.1 s deadline on a loaded runner no longer frees its slot early and changes the worker count.
- Test only: the abandoned recall worker cap test waits for the last worker to reach the hub before counting searches, so a slow thread start on a loaded runner no longer reads 3 of 4.
- Remove the single-operator `wazuh-triage`, `n8n-operator`, and `backup-steward` Grok Bot packs. No replacement. Before upgrading, stop each installed unit and uninstall it with `brigade run cloud grokbot pack remove --id <pack-id> --target <target> --out <unit-directory> --apply`. The remove command deletes owned config and unit files but does not stop services.
- Brigade now requires Python 3.11 or newer. Python 3.10 users should upgrade the interpreter before upgrading Brigade.
- Remove `brigade runs diff` and unused internal modules. No replacement for run comparison or the deleted internal APIs.
- Remove `brigade operator plan`, `operator init`, and `operator bootstrap-portable`. Use `brigade operator quickstart` (with `--dry-run` for planning) and `operator checkup`.
- Remove `brigade work task claim`. Use `brigade work claim`.
- Remove `brigade work task show`, `work task annotate`, `work task edge add`, `work task edge list`, and `work reassign`. No replacement for these leaves. Dependency creation through `work task add --deps` and readiness through `work ready` remain available.
- Remove the deprecated `brigade run-cloud` alias. Use `brigade run cloud`.
- Remove `brigade openclaw-fragments` and `brigade hermes-fragments`. Use `brigade harness fragments --harness openclaw` or `--harness hermes`.
- Allow unused command paths to be removed at the next release without an alias. Paths with callers retain a deprecated alias until no later than the next major release.
- Remove unused internal APIs in `aboyeur_artifacts`, `claude_actions_diagnostics`, `claude_actions_http`, `claude_actions_import`, `claude_actions_metadata`, `claude_review_history`, `claude_cloud`, `evidence_ack_acceptance`, `evidence_ack_protocol`, `evidence_ack_crypto`, `grokbot_fleet.app`, and the `memory_doctor` module CLI. No replacement. Move the projection conformance fixture into the test suite.
- `brigade handoff draft` now runs the ingester's own checks before it writes and refuses a draft that would conflict at ingest, with one `draft refused, nothing written` line per problem and no file left behind. For `update-card` it reads the target card in `--workspace` (default `--target`) and fills a new `## Expected card SHA256` section. When the workspace is unreachable it names the flag to pass, `--workspace` or `--expected-sha`. `brigade handoff lint` shares the same rules through the new `handoff_ingest_rules` module, takes `--workspace`, and now fails an `update-card` without a valid SHA, a card without `title` or `topic`, `category` or `tags` frontmatter, an unsafe card name, a target document outside the ingester's allow-list, and a bootstrap append over budget. With a reachable workspace it also fails a stale SHA and a `create-card` that collides with an existing card. `brigade ingest` uses the same name, document and budget checks, accepts the new section, and sends an `update-card` whose SHA no longer matches the live card to review. The handoff templates, the handoff-flow card and the handoff docs now tell writers to use `brigade handoff draft`.
- Ingest now rechecks an `update-card` handoff's expected SHA against the card bytes under the publication lock and routes a stale one to review, so a card edited after the decision is never overwritten. A routed document append refuses any symlink in the destination path, and writes through a held directory descriptor with no-follow. Frontmatter lists written with unindented entries (`tags:` then `- memory`) are accepted by lint and draft, and a repeated frontmatter key is reported without echoing it.
- Publication now reads the destination card once through a no-follow descriptor and uses those exact bytes for the expected-SHA check and for the publish-boundary revision probes, so draft, lint and ingest all hash raw on-disk bytes and a CRLF card behaves the same on every platform. A routed document append fails closed to review when no descriptor-relative no-follow backend is available, and uses the Windows handle-relative backend when it is. Ingest refusal reasons no longer repeat handoff text, such as the duplicate anchor line, section names, card names, actions or injection excerpts. Trailing YAML comments outside quotes are ignored when lint and draft read card frontmatter.
- Card publication now opens the destination's parent directory once with no-follow, then reads the card, stages the replacement and renames it over the card relative to that held handle, on POSIX and through the Windows handle-relative backend. Just before the rename it reopens the parent path and compares directory identities (`st_dev` and `st_ino`, which Windows reports as volume serial and file index), so a directory renamed away after it was held sends the handoff to review on every platform, and it checks that the card is still the revision it read. When no descriptor-relative backend exists the handoff goes to review and nothing is written, so the earlier path-based fallback is gone. Staged cards are written as exact bytes. Ingest, lint and draft refusal reasons now use fixed wording that leaves out fence languages, card names and `rules/<name>.md` targets. Lint, draft and ingest now read card frontmatter with one parser, the line-based reader ingest already uses for cards, with the card policy applied on top of what it returns: a `title` or `topic`, a `category`, a `tags` key and ISO dates (date-only or a timestamp). Multiline quoted values and multiline flow lists are accepted by all three, and a card that parser cannot read is refused by all three with fixed wording. Because that reader keeps a flow list as text, lint checks that `tags` is present, not that it is a list. `brigade ingest` now sends a card whose frontmatter is missing or never closed, or whose body is empty, to review, and it does not apply the title, category, tags and date policy.
- Opening a card for publication, reading a card with `read_text_nofollow`, and appending to a routed document now use a non-blocking no-follow open on POSIX, check for a regular file, and then restore blocking mode. A card or document swapped for a FIFO goes to review instead of hanging ingest.
- `brigade ingest` records an unreadable, unarchivable, non-UTF-8 or structurally malformed handoff as a failure, leaves it in place and ingests the rest of the batch. The report adds a `Failed` count, a `PARTIAL` line and `Status partial`, and `brigade repos ingest` carries the failed count and status. `PARTIAL` joins the default ingestor warning patterns. Guard and invariant failures still stop the run.
- `brigade ingest` action lines and the `reason:` line of review drafts now carry a stable reason code (for example `[unsafe-card-name]`, `[near-match-proposal]  match=memory/cards/x.md similarity=0.91`) and a safely encoded handoff filename. They no longer quote target cards, target documents, handoff text, scan excerpts or exception text. Review draft filenames replace characters outside `A-Za-z0-9._-` with `_`, and a draft name that is already taken gets a numbered suffix instead of being overwritten.
- `brigade ingest` also reads the handoff inboxes of linked git worktrees of each watched root. Worktrees come from `git worktree list` (output capped at 1 MiB and 256 records) and must share the root's common git dir. Each inbox component is opened without following links. A watched root below its checkout's top level maps to the same subdirectory in each linked checkout. A handoff whose content was already published, appended or archived in the same run is archived as a duplicate, so one handoff copied into several worktrees ingests once. A handoff skipped for a retry (for example a busy card publication lock) does not count. Each handoff is read once, and the digest, parse and every delivered copy come from those bytes. A handoff whose file changed before it was archived stays pending and is reported as `handoff-changed`.
- Removing a worktree through Brigade (run cleanup, `brigade runs prune-worktrees`, model trials) first moves its pending handoffs into the main checkout's inbox, deduplicated by sha256. Rescue finds inboxes from `git ls-files --others -z` with one `:(glob)**/<inbox>/**` pathspec per writer inbox name (untracked files, ignored ones included, at any depth, capped at 8 MiB and 10 seconds, so a large ignored tree such as `node_modules` does not count against the budget) and delivers each to the same subdirectory of the main checkout. That subdirectory must already be a real directory reached without following links, and the destination stays open by descriptor from validation to the last write. The listing runs with the verified worktree git dir and work tree pinned, and with injected git config, location and pathspec environment variables removed. Inbox names match regardless of case, and a worktree whose `.git` marker is not a genuine, unlinked marker for that worktree is kept. A worktree holding an embedded repository or a populated submodule path (with or without its own `.git`) is always kept and reported as `embedded-repository`, because git does not list files inside it. Handoff filenames follow the same rule as ingest, so dot-prefixed handoffs such as `.pending.md` are rescued too. The destination must be the verified main checkout of the same repository. Only a handoff unmodified for 5 seconds is rescued. A newer one stays pending and keeps the worktree. Each source is claimed with an atomic no-replace rename to a private name, then copied under a temp name the ingesters ignore, fsynced, verified and published without replacing an existing file. Only content already archived in the destination's `processed/` counts as delivered. A matching pending copy can still change before ingest reads it, so rescue publishes its own copy and ingest's in-run dedupe handles the duplicate, and a rescued handoff is never published under a name the destination's git index tracks, even when that file was deleted. Unknown destination tracking fails the rescue. The claim is removed only after a bounded final check (size snapshot, hash and mtime, within the ingest cap and 10 seconds) still matches the delivered copy. If it changed, kept growing or the check ran out of time, the claim is renamed back to a pending handoff in the source inbox (or to a name ingest skips when it is over the ingest cap) and the worktree is kept. A handoff that appears during the rescue, a claim left by an interrupted one, or an oversize recovery file from an earlier pass also keeps the worktree, including under `force=True`, and the warning names the file. Warnings print the reason code first and shorten only the middle of a long path, never the file name. If any handoff cannot be delivered, or git tracking cannot be established, the worktree is kept and a warning names it.
- In a linked worktree, the Stop hook's handoff note names the main checkout's `.claude/memory-handoffs/`, which survives the worktree's deletion. The note text is otherwise unchanged.
- The verify-brigade `hub-contract` drive now checks message and question expiry, the
  sealed-content lifecycle and the message `client_id` window on a second hub whose clock
  it steps (`--clock-hub-cmd`), sweeps 60 more refusal paths, and compares the exact text
  of every refusal and the sealed tombstones with a golden captured from the Python hub
  (`--write-golden`).
- `brigade fleet serve --engine rust` runs the Rust hub, `brigade-hub`, in front of the Python hub on the
  same database. It migrates the file first, refuses a binary whose SHA-256 does not match its recorded
  checksum, forwards stop signals, and exits non-zero after 3 failed `/health` checks so the service
  manager restarts it. `--route-mode` sets `brigade-hub` route modes. Dropping `--engine rust` rolls back.
- `brigade setup --component brigade-hub` installs the Rust hub on a Linux hub host. Releases publish it
  for `linux-amd64` and `linux-arm64` after pre-publication acceptance on each target, a plain
  `brigade setup` never downloads it, and a host that installed it keeps it on later setups and updates.
- With a native route family, `brigade-hub` restarts a Python upstream that crashed, after 1 s and then
  doubling to 30 s, while native routes keep serving. Proxied routes answer `502 upstream-down` and
  `/ready` answers 503 until it is back. Native long polls answer at once when the hub starts its 25 s
  shutdown drain.
- The dogfood Compose recipe runs `fleet serve --engine rust` when its pinned Brigade has the engine,
  with the watchdog also exiting when the hub's network namespace is left with only loopback, and
  keeps its S31 wrapper for an older pin.
- `fleet serve --engine rust` runs the exact file it checked: it opens the binary once, refuses a symlink,
  a file owned by another user or one its group or others can write, and executes that descriptor.
  `--port 0` works, since the watchdog uses the address `brigade-hub` reports binding.
- Claude Code recall hooks read hub memory when the workspace's memory config is in `read` or
  `sync` mode. Session-start and prompt recall fuse hub hits with local cards inside the existing
  5 s recall child. With the hub stopped or hung they answer from local cards and the replica
  without a warning line. Hub hits are named `hub:<scope>/<card_id>`, and only `reviewed`,
  `policy-promoted` and `imported` hub versions are shown, so another principal's unreviewed card
  in a shared scope never appears unasked.
- Every recall line a hook injects, local cards included, now goes through the memory prompt-load
  scan, each rendered field on its own and then the whole line. A blocking or warning finding
  shows only `- <card_id> | blocked: <rule id>`. Private-class text is withheld with
  `code: memory_content_private` unless the operator opted the host in with
  `brigade work hooks install --recall-audience local` (or `update`), which writes a marker bound to
  the host name, the machine id (required except on macOS) and the Brigade home's inode. On Windows
  the Brigade home must sit inside the user's profile. Nothing
  writes it by default. `--recall-audience connector` turns it off even when the file cannot be
  removed, and exits 2 with the marker path if local recall may still be on. `CLAUDE_CODE_REMOTE` or a `BRIGADE_RECALL_AUDIENCE`
  value other than `local` always means connector. `brigade doctor` warns "local recall not
  enabled" with the command when a recall target is active. A scanner that cannot run injects no
  recall.
- Recall skips cards the SessionStart work brief already named, so a card is not shown twice. Ids
  pasted into a prompt never hide a card.
- The Stop hook sends hub memory a `recall.capture` event with a hashed session reference and the
  card keys recall showed and the session opened with the Read tool (any card search lists, at any
  depth under a card root), under the id card search gives it. It holds ids only. A batch the hub does not take stays pending and is retried with
  the same event id at the next Stop.
- At session start the work brief runs first under its own deadline, and memory recall gets only
  the hook time the brief left. On a slow host recall is skipped instead of costing the brief its
  injection.
- Replace the always-write Memory Handoff rule with a judged one in the brief sections, installed instruction templates, `brigade-work` skill, Cursor rule and Stop hook note: write a handoff only for knowledge the repo, git history or an existing card does not hold, and otherwise end with `Nothing durable: <reason>`. The Claude Stop hook skips its handoff note when the final assistant line starts with `Nothing durable:`, and ignores that text in user prompts and tool output.
- Hub memory has verifier principals. An admin sets or clears a principal's `verifier` flag
  with a new memory action, and every credential carries a copy: native enrollment binds it
  before issuing, refuses an unflagged credential for a verifier principal with
  `memory_verifier_credential_unflagged`, and a request whose copy differs from the principal
  is refused with `403 memory_verifier_flag_mismatch`. Revocation and removal keep the flag.
  Memory schema moves to 11.
- Every proposal from a verifier principal is Tier 2 at submit, preflight and every decision.
  Preflight reports a `verifier_floor` hold and `auto_apply` refuses the proposal. A fleet
  policy that lists a verifier in `memory.reflect_principals` is refused with
  `memory_policy_verifier_listed`, and so is flagging a listed principal. A flag change that
  cannot publish the verifier list for the policy writer is rolled back and refused. The list
  is written durably and carries the memory file's generation, which must equal the one the hub
  records before each flag change, so a policy write refuses a stale list or one left by
  restoring either database, and hub memory does not start until it can republish it. Memory
  `status` reports both generations, and the admin `verifier_resync` action repairs a mismatch
  after a restore. Memory `status`
  reports the caller's `verifier` and `reflect_principal` booleans and a `verifier_floor`
  capability.
- Verifier attachments are now bound to the proposal they support. A verifier's record must
  be a `memory-verify-manifest` for the attested card, or attach refuses it with
  `memory_manifest_card_mismatch`. The record starts in `pending_review`, never holds
  automatic apply and never counts as proof. `propose` binds it only to the attaching
  principal's proposal for the exact scope, card and base revision the manifest checked, and
  otherwise refuses the whole proposal with `422 memory_manifest_binding_invalid`. Accepting the
  proposal makes the record `active`. Rejection, staleness, compaction and the 24 hour unbound
  sweep make it `inert`, which frees its quota slot. Memory schema moves to 12.
- Verifier records that are pending or inert count against a retained-storage budget of
  16 MiB and 1,024 records per principal and 64 MiB and 4,096 records per scope, refused with
  `429 memory_evidence_retained_principal_quota` or `memory_evidence_retained_scope_quota`.
  The idle sweep purges a record 35 days after it went inert through the `evidence.purge`
  tombstone, keeping its hash and logging the purge.
- `proposal_show` returns a `verifier_manifests` list of bound manifest hashes to callers with
  `review` on every target and every bound record's scope, and omits the key for everyone
  else. Memory `status` advertises a `verifier_manifests` capability.
- `proposal_show` no longer panics on a compacted proposal. Compaction drops a proposal's pins,
  and the reply now lists them as empty instead of failing the read.
- Memory verification now has its prompt contract and reply checks: versioned
  instructions with per-call nonce markers, a strict `brigade.memory-verify.v1`
  reply validator, the deterministic evidence, novelty and supersede checks,
  and op-level agreement between the propose and confirm seats. Nothing calls
  it yet. The run command arrives in a later slice.
- Memory verification can now check an abbreviated commit id against git. The new resolver fetches each allowed
  remote-tracking ref over https in the same run, and expands an abbreviation only when exactly one object
  matches, that object is a commit, and an allowed ref can reach it. It refuses repositories whose local config
  holds includes, hooks, helpers, aliases, URL rewrites, proxies or any `http.*` setting, and it refuses shallow
  clones, partial clones and grafts. The minimum abbreviation drops from 12 to 7 hex characters.
- The resolver runs only a root-owned git from `/usr/bin`, `/bin` or `/usr/local/bin`, executed from a held
  descriptor, with an environment built from scratch. git's https helpers must pass the same ownership, mode and
  native-binary checks before any fetch, and reachability is checked against the commit ids the fetch proved. Commit expansion is POSIX-only for now: on Windows every
  abbreviation abstains with `verify_resolver_unsupported_platform`.
- `[[verify.workspace]]` accepts an optional `commit_refs` list of fully qualified `refs/remotes/<remote>/<branch>`
  refs. Without it, the resolver uses the single ref that `refs/remotes/origin/HEAD` names.
- `memory verify run` now expands abbreviated commit ids through the hardened git resolver instead of
  abstaining on every one. Each non-dry run opens one resolver run that all its cards share, so they share one
  fetch per repository, and closes it when the run ends, even after a failure or an early return.
- A dry run runs no git, because the resolver's fetch writes remote-tracking refs. Its commit expansions abstain
  with `verify_commit_unresolved`. On a platform where the resolver cannot run, a non-dry run reports
  `verify_resolver_unsupported_platform` once under `skipped`.
- `brigade memory verify run` now runs the verification pass: it checks the
  hub's verifier floor and principal flags before any model call, calls both
  seats, attaches a manifest and opens one pinned proposal per corrected card,
  and keeps an owner-only review packet for each proposal for 14 days after it
  settles. Commit-id corrections stay off until the git resolver lands.
- `brigade memory care install --entry memory-verify` schedules the pass at
  11:00 and refuses the reflect job's folder and fleet credentials.
- A rate-limited verify scope is now scored from the last complete read count
  snapshot when it is at most two days old.
- Verify submissions are staged on disk before they are sent. A crash after a
  proposal opens no longer loses its review packet, and a submission that met
  a transient error is replayed with the same request id by the next run
  instead of waiting out the card's cooldown. A run that only skipped no
  longer clears the pause counter.
- Verify stages expire 7 days after creation and are never replayed when their
  ops no longer match the ledger or their scope is no longer bound. A
  transient attach error is retried instead of treated as final, and a
  duplicate pending reply publishes a packet only when the hub lists its
  manifest as bound to that proposal.
- An expired verify stage keeps its request id as a tombstone for 90 days, so
  identical ops are resent under the same id instead of a new one. With 512
  live tombstones the pass opens no new proposal until one ages out.
- `[verify] enabled = false` now stops every verify run that would call a seat,
  scheduled runs included, with `verify_disabled` before any hub request.
- A verify card whose earlier request may have opened an unnamed proposal is
  held until that request's tombstone ages out, so it never gets a second
  proposal. Corrections deferred by the per-run cap, the pending ceiling or a
  quota stop are checked again next run instead of waiting out a cooldown.
  `memory verify doctor` probes no seat while `[verify] enabled = false`.
- A verify replay whose manifest is 23 hours old or more, or that the hub
  refuses as unbindable, is checked afresh with a new manifest instead of being
  cached as a final refusal. A full packet directory defers the card without
  a cooldown.
- Only a verify request that may have opened a proposal keeps a tombstone and
  its card's hold. A request the hub refused or quota-stopped is forgotten
  when its stage is discarded, so the card is checked again.
- Verify saves a tombstone before deleting its stage, saves a proposal id
  together with whether this request opened it, and holds the card of any
  unsettled request found without a stage, so a crash between steps cannot
  reopen a card or publish an unbound packet.
- `brigade memory verify status` and `brigade memory verify run --dry-run --no-model` score and
  select hub cards for evidence-checked verification and print only card ids, scores, lanes
  and skip counts. A quarter of each run's slots, rounded up, goes to the cards with the
  oldest verification. A never-read count stays null until read counting covers its window.
  `[verify]` in `.brigade/memory-care.toml` binds each scope to one workspace root and an
  explicit archive project list. Roots must exist and are compared by directory identity, so
  roots bound to two scopes and nested roots are refused even through case-only spellings or
  symlinks, and so are roots without a stable inode and hub URL or credential keys. A scope
  that `read_stats` rate limits is skipped for the run, and cached counts are never used in
  place of a reply. Card history is bound to the hub, principal and log epoch and ignored when
  any of them changes, and the per-scope card bound applies to bound scopes only. Verify keeps
  its own replica under `.brigade/memory/verify/hub/` and never touches the shared hub replica. The verify state file has an 8 MiB limit and fails closed with a
  fixed reason. The reflect lanes and verification share one fairness order helper.
- Memory verification now assembles each card's evidence per scope. Every bound workspace root
  is opened once as a held directory handle and checked against its pinned identity, and cited
  paths resolve under that handle one no-follow component at a time, so a root swapped, linked
  or recreated after the check is never read. Paths under no root, under two roots, under
  another scope's root or outside the evidence directories, bundles from an unlisted or shared
  archive project, and attested evidence ids are `evidence_unreachable`. Only verify receipts
  whose bytes match the evidence archive's recorded digest rank as authenticated, and only for
  the fields Brigade writes. Their command output is a separate medium item. Every item is cut
  on a UTF-8 boundary and scanned, and flagged items are dropped with their rule ids. A card
  without an authenticated receipt and a second independent item gets no model call.
- `brigade memory verify doctor` resolves `propose_seat` and `confirm_seat` from the local
  roster and probes each one. Seats whose prompts would reach a cloud provider, hosted chat
  session or other connector audience are refused with `verify_seat_connector` before any
  prompt is written, and so are seats only the hub knows about. Only `claude` seats have an
  isolation profile in v1. `ollama:` seats are refused because Ollama tags are mutable and no
  model can be launched by digest. Script launchers, seats that pin no exact model, and two
  seats on one model are refused too.
- Every Claude verify call runs with tools, hooks, settings, plugins, MCP servers and slash
  commands switched off, and is refused unless the CLI's own init event confirms it. The seat
  runs in an empty private directory under a temporary root with no instruction files above
  it. Its executable is hashed and checked again before every call. A passing probe is
  recorded per fingerprint, so a changed seat or an upgraded CLI must be probed again. The
  verified executable is launched from its open descriptor, so a binary swapped after the
  check never runs.
- Verify prompts and replies live only in owner-only work areas that are removed after every
  call, including after a timeout or interrupt. A failed removal is reported and blocks new
  calls until the leftover can be removed.
- Count per-card daily search hits and first-chunk card reads on the hub, without storing a principal, query or time of day. Counts are batched in memory and flushed by the memory writer only while no write is queued, with per-principal limits, flush and sweep budgets, table row caps and 35-day retention under `memory.read_counts`. Upgrade hub memory storage to schema 10. No read action returns the counts yet (#2387).
- Hub memory has a `read_stats` action that returns per-card read aggregates for one scope.
  It needs one grant on that exact scope holding both `read` and a new `read_stats` right
  (wildcards, node defaults and `reflect` do not open it), pages at most 1,000 rows through
  a 15-minute cursor bound to the caller and scope, and allows one request per second and
  600 per day per principal. Each admitted request gets an audit row in the hub audit log,
  refusals are merged into one summary row per principal per hour, and the hub writes at
  most 20,000 such rows a day. `memory_hub_client.read_stats` reads a page and refuses the
  admin credential.
- The scheduled hub reflection docs now say that a scan metadata refresh against a hub that
  predates scan metadata fails the run and pauses the job after three failures, and that stored
  non-retryable refusals such as `memory_content_blocked` do not expire when hub policy or the
  scan ruleset is relaxed, with the ways to clear them.
- The code graph engine takes its identity from the name it was invoked as. `brigade-code-graph --version` now prints `brigade-code-graph 0.4.0` and the canonical MCP server reports `serverInfo.name` `brigade-code-graph`. The legacy entry points keep their old banner, server name and stderr output. Their deprecation line arrives with the release that installs the canonical executables.
- New repositories get their code graph index at `.brigade/code-graph/code-graph.db`, with a matching `.gitignore` entry. A repository indexed by an older release keeps using its existing index in place, with no reindex and no copy. When both exist, the new location wins.
- The engine reads `BRIGADE_CODE_GRAPH_DB` and `BRIGADE_CODE_GRAPH_ALLOW_UNSAFE_ROOT` first. The older variable names still work with a stderr warning, and setting old and new names to different values is refused.
- `brigade code export --format dot` names the graph `code_graph`.
- Brigade features that read the code graph index (context packs, run briefs, work footprints and partitions, verify ranking, `brigade code export`, the unseen-dependents hook, search and operator status, and `brigade doctor`) resolve it with the same rule as the engine, so they find an index in either location and honor `BRIGADE_CODE_GRAPH_DB`. Conflicting database variables are reported instead of guessed.
- The evidence engine now takes its identity from the name it was invoked as.
  `brigade-evidence` prints `brigade-evidence 0.6.0`, names itself in usage text
  and reports `brigade-evidence` as its MCP `serverInfo.name`. The legacy entry
  point keeps its old banner, server name and stderr output. `doctor --mcp`
  accepts either server name.
- A fresh home now stores evidence in `<data>/brigade/evidence/evidence.db`,
  with config and cache under `brigade/evidence/` in their XDG roots. An
  existing archive in the legacy directory is still opened and written in
  place, together with its bundle MAC key, and no canonical directory is created
  beside it.
- `brigade evidence crawl memory` accepts either engine banner when it checks
  the engine version floor.
- Brigade looks up `brigade-code-graph`, `brigade-code-graph-mcp` and `brigade-evidence` before the older executable names. When one directory holds both names, the new name wins.
- `brigade doctor` reports engine names that are still in use. It shows a WARN with both paths and their modification times when both the old and the new code graph index, or both evidence directories, exist. An older engine environment variable name, or the old name of the code graph timeout config key, shows as INFO. Old and new variables set to different values show as WARN and are still refused.
- The older environment variable names no longer print a warning on stderr. The notice returns, together with the notice for the older executable names, in the release that installs the new executables. Until then, Windows PowerShell callers that treat any stderr output as an error keep working.
- New command and flag names: `brigade receipts export evidence`, `brigade work verify run --code-graph-timeout` and `brigade work import context --from-evidence`. The older spellings still work and are hidden from `--help`. `.brigade/config.json` now stores `code_graph_delta_timeout_seconds`. The old key is still read when the new one is absent.
- `brigade init` adds `.brigade/code-graph/` to the managed `.gitignore` block.
- Station ids are now `brigade-code-graph` and `brigade-evidence`, and the older ids still resolve. `brigade search status --json` and `brigade operator checkup` emit both the new and the old keys with equal values.
- New Cursor user installs use the MCP server keys `brigade-code-graph` and `brigade-evidence`. Keys that Brigade already wrote are kept and get a rename notice. `brigade harness install cursor --scope user --rename-legacy-servers` and `brigade mcp sync --rename-legacy-servers` rename the keys Brigade owns. Entries you wrote yourself are never renamed. A rename goes ahead only where the new key can be installed. If another entry already holds the new key, the old entry stays in place and keeps working, and the command reports the conflict. `brigade mcp sync --rename-legacy-servers --harness <name>` can rename one harness at a time, and later runs clean up the other harnesses. After a rename, update tool permission patterns to the new server names, for example `mcp__brigade-code-graph__*`.
- Memory verify seats no longer inherit a descriptor to their own executable. The held
  descriptor is closed on exec, so the seat process and anything it starts never see it.
- Dot and Duet `inbox_list` and `fleet_digest` push memory hits from hub memory when the
  workspace's `[hub]` mode is `read` or `sync`. The connector asks the hub as its own message
  principal, so only that principal's grants apply, and the runbook grants Dot and Duet read on
  `fleet`. Hits keep the one `memory_hits` object and add `scope`, `card_id`, a short `hash`,
  `trust`, `score` and `source: "hub"`, with the title, summary and tags wrapped as untrusted
  text. A local card is never pushed in these modes, and an unavailable or refusing hub leaves
  the field out.
- `fleet_digest` carries up to 5 hits and `inbox_list` up to 3, the wrapped card text stays
  within 300 characters, and the whole `memory_hits` object stays within 4 KiB, dropping
  trailing hits to fit.
- Hub memory `search` accepts `query_source: "push"`. It answers only reviewed,
  policy-promoted and imported cards, plus unreviewed cards to the credential that wrote them,
  after re-reading each head and its current clean scan receipt, so a retired, lowered or
  superseded card in a stale index snapshot is dropped. Private-class and flagged text never
  leaves on this path. `eval` and `ranking: "raw"` are refused with it.
- Dot and Duet `memory_card` read hub cards as the connector's own principal with the hub's new
  `card` `push: true` mode, never with the host node's credential or the replica. The hub serves
  only a live head with a current clean scan receipt and push trust, so a card id an agent is
  given or guesses opens no more than `memory_hits` could send.
- With hub memory on, Dot and Duet `memory_search` search the hub with `query_source: "connector"` as
  the connector principal, never with the host node credential, an admin token in its slot or the
  replica, so a search returns no card text `memory_hits` could not send. Local workspace cards
  come back apart as `local_matches`. The Grok Bot memory profile stays grant-scoped.
  `connector` applies the push filter and counts its matches as reads, as any explicit search
  does. The unsolicited `memory_hits` searches (`push`) are not counted. Explicit connector
  searches still append their search-log record, from the matches they show, so follow-up and
  redundancy metrics keep counting them.
- A push `card` read without a scope considers only push-eligible copies, so withheld copies
  never cause `memory_card_ambiguous` or appear in its scope list.
- Dispatch packets carry a bounded `memory_context` block: `brigade run` planner, worker and
  synthesis prompts, Grok Bot T3 adapter prompts, native T3 child tasks and
  `brigade fleet work handoff show`. Each block holds at most 5 card references (scope, card id,
  title, a summary of at most 300 characters, a short content hash, trust, score, source and how
  to read it) within 4 KiB, never a card body, and says memory text is data, not instructions. A
  hub card is read with `memory_card` or `brigade memory hub card`. A local card, readable only on
  the same host, never appears in a connector packet.
- Selection uses the `memory search` ranking and asks the hub as an unsolicited push, so dispatch
  counts no card reads. It keeps only reviewed, policy-promoted and imported hub cards, the node's
  own unreviewed cards included in the drop, re-reads each hub card's head and drops stale ones, and scans every field on its own and
  then the rendered block. A hub that is stopped, slow or refusing adds no block and never delays
  dispatch past a 2 s budget, and at most one recall runs at a time. Every packet is built for a
  connector audience, so private-class cards never appear in one.
- Grok Bot feed jobs can carry up to 5 hub card references (`memory_refs`: scope, card id and short
  hash), searched with the Grok Bot memory principal's own credential so they name only cards it
  may read. No references are computed until that credential has a user-level location. They ride
  in the private job envelope, returned with the claim's execution context. Hub job payloads are
  unchanged and hold no task text.
- Grok Bot feeds can attach `memory_refs` to new jobs. Name the Grok Bot memory principal's token
  file in the user-level `~/.brigade/fleet.toml` under `[grokbot_memory] token_file`. The file must
  be an absolute path to a private regular file owned by the feed user, not a symlink, at most
  4 KiB, and the fleet hub must confirm the token as `agent:grokbot-memory`. A workspace file never
  sets the path, and a relative `BRIGADE_HOME` turns references off. Windows is not supported.
  Without the key, or when anything is refused, references stay off, and
  `brigade run cloud grokbot doctor` prints a `memory-refs` line with the reason and never the token.
- `brigade fleet work handoff show` now builds its `memory_context` for the local audience on a
  host the operator opted in with `brigade work hooks install --recall-audience local`, so the
  operator's own terminal sees private-class card summaries there. A missing, unreadable or
  refused marker, or a session the environment marks as remote, keeps the connector audience.
  Every other dispatch packet (aboyeur briefs, native T3 children, cloud seats and Grok Bot
  jobs) stays connector.
- The verify-brigade hub contract drive has a `--profile prod-readonly` mode: an exact list of
  operations that a SQLite authorizer audit proves write nothing on the Python hub and on the
  Rust front in managed proxy mode, with a whole-database digest and no retention exception.
  A case outside the list stops the run before any request, and the Proxmox hub runbook names
  it as the only drive run allowed against production.
  An inotify watch on the database path fails the audit when anything opens the database
  outside the audit hooks, and the audit reports the front as not certified where the watch
  cannot run.
- Scanner imports distinguish missing files, read failures, and invalid UTF-8, preserving the cause of non-missing read failures.

## [0.30.0] - 2026-10-20

### Added

- A fleet ownership map. The policy document gains an optional `owners` section of charters that say what each agent or surface owns, must never touch, and how work reaches it. The Command Deck page `/deck/owners` shows each charter beside its live claims, Grok Bot jobs, sessions, and delegations, with an Unassigned list for work no charter matches. `GET /owners` and `brigade fleet owners show` return the same view as JSON. Charters describe responsibility and do not block any claim, dispatch, or merge.
- CI runs affected test files on Windows for pull requests, and keeps one nightly tracking issue open until the full Windows suite is clean.

- Stable npm publishing support for `@escoffier-labs/brigade@0.29.0` and five public platform companions, using the pinned official PyPI wheel, tag-based payload auditing, immutable archive retries, hosted provenance and native registry acceptance gates. Registry publication is tracked by the release workflow.
- Installation guides now cover the npm and npx routes, Node.js requirements, managed Python downloads, npm-managed upgrades, and known platform limits.
- The README downloads badge combines PyPI downloads without mirrors and npm downloads for the main package over the same 30 completed UTC days. A daily or manual workflow updates public metric JSON on `download-metrics`, retaining the previous metric when a source fails or is stale.

### Fixed

- Keep caught verification cancellation active through the `brigade` console script and `python -m brigade` process exit so late repeated signals preserve exit status 130. Programmatic calls still restore their prior signal handlers.
- The workflow-rules repair hint includes the full repo kit (`--full`), so following it installs the missing rule templates and keeps rule files that already exist. (Fixes #1762)
- Fleet event and message spools work on Windows when an existing drive or share root denies directory creation. Existing directories are recognized before treating a creation error as a persistence failure.
- Dot reports `unsupported_on_windows` for its Linux-only local runtime and state operations. Portable auth/config tests now use native absolute paths, and the bounded command runner uses Brigade's owned Windows job objects and pipe readers.
- T3 dispatch now reports an explicit unsupported-platform error on Windows before opening private runtime, ledger, or wake-log files. Its existing POSIX owner-only storage requirement is documented.
- Cloud leases for bound Jules and Cursor sessions no longer lapse five minutes after launch. The hub `bind` action accepts an optional `ttl_seconds` (same 1 to 86400 range as `admit`) that resets the lease clock in the same update, and the Jules and Cursor launchers bind with six hours, matching the tracker's stale-ready threshold. `brigade run cloud sync` renews active leases with the same six hours instead of 900 seconds. A bind without `ttl_seconds` is unchanged, and an older hub ignores the field and keeps the admission TTL. Codex Cloud still renews in-process. Running `cloud sync` on a schedule is not part of this change. (Refs #1794)
- Jules sessions that finish with a pull request now reach `landed` once it merges. The tracker reads the `pull_request_url` the session reports, looks that PR up directly by URL, and records its head branch on the entry, so branch names without a `jules/` prefix no longer strand a merged session at `ready-to-land` and then `stale`. Branch discovery by prefix is unchanged. (Refs #1794)
- The npm validation harness canonicalizes its temporary root before deriving install and cache paths, preserving exact lockfile integrity checks on symlinked temporary directories. A manual read-only macOS workflow can validate the retained original release archives with separate packaging and harness identities.

## [0.29.0] - 2026-10-18

### Changed

- Verification reaps descendants that escape the process group on macOS when a run is canceled or times out.

- Claude hook state reads retain the opened snapshot when atomic replacement unlinks it before the first metadata check, preventing retry starvation while preserving observed writes and containment checks.

- Declared version and beta preview base move to 0.29.0 after the v0.28.0 tag so publish-dev wheels sort above stable. (#1787)
- Add an unpublished 0.29.0 npm/npx distribution candidate that bundles Brigade's wheel and provisions pinned uv and managed Python on Node-only machines. Candidate CI defines native acceptance jobs for Linux, macOS and Windows. Registry publication remains a separate step.
- Preserve canceled verification receipts, neutral outcome capture and the first interruption signal across command startup, process cleanup and receipt finalization. Canceled verification skips optional code-graph work.

- The Command Deck opens on a Fleet overview: a left sidebar with the page nav, a machine tree, and cloud providers; four summary cards; an alerts strip with first-observed times; working and queued lists; and a machines table with seat and load bars. Deck pages and the fleet boards follow the system light or dark scheme. The routing snapshot gains an additive `node_id` per machine so stations join to machines by node id. Center stays dark for now.

## [0.28.0] - 2026-10-18

- Background cleanup of an orphaned fleet claim keeps using the hub URL and token of the claim that scheduled it. Before, each retry re-read the fleet configuration, so a process whose configuration changed could send the cleanup request, with the new token, to a different hub. Renewal and exit release use the same bound connection. (Fixes #1778)

- Deck, fleet boards, and Center use shared spacing tokens derived from one scale while preserving existing layout sizes. Center navigation explicitly uses the shared Deck nav class. (#1495)
- The dot connector pack connects a ChatGPT dot to T3 fleet dispatch, Brigade memory and the Fleet Hub inbox, with Cloudflare Access authentication and operator checks for setup and service health. (Refs #1765)

- Fleet Hub refuses oversized or ambiguously framed JSON bodies, documents above the nesting and aggregate complexity budgets, and new acquisitions above 1024 active claims per node. Clients report claim quota refusals as definite failures, terminate the lease, and schedule fenced cleanup after an ambiguous acquire. The JSON complexity budget accepts producer-shaped Worklore import batches, with regex scans for string and numeric bodies. The claim count uses an index repaired on current-version databases without a schema version bump. Result-set bounds move to #1738. (#1151)
- Claude hook session state, timeout journals, pending increments, locks, and recent-state reads use validated directory descriptors on POSIX and native handles on Windows. POSIX reads preserve held-fd contents across atomic replacement. Windows publication retries open-handle contention for a bounded interval. Integrity refusals preserve observed writes and stop repeated Stop attempts safely. Ordinary resource errors retain fail-open behavior. Doctor and adoption report unsafe session storage without aborting their reports. (#1238)
- Fleet claims reject stale holders within a durable seven-day retirement horizon, bounded by a 1,000,000-row global budget and a 100,000-row per-node quota on free-target cleanup and every non-retry admission, including superseding replacements. Live-claim retirement is exempt from the node quota, and expiry pruning handles at most 32 unrelated claims per acquire. Target generation fences are never evicted. Optional `generation` on `/claims` and inspected force release fence obsolete operations with `stale-generation`. Schema 34 preserves live claims and v33 retirement history while adding age indexes. Claim mutation fencing failures return HTTP 409 with terminal `claim-fence-refused` and an empty owner object to prevent base-client local fallback. Read-only fencing failures return HTTP 503 for monitoring. Oversized new targets return terminal HTTP 409 with `claim-target-too-long`. Exit and rejected-grant cleanup retry fencing refusals until their deadline. Missing-row holder release stays HTTP 200 with `stale-holder`, and the CLI preserves JSON and exits 1. Fresh legacy holders and current live holders remain compatible. See [claim fencing](docs/fleet-claim-fencing.md) for the finite-horizon residual and rollback procedure. (#1189)
- Antigravity model pins map bare Gemini Argon IDs such as `gemini-4-argon` to the default low-effort slug, matching flash/pro. Explicit effort slugs remain unchanged. Document the `agy_argon` Gemini 4 Argon High subscription seat for implementation and research, excluding security review. (Refs #1747)
- Runs with an acquired fleet claim now share one cancellation registry across registered local workers and local `t3-fleet` client calls. `brigade run` checks claim loss before planning, synthesis, dispatch, and terminal result receipts, waits for cancellation outcomes before releasing its local run lock, and retains the lock if cancellation fails. POSIX groups get a three-second SIGTERM grace for nested seats. Only directly registered groups are guaranteed to stop. T3 calls without a registry retain their previous subprocess behavior. Remote jobs and target-owned reservations are unchanged. (#1219)

- `brigade fleet events report` delivers or durably queues T3 lifecycle observations with stable replay digests and bounded execution context. Hub schema 32 preserves event history and exposes reported initiators and request correlation on fleet boards. Unknown or delayed observations do not appear as running. (Refs #1630, #1121)
- Upgrade the hub before enabling t3-fleet reporting. T3 observations remain fresh for 900 seconds with a 300-second producer heartbeat. Permanent hub rejections return exit 2 without queueing. Older binaries refuse schema 32. For the nullable context-column migration alone, rollback can retain the column and set `PRAGMA user_version=31`. See [deployment and rollback instructions](docs/fleet-t3-events.md).

- Grok Bot: new first-party connector pack `t3-dispatch` launches and steers T3 threads through `t3-fleet`, with a one-minute wake watcher.
- `brigade fleet models set` accepts `--t3-model` and `--clear-t3-model` to bind the exact native model id T3 launches for a seat, next to `--t3-instance-id`. The t3-fleet plan binding then carries `model`, so seats whose canonical id differs from the native id (for example OpenCode `opencode/<model>`) launch through T3. The value requires a T3 instance binding, survives later `set` calls and fleet policy activation, and does not change the brigade-run plan. Hub schema 32 adds an additive `model_policy.t3_model` column.

- The admin fleet Machines board shows unread totals for active agent principals in an Agents panel. Acknowledged messages and revoked principals disappear from the panel. (#1723)

- Fleet messages support administrator-managed agent send grants, protected per-invocation token files, sender labels, threads and kinds, sender-scoped acknowledgement status, stable client IDs, credential-scoped flushing, and JSON error status and codes. Hub schema 31 migrates existing messages without changing legacy payload hashes. Tighten #1705: every new agent message, including replies, requires a matching send grant and shares the 200-per-recipient unacknowledged cap. Reusing a queued client ID preserves identical requests and refuses changed payloads locally. Enrollment without grants remains compatible with schema 30 hubs. (Refs #1688, #1121)
- `brigade fleet roster validate` uses fixed purpose and caveat labels and bounded identifier grammars instead of screening free text for leaks. Model IDs support numeric context suffixes such as `[1m]`. Unknown labels, malformed IDs, and oversized values fail validation without regex backtracking. (Refs #1259)

- Fleet Hub upgrade: initialization now enforces mode `0600` on the database and SQLite `-wal`, `-shm`, and `-journal` sidecars on POSIX. Startup fails if required permission changes are refused, including sidecars owned by another user or storage on a read-only filesystem. Check ownership and writable storage before upgrading. `brigade fleet flush` now exits `1` if either the event or message flush fails. (Refs #1688)
- Research doctor reports missing browser readiness evidence as a warning when executable and auth checks pass, including required browser discovery and Oracle-only synthesis or review. Actual readiness blockers, missing executables, and failed authentication still fail required lanes. (#1713)
- Fenced Worklore checkpoints atomically refuse overlapping repository path declarations with bounded, structured conflicts. Handoff, terminal and unchecked historical scopes remain reserved until explicit replacement or release. Ownership reads expose historical `path-admission` while retaining unknown liveness. (Refs #1595)
- Add a preview Fleet Hub OCI image and loopback-only Compose and Quadlet recipes. Pin to a reviewed source revision and exercise Linux amd64/arm64, non-root secrets, node ingestion, crash recovery, SQLite backup/restore, and HTTPS enrollment through a loopback proxy. Podman runtime acceptance and registry publication remain deferred. (#1635)
- Windows skills state reads and mutations use held native handles, reject reparse escapes, and fail closed when anchoring is unavailable. Read-only commands retain traversal and read access without requesting mutation rights. (#1213)
- `brigade memory recall --query` accepts bounded caller-supplied task context using the existing fail-open recall API. Task/worktree fixtures compare relevance against basename queries and report stale/scope limits. Search status adds read-only repeat-query/result counts from the existing local log, separate from verification scores. (#1563)
- `research doctor` keeps installed Oracle browser lanes unverified until readiness evidence matches the named lane and owner. It reports distinct blockers for profile, authentication, verification, quota, mode, model, collector, and resume receipt gaps. (#1614)
- Persist bounded sanitized managed Claude review history in caller-owned SQLite transactions, with exact replay, revision snapshots and conservative caller-head projections across pushes and reruns. (#1601)
- Fleet Hub schema 26 preserves legacy claims and adds durable target generations across acquire, supersede, release, and expiry pruning. Listings, inspection, JSONL/CSV exports, and Dolt history expose sanitized generations and the legacy protocol. Claims exports validate fences before output, and Dolt sinks add metadata columns while preserving table indexes and history. New fence targets are limited to 1,024 UTF-8 bytes while longer migrated targets remain usable. Existing fence history survives old-schema upgrades. Fence capacity, corruption, exhaustion, and unsupported newer schemas fail closed with sanitized HTTP refusals, including dashboard readers. Legacy late-acquire and same-holder races remain open. (#1189)
- Cursor ACP worker receipts distinguish the declared model argument from the observed model label and `acpx` version, preserve unavailable backend revisions as unknown, and validate provenance before serialization. (#1624)

- Jules cloud status preserves explicit empty-output evidence, so completed sessions with a recorded plan but no work are labeled for plan approval.
- `brigade doctor` reports an `instruction-chain: codex` check for codex-harness targets. It estimates the Codex 0.160.0 project instruction chain at the target under stated default assumptions, warns on consumer-cap truncation separately from Brigade advisory budgets, and reports reason codes when it cannot evaluate. Results are fresh-disk estimates, never observed session consumption. (Refs #1564)
- Add `brigade receipts export derivative` for locally minimized evidence exports with source artifact digests, redacted allowlisted receipt metadata, and explicit authorization and verification limits. (#1623)
- Treat generic JSONL imports as external content for every import kind, redacting email addresses and infrastructure values from import text before persistence while preserving importer identity. Record the shipped #1006 fixes and the separate same-UID containment residuals.
- The fleet hub rejects `/events` posts with HTTP 422 when any string event field exceeds its length bound (64 characters for the timestamp, 128 for node, state, digest and capability fingerprint, 512 for seat, harness and repository identity, matching the run-event payload bound, 1024 for the repository, and the 16384-byte journal line bound for run id). New claim targets are capped at 1024 UTF-8 bytes with HTTP 409, `claim-target-too-long`, and an empty owner object. Invalid UTF-8 remains HTTP 422. Migrated longer targets remain inspectable, renewable, releasable and re-acquirable, but their lifecycle events are rejected by the `/events` repository bound. Bounds are measured before trimming, and one oversized event rejects the whole batch. Retention and result-set bounds remain open. (Refs #1151)

- Explicit authority downgrades remove every posture marker governing a relocated workspace and stop when marker state cannot be read safely. (#1217)
- Explicit authority downgrades remove every posture marker governing a relocated workspace, fail closed when marker state is unreadable, and record removed fingerprints only after rollback is no longer possible. (#1217)
- Candidate skills use the same post-regression install margin as candidate memory cards: after a verified regression, clears must exceed hurts by at least `install_min_helped`, so a skill at helped=2, hurt=1 now holds instead of installing. Skills without regressions are unchanged, and zero-threshold fork overrides still require helped to exceed hurt. (#1686)
- Run the full local verification gate in deterministic pytest shards with combined coverage and counted concurrency slots shared by linked worktrees. Shard and slot counts are configurable, and contention can wait for a bounded deadline.

- Speed up strict JSON validation of unescaped ASCII strings while preserving byte limits, Unicode checks, and string-subclass isolation. (#1674)
- Keep Worklore item, fleet-event, and link cursor reads within their SQLite work budget by seeking existing ordering indexes, preserving filtered pagination and timestamp ties. (Related #1259, #1601)
- Honor update-notifier opt-outs before resolving the cache path, avoiding Windows platform subprocess probes during offline CLI adoption. (#1664)
- `brigade fleet cloud set` updates global and provider hosted-worker concurrency with administrator authorization. Limits persist across hub restarts and preserve omitted policy fields and active leases. The writer requires an upgraded hub, and explicit null policy limits are rejected. (#1576)
- Fleet journal events use the same repository key as claims, so repositories sharing a home identity appear in separate status groups. Transient git failures retain event reporting with a workspace-name fallback warning. (#1662)

- GrokBot treats replacement as the visible commit point for POSIX queue-storage writes, including rows, reports, snapshots, and authority markers. A later directory-fsync failure logs uncertain crash durability and reports success, preserving granted fleet and cloud claims. Failures before replacement still raise and release first-claim ownership. (#1660)
- On Windows, control assessments classify refused symlinks and reparse points as `invalid` with `symlink_refused`, including linked run directories and local commit-trailer receipts. Synthetic NT metadata with unset file attributes no longer interrupts directory discovery. (#1642)
- Add a local CRA actor and incident worksheet separating applicability, incident triggers, reporting deadlines, and actual filing evidence, with three synthetic drills. (#1625)
- Add coordinated journal process qualification for locked same-key tail/index reads, replay/conflict, stale-head rejection, and deliberate termination at complete or partial write boundaries, with an inventory of durability limits. (#1565)
- Journal sync failures report indeterminate outcomes. Checkpoint recovery synchronizes the verified head while holding the writer lock through terminalization, including recovery of multiple stale claims. (#1565)
- `brigade doctor --operator` checks the Grok CLI against the reviewed 1.0.13 JSON-envelope floor when that harness is selected, with bounded probing and safe missing/uncertain-version diagnostics. (Refs #1351 item 7)

- Correct the twelve reviewed NIST AI RMF claim mappings: six mismatched pairs receive conditional mappings, three become unmapped evidence properties, and three retain documented prerequisites. Provenance now names sources, actors and verification limits. Migration: `crosswalk_version` is 2 and `control_id` uses the NIST space format, so consumers matching old IDs must update. The template schema stays `brigade.control_crosswalk.v1`; controls output adds `relationship_semantics`, `integrity_boundary`, root `unmapped_evidence_properties` and per-row provenance keys. Local evidence states its tamper-evidence and custody limits: EC-12 rationales say the evaluator checks only the unsigned manifest's entries-list digest, and EC-06 says a consistent journal rewrite is undetectable without an external anchor. Licensed ISO/AICPA text remains unverified. (#1616)
- `brigade evidence controls` emits schema `brigade.evidence_controls.v2`, adding a per-claim `evidence_readiness` assessment that separates discovery, structure observation and claim validation across six independent dimensions. Only claims whose every in-scope artifact validated still report `evidenced_passed`. Presence- or structure-only claims (EC-01, EC-03, EC-07, EC-09 to EC-12) now report `untested`; EC-01 stored digests and optional local HMACs are unchecked, so receipts stop at `structure_observed` until the integrity adapter lands. EC-01 needs each command to match its planned check in order. Failed or rejected receipts report `evidenced_failed`, and canceled ones stay incomplete. EC-02 and EC-06 reject evidence copied from another run. EC-04 discovers the `requests/<nonce>.json` envelopes that request signing writes. EC-05 keeps a known journal, run-metadata or event-shape rejection when `ssh-keygen` is missing. EC-08 counts trailered and untrailered commits in its 20-commit window, and a missing local receipt is incomplete, not a digest failure. A missing `ssh-keygen` or `git` otherwise reports `unavailable`. Under `--run-id`, unbound artifacts never count, and a truncated scan never validates. A claim with no reason code reports `reason: null`. Receipt digests and strict package verification remain #1618. The index is not a score, gate or audit conclusion. `docs/control-crosswalk.md` documents the contract and migration. (#1617)
- Add `fleet campaign preview` for bounded offline repository membership plans, explicit task/action bindings and prior-preview conflict checks. Supplied observations remain unverified, with unknown task states and no dispatch or completion claims.
- Add `fleet campaign inspect` for exact recorded local action/import/task lineage with bounded reads and opaque diagnostics. Local task status is an observation. Specification verification and launch safety stay unknown, and campaign completion stays false. (Refs #1128)
- `fleet work ownership show` reads bounded item and ownership metadata with separate revisions and observation times, explicit unknown liveness and unchecked conflicts, and sanitized refusals. It performs no ownership writes. (Refs #1595)

- Command Deck links optional bounded observed-work groups with configured attribution, bounded stored references, snapshot freshness on home and detail pages, and unavailable record notices. Groups use existing dashboard authentication and leave generic Worklore visibility unchanged.

- Node identities accept optional offline `center_url` metadata without changing default initialization or machine authority. Invalid values suppress links and preserve identity. A bounded URL validator and escaped `Open Center` renderer provide a preparatory boundary. Reporting, Hub persistence, and card wiring remain open. (#1495)

- Command Deck adds a read-only Worklore task page with bounded cursor pagination, plain stored fields and reference summaries, explicit truncation, and existing dashboard authentication. (#1609)

- Claude Actions snapshots can be collected through a bounded injected GET transport with offline defaults, independent per-attempt definition confirmations, quarantined identity conflicts, and separate current-head evidence. Live transport, discovery and FleetHub integration remain pending. (#1602)

- `fleet dot report` validates bounded metadata offline by default and publishes explicitly reported cloud presence only with node authentication. Holder reports append fenced Worklore ownership events without changing task fields or lifecycle. Session cloud context adds schema 24 while preserving local snapshots. (#1606)

- Grok Bot role listeners accept optional `--client-id` across setup, serve, doctor, canary, and service rendering. Same-role clients use separate validated config paths, service names, and local lease identities. Hub-backed clients must match their credential-authenticated node ID. Legacy role-only configurations and tool inventories remain unchanged.

### Fixed

- Python code-graph module assignments shadow earlier definitions and imports. Conditional assignments and external or module imports retain lower-confidence callable candidates when another binding can supply a definition. Existing indexes refresh assignment bindings on sync. (#1658)
- Jules session reads request a metadata-only partial response, so large prompts and patches do not block inventory. Output presence and documented nested PR URLs remain available under the existing byte and paging limits. (Refs #1498)
- EC-05 retains malformed and incomplete approval journals even when no approval event was parsed. A valid approved sibling cannot hide those runs. Clean unapproved runs remain absent. (#1642)
- Managed Claude review timestamp comparisons accept normalized UTC timestamps on Python 3.10, retaining stale, superseded and future-timestamp uncertainty. (#1601)
- Claude cloud register/adopt accepts explicit session and repository bindings with local replay/conflict checks. Unsupported provider lifecycle remains unknown separately from GitHub artifact landing, local continuation and lease evidence. (#1600)
- Local worker preflight preserves bounded model-seat denial reasons for disabled seats and exhausted capacity, and identifies the selected provider. Unknown remote errors remain generic, with admission and fenced lease behavior unchanged. (#1573)
- Roster and policy forms preserve retired role assignments and admission defaults during unrelated edits and repeated previews. Operators can clear or replace them, while retired seats remain unavailable as new choices. (#1580)
- Fleet model lists and roster-page seat tables and selectors exclude retired seats, while separate retirement history and signed admission identities remain available. Retired seats can be disabled safely, and re-enabling stays blocked without deleting run or audit records. (#1571)
- Bound Windows journal sibling locks request read/write data rights without DELETE, so CRT producers can open the same empty lock file in either order. Required native acceptance now exercises both two-process orders.
- Inbox archiving rejects generation changes at publisher reopen, preserves pending records on disk-full publication failures, and restores the original inbox when replacement is interrupted after rename. Archive reads avoid retaining a second full raw snapshot, and Windows recovery tests retain their data checks without requiring POSIX permission bits. (#1581)

- Windows journal mutations serialize bound reapers and unbound controllers on the same sibling file lock, preventing concurrent appends from forking the sequence and digest chain. Lock acquisition fails closed when serialization is unavailable. (#1591)
- Windows journal writes preserve binary LF framing, and recovery preserves complete legacy CRLF records while quarantining the exact partial suffix. Bound recovery uses directory handles and supports nonexclusive creation and append. (#1565)
- Journal append results and exported event dictionaries detach nested payloads from caller-owned data so later mutations cannot change the accepted event copy. (#1565)
- Journal, lifecycle, and control retries synchronize the existing event before acknowledging it again. Fresh appends also synchronize the events directory, and control retries preserve the original event without repeating transport. (#1565)
- `brigade memory status` no longer counts an inbox's `TEMPLATE.md` or dotfiles as pending handoffs, so its pending count and oldest-pending age match `brigade handoff doctor`.
- Windows per-file pytest now separates product regressions from incomplete infrastructure coverage, records atomic v2 progress and coverage counts, accounts for CI setup time, and runs validated serial selectors before parallel files. Each child now launches atomically into a noninheritable kill-on-close Windows job, with orphan cleanup, immutable termination causes, and a shared cleanup deadline. Launch and cleanup failures retain incomplete evidence and fail closed. (#1551)

- Dashboard enrollment now uses five-minute single-use codes and independently revocable 30-day read-only browser sessions. Bearer query URLs and old HMAC cookies are refused. Browser cookies no longer edit roster/policy pages or access JSON APIs. `fleet enroll` and `fleet sessions --dashboard` support labeled devices, listing and revocation. (#1153)

- Grok Bot scout feeds select issues with fewer failed attempts before retrying lower issue numbers. Hub previews report `listing=none` when queue state has not been fetched. (#1351)
- Grok Bot doctor applies its timeout to feed authority requests and names the check `host-wide-feed-authority`. Hub outages report `unavailable`, authorization refusals report `fail`, and both retain a nonzero exit. Local request timeouts stay out of hub job payloads. (#1351)
- Fleet model defaults require an enabled, non-retired seat with a launch binding for the selected consumer. CLI and roster-page validation now use the same eligibility rules, and rejected defaults leave the roster revision and previous default unchanged. (#1572)
- MCP plan, sync, and doctor report `native_config_malformed` for VS Code input entries with nonstring IDs, preserving native files and refusing the entire sync transaction before writes. (#1575)
- OpenCode MCP import and sync preserve existing flat, nested, and mixed layouts, native activation, timeouts, and live credentials, and refuse duplicate locations before mutation. Safe imports omit sensitive literal headers from canonical storage and retain their live values during adoption. Sync now projects explicit canonical timeout seconds as native milliseconds. (#1560)
- Verification cleanup retains recorded targets when removal fails or a directory has been replaced by a file. Dry-run reports those replacements as skipped.
- Codex MCP sync preserves native HTTP authentication fields, tracks managed header removal, and reports authentication combinations that cannot be projected safely. (#1559)
- The Command Deck counts missing or unknown control-plane authority as a coverage gap, so a legacy routing snapshot no longer renders `ALL CLEAR` and "all control-plane sections reported" while the panel prints `authority unknown`. (#1460)
- A `brigade run` cancellation whose journal append fails no longer drives local state to a terminal result, and a malformed `run_budget.cancelled` list reports a bounded invalid-event diagnostic instead of raising `TypeError` out of journal validation. (#1439)
- `brigade memory care plan-fixes` and `memory care status` now name the reason each metadata fix is blocked, and a freshness date derivable from `last_reviewed` is no longer blocked. A `last_reviewed` near `date.max` reports `freshness-date-not-representable` instead of crashing both commands after a clean scan. (#1467)
- Agent-change verification now separates clean approval-policy refusals as `POLICY-FAIL`, verifies current approval evidence against the signed index tree, and keeps historical approvals from satisfying the required approval. (#1464)

- Committed fleet claim-loss notification and its abort grace proceed independently of rejected-grant cleanup, while holder-fenced cleanup continues during blocking callbacks. Shutdown gives the loss publisher a bounded drain after fenced release. If its thread cannot start, notification follows rejected-grant cleanup. If the abort watchdog also cannot start, abort dispatch precedes the callback. Committed heartbeat credential failures retain their callback after shutdown. Failures arriving after shutdown remain suppressed. (#1219)
- Fleet claims now expire local authority at the last confirmed request-start deadline, even while renewal is in flight. Late grants cannot revive authority and receive holder-fenced cleanup, delayed initial grants refuse admission, and continue mode stops renewing after expiry. Committed loss callbacks survive shutdown while delayed main-thread interrupts are suppressed. Ambiguous initial acquires retain orphan cleanup after a missing retry. Successful retries and delayed-grant refusals preserve two spaced cleanup releases despite earlier positive replies, with a nominal 12.5-second inline release budget. Server commits beyond that window remain possible (#1189). Cancellation is requested at expiry but does not prove worker termination before the hub lease ends. (#1219)

### Added

- Offline Claude evidence normalizers validate supplied cloud session identities, managed-review severity counts and exact-head coverage, and confirmed Actions run attempts. A bounded injected-GET review collector preserves incomplete pagination and unverified attribution. Passive diagnostics inspect supplied workflow definitions and independent metadata. Hub, Worklore and FleetHub integration remains open. (#1600, #1601, #1602)
- Document a proposed opt-in fleet claim generation protocol with generation-scoped invalidation of unseen acquires, selective cancellation of matching committed tenures, shared fences, and pending restore and rollout decisions. No protocol support ships with this design, and legacy orphan and delayed-mutation exposures remain. (#1189)
- Worklore records explicit operator offers, fenced node ownership, metadata checkpoints, handoffs, release, and withdrawal without changing item status or enforcing filesystem conflicts. (#1595)
- `brigade context preview` reports scoped Codex 0.160.0 instruction selection and cumulative byte accounting from supplied settings, with explicit unknown boundaries and separate advisory limits. (#1564)
- Worklore Python clients can read and resume bounded item or filtered global event history pages using the Hub's existing cursors and authorization. (#1595)
- Memory recall accepts bounded explicit queries through its Python APIs and standalone module and uses stable repository hints for conventional linked worktrees. Existing fallback and recall limits remain. Production CLI query wiring, retrieval evaluation, and repeat-search telemetry remain follow-up work. (#1563)
- MCP plan, sync, and doctor report versioned field fidelity separately from transaction success, with complete Codex/OpenCode contracts and explicit unevaluated adapters. Security field losses refuse the transaction, while nonsecurity omissions remain visible warnings. Doctor assesses default project targets without writing native files. Malformed nonblank JSON and all selected invalid target plans, including those behind global stdio acknowledgment, refuse the transaction. Ordinary JSON blank-file compatibility and strict Cursor/Kimi user diagnostics remain intact. Native-auth collisions remain doctor warnings so operator sync can write safe siblings with exit 1. Explicit `oauth: false` remains protected from loss. (#1561)
- `grokbot_queue_claim` accepts an optional bounded `worker_label` (`[a-z0-9-]{1,32}`) so several Builder bots sharing one connector identity stay distinguishable. The listener validates the label, forwards it to the hub unchanged, stores it on local file-backed job rows, and echoes it on the claim, status, renew, and complete projections and in `brigade run cloud grokbot status`. Hub-side storage is a pending fleet control-plane change, so a hub-authority deployment accepts the label and shows none until that column lands. (#1501)
- The Cosign signer profile now exports agent-change and commit-linkage statements as Sigstore bundles for external verification. (#1404)
- Fleet status and Command Deck report policy authority as staged before migration activation and active afterward, without activating the migration. (#1460)
- `brigade memory care closeout --dry-run` previews queued candidates grouped by issue kind without writing a closeout. (#1466)
- Grok Bot rejects `report_text` for draft-PR and branch completions with `report-not-allowed`, leaving the job running and writing no report snapshot. (#1472)
- `brigade run` no longer dies with `CheckpointError: checkpoint bytes exceed MAX_CHECKPOINT_BYTES` after a finished worker writes screenshots or other assets. An oversize recovery checkpoint now degrades: it keeps the tree fingerprint, the bounded `approval_reference` that marks a deliberately approval-paused run, and the names of the oversized asset paths, marks the body `truncated`, and lets the live `run.json` write finish with the real status. Recovery strips the truncation markers on both the authority and the legacy path, so a recovered `run.json` stays auditable. `publish_checkpoint_file` still refuses a raw oversize body. (#1506)
- `brigade run cloud launch --provider jules` accepts `--no-plan-approval` (the default for headless conductor launches; `--auto-create-pr` stays opt-in), `brigade run cloud approve --provider jules --task <id>` wraps `approve_plan`, and `status` classifies a plan-only Jules session as `needs-plan-approval` instead of `ready-to-land` while surfacing the session URL. Plan-only requires observed `planGenerated` activity evidence, fetched by a bounded second read for candidate sessions only; missing evidence never manufactures the label. (#1419)
- Post-publish and Windows native acceptance now assert MiseLedger search by imported item id, not snippet text hidden by 0.27.0 trust gating. Unix published-artifact acceptance runs the same import-and-search contract, leaves an uncaptured receipt so the `--new-only` export is not empty once capture has advanced the export cursor, and retries pipx while the simple index lags the JSON API. Source-mode Windows CI pins engine binaries built from the same commit after the component health report is taken, so pinned binaries never read back as corrupt. (#1492)
- Add exact two-card memory proposals with explicit digest-bound review, private revision history, alias migration, index previews, and recoverable transactional apply. (#1562)

### Changed
- Share Deck navigation and spacing across fleet pages, improve form helper contrast and keyboard focus, keep fleet boards within narrow viewports, and render badge colors through the nonce stylesheet.
- Private Grok Bot manifests, ledgers, action stores, and runtime configuration reads now fail closed on Windows with `secure-owner-read-unavailable` until owner-SID/DACL enforcement is implemented. (#1523)
- Private Grok Bot state path references must be drive-absolute (`X:\path` or `X:/path`) on Windows. A drive-less root such as `/var/lib/state` carries no drive there and resolves against the current drive, but normalizes to a different string than `C:\var\lib\state`, so two settings naming one state tree passed the disjoint-path check that these gates exist to enforce. The five connector packages now share one absolute-root rule in `brigade/grokbot_paths.py` instead of nine near-identical copies. POSIX still accepts slash-rooted paths, and UNC and device-namespace roots stay rejected on both platforms. (#1476)
- Fleet action-state reads now fail closed with `secure-owner-read-unavailable` before the file is opened, instead of reporting the write-side `secure-owner-write-unavailable` after the open, and Fleet private-state enumeration is gated like its Backup counterpart. (#1523)

- Contributing policy drops the formal `APPROVED` review gate: merge requires green required checks and resolved conversations, and documents that GitHub ignores the `CHANGELOG.md` union merge driver when computing mergeability.
- Update-channel documentation now describes beta previews and automatic update-check collection and retention.
- Document the Arch Linux and Omarchy install path using `python-pipx`, including recovery after a system Python minor-version upgrade.
- Scanner commands that mutate or inspect descriptor-protected state, plus inbox, rollback, run-record, and import helpers, now refuse on Windows before touching protected state. Windows scanner descriptor-relative operations remain unsupported pending a safe implementation. (#1512)
- Pull requests run a Windows per-file pytest ratchet. It publishes machine-readable results and per-file logs, allows the recorded baseline failures during rollout, and reports passing allowlisted files as removal candidates. (#1482)
- `brigade center serve` views render in the Command Deck theme through the shared `ui_theme` shell: dark tokens, the Deck masthead and nav, and no light-mode colors. Status chip colors are unchanged. (#1495)
- The fleet boards at `/view/machines` and `/view/repos` render in the Command Deck theme through a shared `ui_theme` module, and Deck forms gain a spacing scale (16px panels, 36px controls, 8px helper-text gap, 40px buttons). Routes are unchanged. (#1495)
- Declared version and beta preview base move to 0.28.0 after the v0.27.0 tag so publish-dev wheels sort above stable. (#1468)

- `brigade receipts export journal` exports one fully validated lifecycle journal as canonical JSONL, with inclusive UTC time filtering, original sequence order and digests, exclusive file creation, and no source mutation. Filtered subsets require original journal context for chain verification. This is the journal-export slice of #1407, following shipped trailers, packages and acknowledgement. Retention (#1621) and prune (#1622) remain separate, and the parent stays open.

## [0.27.0] - 2026-09-06

### Fixed
- Record `tree_fingerprint_head` beside every `tree_fingerprint` in verify and run receipts; the post-commit linkage statement uses it as a `receipt-head` normalization base, proving `LINKED-NORMALIZED` even when the linked commit's first parent is not the run baseline. (#1454)
- Post-commit linkage now always records a `baseline` predicate with `baselineRelation` set to `unknown` (never `null`) when `run.json` has no `baseline_commit`, and `verify-commit-linkage` confirms the comparison as `confirmed` when the statement and recomputed relation both say `unknown`. (#1454)
- Control crosswalk EC-12 rows now reflect the landed evidence package: relationships are `supports` or `partially-supports`, the state rule is `evidence-package-manifest`, and `brigade evidence controls` checks `.brigade/evidence-packages/*/manifest.json` for schema `brigade.evidence_package.v1` and recomputed `entries_sha256`. (#1454)
- Control crosswalk review fixes on `docs/1406-control-crosswalk`: corrected ISO 42001 clause/Annex control identifiers, ISO 27001 A.5.9 and A.8.12 mappings, SSDF practice IDs, SP 800-53 AU-11 and SOC 2 CC8.1/CC7.2 mappings, EU AI Act applicability/bearer metadata, removed Art. 50 and added an explicit Art. 9 no-relationship row, removed unsourced NIST AI 600-1 and CSA AICM mappings with `identifiers-not-sourced` status, and corrected framework publication dates/editions. Strengthened `brigade evidence controls` state rules for EC-02 through EC-11 to use existing verifiers and journal readers, and no-relationship rows now render `not_applicable`. (#1406)
- Test Result attestation export now rederives verify-receipt SHA-256 values
  from the writer's compact sorted-key JSON contract, including receipt
  `path`, and refuses stale or malformed stored digest evidence. Legacy
  digestless exports remain supported. Export receipt lookup is strict and
  bounded. Approval collector integration remains deferred. (#1404)
- Approval v1 and v2 collectors now require a stored receipt digest on every
  matching verify receipt, validate the digest before any tree fingerprint
  filter, and re-derive the Test Result attestation from the same snapshot so
  `receipt.json` is read exactly once. (#1404)
- Agent-change evidence index hardening: run ids are validated against
  `^[A-Za-z0-9._-]+$`, run directories and reference locators are resolved and
  contained under `<target>`, symlinked components are refused, Test Result
  references pass `require_receipt=True` with the snapshot receipt, the
  `request` predicate field is resolved from the journal's `request.signed`
  event, `missing` entries are only emitted for kinds required by the policy,
  and the verifier checks allowed profiles, no-approval cycles, and bounded
  verify-run scans. (#1404)
- Attestation, cosign bundle, and approval verification now reject oversized,
  duplicate-name, non-finite, malformed-Unicode, over-nested, and cyclic JSON
  inputs before signature processing. DSSE accepts both standard and URL-safe
  base64 alphabets under bounded decoded payload and signature sizes, and file
  reads use no-follow regular-file descriptors. Mapping inputs are copied into
  bounded plain JSON snapshots before use, and dot-only fallback attestation
  run IDs are discarded.
- `brigade center report build` rotates old operator report directories automatically under `.brigade/center/reports/` into `reports-archive/` according to retention (default keep newest 20, configurable via `operator_report_retention` in `.brigade/daily.toml` alongside `allow_operator_report_build` or CLI flag `--keep N`), caps `reports-archive/` to the same retention count by deleting the oldest archived directories, never rotates unclosed reports, and supports `--dry-run` to preview rotation without deleting or moving bundles. Fixes #1415.
- Fixed timing flakes in tests `test_release_with_renew_in_flight_never_resurrects` and `test_concurrent_edge_writes_do_not_lose_dependency_edges`. (#1396)
- Antigravity seats now pass `--print-timeout` derived from the seat timeout to `agy` when supported by `agy --help`, preventing worker turns longer than five minutes from timing out prematurely. (#1431)
- Grok Bot hub jobs now count `timeout_seconds` as an execution budget from claim time rather than enqueue time, avoiding premature expiry of jobs that waited in queue, and add an independent `queue_ttl_seconds` to expire unclaimed jobs. (#1409)
- `brigade run cloud grokbot scout-feed` under Fleet Hub authority now re-selects issues whose previous hub jobs expired, failed, or were canceled, using a fresh idempotency revision instead of permanently marking them as already known. (#1364)
- Fleet Hub grants feed and control actors queue-scoped `list` authorization so `scout-feed --apply` can check queue limits and existing jobs under hub authority, and adds a `feed-authority` check to `doctor`. (#1350)
- Grok agent dispatch now recognizes snake_case `end_turn` stop reasons in read-only mode and passes `--output-format json` in write mode, enabling `cli = "grok"` seats to complete turns on grok 1.0.13. (#1349)
- Direct read-only Grok dispatches now bind session continuation to a launcher UUID and reject external session controls while bound, failing unbound output as an output contract violation. (#1333)
- Claude hook timeout handling retries locked state updates and records pending increments in a sidecar when the timeout journal is full, preventing dropped timeout latches under high concurrency. (#1304)
- `brigade run` on Windows no longer fails with `PermissionError` when enforcing journal file modes on read-only descriptors and handles closed pipes cleanly during process termination. (#1098)
- Seat process timeouts now properly terminate child processes and report timeout status instead of failing with an `AttributeError` on `_SeatProcessRegistry`. (#936)
- `brigade skills sync --write` now routes mutations through the shared projection transaction kernel, executing bundle updates, receipts, and install history as an all-or-restored operation. (#924)
- `brigade release` security summaries now expose `projection_truncated_count` when nested security checks or top findings undergo truncation. (#678)
- `brigade release` candidate auditing redacts unsafe Unreleased changelog entries from release candidate artifacts and reports a counted redaction summary. (#674)
- `brigade outcome capture` discovers skills in linked git worktrees by checking the target worktree before falling back to the parent checkout, keeping skill discovery and card fingerprinting repository-bounded. (#874)


### Added
- Agent-change evidence index emitter (`brigade receipts export agent-change`), verifier (`brigade receipts verify-agent-change`), and policy initializer (`brigade receipts agent-change-policy init`), with external policy schema, in-toto predicate, and machine-readable verification output. (#1404)
- Post-commit linkage statement (`brigade receipts export commit-linkage`) and verifier (`brigade receipts verify-commit-linkage`) that bind a run's attested tree to a specific git commit after the commit exists, with exact/normalized tree equivalence, isolated git environment, and machine-readable verification axes. (#1404)
- `brigade evidence controls` emits a versioned, machine-readable control crosswalk (`brigade.control_crosswalk.v1` / `brigade.evidence_controls.v1`) mapping Brigade evidence claims to ISO/IEC 42001:2023, NIST AI RMF 1.0, NIST AI 600-1, NIST SP 800-218/SSDF, NIST SP 800-53 Rev. 5.1, EU AI Act, ISO/IEC 27001:2022, AICPA SOC 2, OWASP Agentic Top 10, and CSA AICM identifiers. The normal command and `--json` output compute an evidentiary state per row (`evidenced_passed`, `evidenced_failed`, `untested`, `not_applicable`) from configured workspace artifacts. `--render-doc` regenerates `docs/control-crosswalk.md` as a static mapping (no state column, no scope, no `evaluated_at` line) that is a pure function of the bundled template; evidence states are computed at query time and are not part of the document. The header states that this is an evidence index and design documentation, not evidence that a control operated, and the template does not reproduce licensed ISO or AICPA clause text. Fixes #1406.
- `brigade governance inventory` now exports one canonical workspace-scoped inventory with bounded observed worker use, node-local Fleet policy facts, and optional CycloneDX machine-learning-model components. Remote MCP endpoints retain only scheme and authority; user-home rosters are never substituted. Fixes #1408.
- Fleet hub roster page at `/deck/roster`: roles (impl, review, chef, research, security, scout), seat and cloud lane toggles, consumer defaults, and notes saved in one revisioned transaction; `brigade work brief` prints the `fleet_routing` block; `fleet preference set` gains `--research`, `--security`, `--scout`; hub schema v19.
- `brigade receipts trailer --run <run-id>` prints `Brigade-Run` and `Brigade-Receipt` trailer lines for a local run receipt so a conductor can pass them to `git commit --trailer`. `brigade receipts verify --commit <sha>` reads the trailers from that commit's message, resolves the run receipt, and recomputes the digest to check for mismatches. (#1407)
- `brigade receipts export package` exports one verify run as a portable, staged evidence package with a detached `brigade.evidence_package.v1` manifest, and `brigade receipts verify-package` independently verifies content integrity of the copied files. The package is integrity-only and does not include command logs, graph databases, or other run files. (#1407)
- `brigade run cloud` is a native parser branch; `brigade run-cloud` still works and prints a one-line deprecation notice. `brigade harness fragments --harness {openclaw,hermes}` fronts the shared fragment backend, with `openclaw fragments` and `hermes fragments` kept as aliases. `tests/test_cli_inventory_contract.py` asserts the parser, `docs/command-inventory.md`, and the roadmap missing-command check agree after alias normalization, and reports (without failing) parser paths that have no direct test invocation (#1229).
- Obsidian Operator Phase 2 atomic structured-file adapter: checked-in `obsidian-plugin/grokbot-operator-adapter` plus operator-only `ops/install-grokbot-operator-adapter.sh`. Public tools, bind, and proposal lifecycle stay unchanged. The plugin now ships a build-time CommonJS bundle of Zod 3.25.76 (pinned URL plus npm integrity, MIT license and NOTICE in-tree) so `addMcpTool` can register the private CAS tools against the published Local REST API v2 host contract, which wraps a `Record<string, z.ZodTypeAny>` shape with `z.object(shape)`. `scripts/bundle-zod.js --check` reconstructs `main.js` and `SHA256SUMS` from the checked-in vendor CJS plus `src/adapter.js` with no network and no writes, and exits nonzero on drift. The three tools use the same exact shape `{path, expected_sha256, replacement_utf8}`; `parseInput` still rejects extra keys, malformed hashes, and oversize replacements. Brigade's Python runtime does not depend on Zod, and the vault install still copies only `main.js` and `manifest.json`. `obsidian_capabilities` stays Phase 1 until a matching live fingerprint (versioned tool descriptions plus input keys) is observed; this slice does not enable, reload, or cut over live services. Outbound Streamable JSON-RPC now covers the maximum allowed replacement plus a bounded envelope, and an unsendable replacement fails `invalid_request` at propose time before a single-use approval can be claimed. `update_excalidraw` binds the embed note path and revision separately from the scene. A Local REST host object change unregisters the old handle and reacquires once. Refs #1255.
- First-party Grok Bot Obsidian Operator Phase 1 connector pack `obsidian-operator` on `127.0.0.1:8773` with public route `/mcp` and the closed six-tool inventory `obsidian_capabilities`, `obsidian_search`, `obsidian_read`, `obsidian_action_status`, `obsidian_propose_action`, and `obsidian_execute_action`. Public capability output stays generic. Preview-first pack lifecycle writes only local config and an explicit unit file; canary is non-mutating, calls authenticated `obsidian_capabilities` through the public listener, and this pack does not cut over live services. Setup stores a validated loopback HTTPS upstream URL and an upstream-key env/file reference, never a raw key. The installed listener builds a fixed Streamable-HTTP native MCP client, verifies CA plus hostname/IP plus SPKI pin before sending Authorization, and maps Local REST search without an unsupported limit, snippets from bounded matches, targeted read/patch, and copy/move `path`/`destination` with `allowOverwrite: false`. Native search, read, and existing mutations retrieve vault tags and deny configured sensitive tags before exposure or execute. The validated Excalidraw helper is launched from a no-follow executable descriptor via `/proc/self/fd/<fd>` (absolute helper, empty argv, staging cwd, allowlisted environment, 45s deadline, 256 KiB output cap, initialize plus four tools). The action store bounds active proposals and prunes expired proposal families. Private runtime files are read through a no-follow descriptor that requires the current UID and mode 0600.
- First-party Grok Bot Backup Steward connector pack `backup-steward` on `127.0.0.1:8772` with the closed six-tool inventory `backup_overview`, `backup_target_status`, `backup_restore_readiness`, `backup_operation_status`, `backup_propose_action`, and `backup_execute_action`. Preview-first pack lifecycle writes only local config and an explicit unit file; canary is non-mutating and this pack does not cut over live services.
- `brigade run cloud launch` starts Cursor Cloud or Jules work from a bounded private prompt file. Provider keys stay in the existing environment resolution, launch JSON never includes the prompt or the private lease holder, and a missing key or bad prompt file makes no provider call. Successful bind registers provider IDs, the prompt hash, the expected artifact, and the holder `sync` needs.
- Grok Bot `feed`, `scout-feed`, and `build-feed` commands can now wake a webhook routine after enqueuing work with `--apply`, sending a bounded notification payload using sender credentials from a local private configuration file. (#1391)
- `wake.json` may declare per-role webhook targets under `webhooks`, so a scout enqueue wakes the Scout routine and a build enqueue wakes the Builder instead of every enqueue hitting the default webhook. Secrets never appear in logs. (#1449)
- Added an opt-in Worklore fleet backlog ledger to Fleet Hub with `brigade fleet work` commands for managing shared backlog items, tracking lifecycle events, and synchronizing issues and task ledgers. (#1361)
- `brigade care install` supports repeatable `--schedule` overrides per entry, preserves custom schedules across reinstalls, parses structured systemd calendar records, and warns when evidence-ledger writers share a timer calendar. (#1326)
- Added the bundled `operations-relay` Grok Bot connector pack with loopback bearer isolation to report operational incidents and propose remediation actions for operator review. (#1330)
- Added approval-gated fleet remediation allowing Fleet Steward to execute catalogued service recovery actions bound to active Wazuh findings under single-use operator approvals and fenced Fleet claims. (#1310)
- Added the first-party `wazuh-triage` connector pack with six closed tools to ingest, normalize, and classify Wazuh security alerts into sanitized incident bundles and non-executable remediation proposals. (#1306)
- Added the first-party `cerebro` Grok Bot connector pack providing five tools for Cerebro memory integration with preview-first setup, loopback bearer security, and read-only canary verification. (#1263)
- Added `brigade run cloud grokbot scout-feed` to enqueue Repository Scout jobs for approved GitHub issues under queue locking, daily caps, and issue idempotency. (#1224)
- Added `brigade run cloud grokbot feed` to validate private manifests and safely enqueue approved Grok Bot tasks under the queue lock with `--apply`. (#1176)
- Grok Bot queue jobs are now projected into Brigade's cloud tracker with `grokbot-cloud` status across Center, work brief, daily status, and operator health. (#1148)
- Added a bounded local Grok Bot job queue (`brigade run cloud grokbot status|claim|cancel|expire`) with leased claims, safe status projections, and artifact references. (#1143)
- Center Agent Activity displays inline provider brand marks, flags lockless or unresponsive runs as stale, defaults machine views to the last 12 hours, and polls every 15 seconds. (#919)
- Added `brigade code export --json` (`brigade.code-graph-export.v1`) and the Center Code Graph view (`/view/code`) featuring module dependency maps, symbol impact drill-down, and branch change overlays. (#887)
- Added `brigade memory topology --json` and `brigade memory inventory --json` contracts and replaced Center's Cards search with the Memory Operations topology and inventory dashboard. (#875)
- `brigade update` passes `GITHUB_TOKEN` or `GH_TOKEN` via `Authorization: Bearer` for GitHub API requests to respect rate limits, retrying unauthenticated on credential errors without leaking token values. (#751)
- Added `brigade evidence memory audit` to report canonical memory card identity coverage, collisions, legacy aliases, and proposed mappings, surfacing body-free projection health states for dashboard consumers. (#844)
- The evidence ledger now enforces a single default-live version per external identifier by tombstoning superseded records, filtering them from search queries, and concealing unreviewed injection-pending bodies in `show --json`. (#843)
- `brigade context` tracks context-pack freshness against underlying files and dependent receipts, rejecting control characters in persisted snapshot paths so doctor reports generic unsafe-path findings. (#492)

### Deprecated
- `brigade run-cloud` remains a working alias for `brigade run cloud` for at least one stable release and prints its existing one-line replacement notice. The generated inventory marks its command paths deprecated. Usage evidence: `brigade evidence search "run-cloud"` found historical receipt `148c30c8534922236df439cb`. (#1230)

### Fixed
- `brigade work verify run --command <script>` timeout cancellation now terminates all descendants of the verify child, including processes that moved to a new process group (e.g. `timeout <secs> <cmd>` without `--foreground`). Descendants are collected via `/proc/<pid>/stat` before and after the group kill and terminated with the same shared grace windows. Fixes #1413.
- Grok Bot hub jobs now expire on their own `timeout_seconds` instead of waiting for an operator to call `expire` by hand. A job's deadline is `queued_at` plus `timeout_seconds`, and the hub enforces it in three places: `status` and `report-metadata` sweep before they answer (`list` and the mutating actions already did), a claim of a job past its deadline is refused with the bounded reason `job-expired` rather than the `revision-conflict` the sweep's revision bump used to produce, and `fleet_hub_grokbot.start_expiry_sweeper` runs the sweep on a 60-second timer for the life of `brigade fleet hub`, so a queue nobody polls still expires (seven of the ten jobs in the 2026-08-31 diagnosis sat queued past their timeout, one for 13h21m against a declared 3600s). Each automatic expiry writes an `expire` row to `grokbot_operations` under an `expire:deadline:<job_id>:<revision>` operation id with a NULL `actor_node_id`, which cannot collide with an operator's own id. The local (no-hub) queue follows the same rule: `grokbot_jobs.status` and `get_job` terminalize an elapsed job before projecting it (both take an optional `now`), and a claim past the deadline expires the job and fails `job-expired`. Timestamp helpers moved to `grokbot_job_clock.py` to keep `grokbot_jobs.py` under the module-size ceiling. Deadline expiry composes with the lease reason added in #1386 rather than pre-empting it: a read keys on the job's own deadline only, so a claimed job whose lease alone lapsed stays `claimed` and its holder is still refused with `lease-expired`; a lease-holder call on a job past its own deadline expires the row, records the operation, and still answers `lease-expired`, since a granted lease is always clamped to the deadline. `job-expired` stays the claim-side answer. Fixes #1353.
- `brigade run cloud grokbot reconcile-reports` lists completed Repository Scout jobs from the hub when the queue is hub-authoritative, pairs each with the local artifact and private snapshot by `task_hash`, and drafts unmarked reports. Leftover pre-cutover `jobs/` rows are not scanned. `unavailable` now distinguishes a hub job with no local artifact (`artifact-missing`) from a local job with no stored report (`report-missing`). Fixes #1367.
- `brigade run` no longer loses a finished worker's result when a long session crosses the 1 MiB capture cap. The cap was charged against every byte read off the transport, including reasoning deltas, tool-call notifications, and command output that Brigade routes and discards, so ordinary session chatter spent the budget, killed the app-server child before `turn/completed`, and left a completed worker's edits stranded in the worktree. Reading is now bounded separately from retention: `proc.MAX_STREAM_BYTES` caps the untrusted stream and is the only thing that terminates the child, while `proc.MAX_CAPTURE_BYTES` caps retained text and is charged only for output Brigade keeps. Retained text keeps its head and tail so a truncated capture still carries the final answer, a turn that completed with truncated text stays `ok`, and `output-limit` classifies as an output-contract violation rather than a transport failure so truncation never poisons seat health. Worker receipts record the observed byte count and the configured cap. Fixes #1144.
- Fleet hub ``init_db`` now runs every schema-creating statement (including Grok Bot and run-preference ``ensure_schema``) inside one lock-guarded ``BEGIN IMMEDIATE`` with the existing locked-database backoff, and the migration connection uses a 30s busy timeout. Concurrent first-touch init on a loaded CI shard no longer leaks ``OperationalError: database is locked`` from DDL that used to sit outside the claims-only retry loop. ``fleet_hub_grokbot.ensure_schema`` uses per-statement ``execute`` so it cannot ``COMMIT`` a caller-held write transaction. Fixes #1272.
- The Grok Bot MCP request gate now replays buffered ASGI receive messages and then calls the original receive callable, instead of inventing a disconnect after the buffered request.
- The isolation posture marker is now tri-state, identity-bound, and fail-closed on persistence (#881 final round). Marker reads previously collapsed every error into "marker absent": a same-uid writer who replaced the user-level `authority-isolation` directory with an unreadable stand-in (or corrupted any marker entry) after flipping the repo-writable isolation flag to `off` and removing the signed store could admit the unsigned dedupe fallback, letting forged sidecars suppress a canonical import. Reads now return present / confirmed-absent / unknown — only a positively read healthy marker store naming no governing marker reports absence; unreadable roots or entries, malformed bytes, non-regular entries, and incomparable identity bindings report unknown, which the fallback treats like present and refuses. Observing genuine `external-key` isolation no longer swallows posture-persistence failures: it binds the marker to the stable workspace root identity (device/inode, the same identity the signed authority record carries) and refuses to proceed when the identity cannot be read or the marker cannot be persisted; `brigade security doctor` reports that as FAIL. Posture markers were keyed by path fingerprint only, so renaming a governed workspace orphaned its marker and let an attacker flip the renamed workspace's config off before its first post-rename observation: the authenticated reanchor now transfers and re-verifies the posture onto the new path (ahead of store adoption, before any unsigned evaluation), identity-scoped reads keep the moved workspace governed even without a transfer, and an unrelated workspace that recycles the old path is never blocked by the stale path-keyed marker. An import append also no longer reopens the authority state through the legacy helper when its own snapshot acquisition legitimately found nothing: append-path validators receive an explicit absent-snapshot sentinel, so one append consumes exactly one acquisition (spy-tested). New regression tests cover the unreadable marker root, persistence-failure observation, rename carry-over without pre-opening, stale-marker non-governance at the recycled path, and single-acquisition appends over intact and removed stores. Fixes #881.
- `brigade run` no longer loses a worker's result or poisons the seat when the combined app-server output stream exceeds the 1 MiB capture cap. An over-cap codex app-server turn (`failure_kind=output-limit`) with salvageable final-answer text is now delivered bounded through the normal envelope gate as a successful worker result, with the truncation recorded via `output_truncated: true` in worker-results.json and the original "combined output exceeded ... byte limit" detail kept; an over-cap turn with no salvageable text stays a typed failure but no longer triggers same-seat retry or quarantine. Direct CLI overflow keeps its existing #1012 contract. Fixes #1144.
- Fleet spool events are no longer lost silently on platforms without descriptor-relative file APIs (Windows; #1157 round 3 regression). `_ensure_private_dir` documented returning `None` there but always returned a directory descriptor, sending `_spool_lock` and `_write_spool_atomic` into the `dir_fd=` / `src_dir_fd=` / `dst_dir_fd=` calls CPython documents as unsupported on Windows; the resulting failure surfaced only as `report_event`'s silent `return False`, so undelivered events vanished without a trace. Descriptor support is now detected once per call (`sys.platform` plus `os.supports_dir_fd` membership of `os.open`, `os.rename` — which registers `os.replace`'s dir_fd support, `os.unlink`, and `os.stat`): unsupported platforms take the documented path-based fallback (`None` returned, a symlinked spool directory still refused, best-effort 0700 path chmod, `tempfile.mkstemp` + `os.replace` rewrite), and any spool write that fails anyway is logged at WARNING with the reason instead of being swallowed. Regression tests simulate the unsupported platform via monkeypatched `os.supports_dir_fd`; POSIX descriptor-scoped behavior is unchanged. Fixes #1157.
- Fleet abort hardening residuals (#1157 round 3). `brigade run`'s credential-failure callback now appends its classification marker before printing the diagnostic and fires `_thread.interrupt_main()` from a `finally`, so a failed stderr write can no longer swallow the callback and let the run continue after the hub rejected this machine's token (receipt kind `fleet-credentials-rejected` either way); the claim-lost callback records its marker before its fallible print, so a dead stderr yields `fleet-claim-lost`, not a user cancel. Fleet spool atomic rewrites are now descriptor-scoped on POSIX: the exclusive unpredictable temp file (`os.open(O_CREAT|O_EXCL|O_NOFOLLOW, 0600)`) and the final `os.replace(src, dst, src_dir_fd=…, dst_dir_fd=…)` address names relative to the validated spool-directory descriptor instead of re-resolving `path.parent`, so a concurrent directory swap cannot redirect the rewrite (`tempfile.mkstemp` remains only for the no-descriptor fallback). The claim-loss abort's watchdog grace fire and the callback-finally fire share one lock-guarded check-and-set on the one-shot fired flag, so a callback ending exactly at the grace boundary interrupts exactly once. Fixes #1157.
- Fleet claim-loss abort no longer depends on the `on_claim_lost` callback behaving (#1157 round 2). The interrupt now fires independently of callback execution: a callback that blocks forever is cut off after a bounded grace period (`fleet_client.CLAIM_LOST_CALLBACK_GRACE_SECONDS`) and a callback raising `SystemExit`/`KeyboardInterrupt` (any `BaseException`, not just `Exception`) is logged — the guarded block is interrupted either way, after the callback has had its chance to record state. A lost-claim abort entered from a non-main thread no longer calls `_thread.interrupt_main()` (which targets the process main thread and would stab an unrelated thread while the owner kept working): the yielded `ClaimDecision.cancel_event` is set instead, and guarded work off the main thread must observe it to unwind (documented in `docs/fleet-sync.md`). The spool directory's 0700 privacy is now enforced through a descriptor rather than check-then-chmod on the path: it is opened `O_DIRECTORY|O_NOFOLLOW`, forced via `fchmod`, re-verified with `fstat`, and lock/spool files are opened `dir_fd` against it; when privacy cannot be applied or verified the write is refused instead of warned about. Hub redirect same-origin checks normalize default ports, so `http://hub` → `http://hub:80` is followed while cross-origin hops stay refused. `brigade run` classifies and records a fleet claim-loss abort on the main thread from the heartbeat's marker, so a heartbeat-thread receipt-write failure can no longer record the abort as "canceled by user"; regression tests assert `run.json`'s `failure.kind`, not stderr. Fixes #1157.
- The canonical-dedupe isolation selector is no longer attacker-writable (#881 follow-up). `_unsigned_dedupe_proof_for_non_isolated_workspace` decided isolation purely from the repo-writable `.brigade/security.toml`, so a same-uid scanner could set `authority_store.isolation` back to `"off"` or malform the file (invalid config normalizes to off) and, while the signed snapshot was missing, malformed, or mid-rotation, select the unsigned dedupe path where attacker-controlled sidecar records suppress a canonical import. Observing genuine `external-key` isolation now persists a per-target posture marker outside the workspace at `~/.brigade/authority-isolation/<target-fingerprint>` (override `BRIGADE_USER_DIR`, beside the existing `authority-signed` sticky state), and the unsigned fallback is refused whenever that marker exists or the workspace configuration exists but cannot be parsed; only a healthy config that genuinely reads `off` without a marker admits it (with the same one-time downgrade warning). Clearing requires the existing explicit `brigade security authority downgrade --target <workspace> --confirm` path, which now removes both sticky markers under the same audit-and-restore discipline; no new CLI verb was added. The isolated-refusal regression test is parameterized over intact, missing, malformed, and rotating store states, asserts the recordless persisted-proof validator is never consulted, and asserts stderr stays free of the downgrade warning; new tests pin marker creation, both refusal attacks, the allowed-with-warning non-isolated case, and downgrade clearing. The fallback's docstring and warning wording no longer claim receipt validation — the unsigned path validates only the persisted sidecar against the workspace's own authority record, as `docs/security.md` already described.
- Import authentication now follows a deliberate two-posture split (#881 operator decision). Legacy identity grants stay signed-only in every workspace: `_legacy_import_source_content_identity` and the existing-migration-proof path keep requiring one verified authority snapshot, so forged receipt or proof bytes never grant legacy source/content identity anywhere. Canonical dedupe suppression, by contrast, follows the workspace posture: with `authority_store.isolation = "external-key"` it keeps requiring that verified snapshot plus its bound persisted sidecar, but in a workspace without external-key isolation no signed store can exist (and a same-uid writer there can already rewrite the import inbox directly; issue #1093 tracks that boundary), so suppression returns to the pre-#881 evidence grade — a persisted sidecar plus reproducible receipt validated against the workspace's own authority record — via the explicit `_unsigned_dedupe_proof_for_non_isolated_workspace` predicate, which logs one warning per process naming the downgrade. This restores duplicate suppression (`brigade work import ingest` no longer re-imports the same canonical row) for every non-isolated workspace while keeping isolated workspaces on the signed path; the round-1 fixture that asserted unsigned-dedupe rejection without isolation was converted to external-key isolation rather than weakening its assertions, and new tests pin both postures. Documented under "Canonical import dedupe and the isolation posture" in `docs/security.md`. Fixes #881.
- Legacy import authentication and canonical dedupe now anchor to exactly one verified authority snapshot per decision, closing two remaining #881 defects. Receipt, directory, and sidecar validators previously re-read the external authority store independently and discarded the verified snapshot's record, so a concurrent same-uid writer alternating a forged unsigned binding with a saved valid signed envelope could combine evidence that never existed in one authenticated snapshot (granting a legacy identity or suppressing an incoming import); `_authenticated_legacy_import_proof` now reanchors first, reads one signed record via `_read_verified_authority_snapshot`, and passes that record into `_has_locally_stamped_import_proof`, `_has_persisted_import_proof`, and every directory/file validator, none of which reopen the store. Workspace relocation also no longer mis-orders trust: signedness was evaluated before the proof-directory open triggered authority reanchoring after a rename, so `authority_is_signed` stayed false on the not-yet-reanchored store and the first matching canonical import was duplicated instead of suppressed; `_acquire_authenticated_authority_snapshot` now runs the reanchor pass before its single verified read and is the one acquisition point used by `_append_import_records`. Regression tests cover both: a store swap between artifact validation and the final predicate is rejected (`test_store_swap_between_validators_cannot_combine_snapshots`), and end-to-end canonical dedupe after renaming a workspace without pre-opening authority directories suppresses the duplicate (`test_canonical_dedupe_after_rename_without_preopening_authority`). Fixes #881.
- Concurrent trusted task-ledger writers serialize again instead of failing (PR #1207 follow-up). The round-4 sendback removed the bounded wait around `_acquire_task_ledger_lock` together with the inherited-lock inversion, but that made every overlapping writer of `.brigade/work/tasks.json` fail closed immediately with `RunLockError: another brigade run appears active` — breaking concurrent edge writes and the work-store characterization's claim race, where the loser must exit 13 (`already claimed`) rather than 2. Task-ledger acquisition is deadline-bounded again with a nonblocking retry loop in the `DEFAULT_LOCK_DEADLINE_SECONDS` style: outside writers wait patiently up to 30 s and raise a typed `TaskLedgerLockTimeout` on expiry; inside a scanner run the bound stays short (5 s) and fails closed with `RunLockError`, so a marked child already holding the writer lock still cannot pin the task-ledger lock for a run window. Lock validation is unchanged (every attempt goes through `runguard._acquire_lock`'s stale reconciliation), and the documented lock order task ledger → run → writer is untouched.
- Security sendback hardening for the import inbox, round 6 (PR #1207). Scanner launches are now supervised: once the direct scanner child exits, `proc.run(supervise_group=True)` reaps the owned process group regardless of pipe state and records `descendants_reaped` on the run receipt, so a forked descendant that closed every capture pipe while holding `inbox.jsonl.writer.lock` can no longer outlive the run and force every later scanner and outside writer into `InboxLockTimeout`. A writer-lock timeout during output ingestion now marks the run failed (previously only `run["error"]` was set), so the command returns nonzero instead of reporting rc 0 with every import rejected as `inbox_lock_busy`. The remaining canonical writers (`import add`, `import context`, single handoff promotion, single promotion, both dismiss modes, inbox archive) hold the canonical writer locks from their initial read through publication — promotion takes the task-ledger lock first, matching the documented order — so a scanner commit landing between a pre-lock read and the locked write survives instead of being deleted by stale state. Ingestion now publishes its run receipt inside the writer-lock window that spans the append, so a receipt failure's snapshot rollback can never restore pre-append bytes over a trusted commit that landed in the release gap. The per-process inbox-lock registry keeps ownership and reentrancy depth in one entry with serialized initialization: an inner thread context stays protected when an outer context exits first, the descriptor closes exactly once at outermost exit, and concurrent first acquisitions wait for the owner instead of observing a half-initialized entry.
- Security sendback hardening for the import inbox, round 5 (PR #1207). The in-process lock registry no longer keeps a stale entry after a lock is released: `_held_inbox_lock` now drops the `_ACTIVE_LOCKS` entry atomically under the module lock before closing the descriptor on the outermost exit, `verify_inbox_lock`/`verify_inbox_writer_lock` look the entry up under that same lock and `fstat` the live descriptor (regular file, single link, dev/ino equal to the lock path), so an acquire/release cycle can no longer leave `verify_*` — and the protected writes gated on it, like a marked child's `_append_import_records_locked` — succeeding while no OS lock is held; reentrant depth accounting still keeps the entry until the outermost exit. Scanner stamping and output ingestion now take the writer lock before each canonical snapshot and hold it through publication or rollback-state capture: stamping's live inbox read moved inside a writer-lock span covering its publication (`_scanner_stamp_new_imports` wraps the locked body), so a marked child committing between the launcher's read and publication has its row appended after instead of overwritten, and ingestion captures its dedup input and rollback snapshot under the same lock that spans `_append_import_records`, so a receipt failure after a concurrent commit restores a snapshot that includes it instead of deleting it. Inbox lock acquisition is deadline-bounded on both platforms: POSIX `flock` retries nonblocking and Windows retries `msvcrt.locking(LK_NBLCK)` against a module default deadline (`DEFAULT_LOCK_DEADLINE_SECONDS`, 30 s, per-call overridable via the context managers' `deadline_seconds`), raising a typed `InboxLockTimeout` (a `TimeoutError`/`OSError`) on expiry — an escaped descendant holding `writer.lock` now fails launcher stamping with a recorded run error that proceeds through the existing bounded cleanup/rollback instead of hanging the launcher (and behind its run lock every outside writer), outside writers get the same typed failure, and the ingestion path records an `inbox_lock_busy` rejection.
- Security sendback hardening for the import inbox, round 4 (PR #1207). The round-3 design passed the launcher's held run-lock descriptor to self-importing scanner children (`BRIGADE_INBOX_LOCK_FD` with `proc.run(pass_fds=...)`); review found that unsound — any child or descendant sharing that open file description could `flock(LOCK_UN)` the run-wide lock open to outside writers mid-run, sibling children adopting the same description got no mutual exclusion between their self-imports, a child holding ambient inbox ownership could invert the documented task-then-inbox lock order and strand the task-ledger lock for the run window, and on Windows the msvcrt adoption branch never actually acquired anything. Inherited-lock adoption is deleted entirely (no descriptor is ever passed to a scanner child, and `proc.run` no longer accepts `pass_fds`). Exclusion now uses two locks opened and validated identically (held no-follow dirfd walk, `O_NOFOLLOW|O_NONBLOCK`, regular file, single link, dev/ino re-check before protected writes): the existing `inbox.jsonl.lock` run lock, which only the launcher holds for its whole run window to exclude outside writers, and a new `inbox.jsonl.writer.lock` that serializes each canonical read-modify-write (imports, backfill, promote, dismiss, rollback, scanner stamping). Outside writers take run then writer; a self-importing child is recognized by a new capability-free environment marker (`BRIGADE_SCANNER_RUN_ID`, set by the launcher) and takes only the writer lock it opens itself — so siblings serialize on their own open file descriptions and no child can release the launcher's run lock; forging or stripping the marker only demotes a process to writer-only or outsider behavior, never breaks exclusion. The global lock order everywhere is task-ledger → run → writer, a test asserts every caller follows it, and the task-ledger acquisition's five-second wait/failure path that bounded the old inversion is removed in favor of immediate fail-closed behavior; on Windows both locks use the same validated msvcrt path and the marker only selects which locks are taken.
- Security sendback hardening for the import inbox, round 3 (PR #1207). The canonical inbox writers (`ledger._write_imports`, `_append_import_records`, `_backfill_import_provenance`, and `_promote_matching_imports`) now hold the same run-wide writer lock as scanner runs — moved into a shared `brigade/work_cmd/inbox_lock.py` module, reentrant per process — so a concurrent `brigade import`, promote, dismiss, or provenance backfill can no longer interleave with scanner stamping or rollback and lose or misattribute trusted rows. Self-importing scanner children join their own run's exclusion window through an inherited lock descriptor (`BRIGADE_INBOX_LOCK_FD`, passed with `proc.run(pass_fds=...)`), so locking writers no longer deadlocks them against their parent run; on Windows (no descriptor passing) that child-side serialization remains a documented residual. The lock file itself is hardened: on POSIX it is opened through a held no-follow directory descriptor with `O_NOFOLLOW|O_NONBLOCK`, validated by `fstat` as a single-link regular file (planted symlinks, FIFOs, and hard links are refused), and every protected write re-checks that the path still names the locked device/inode, failing closed when the lock was replaced mid-run. The reviewed-inbox read (`_read_import_inbox_raw`) now enforces the same 4 MiB snapshot cap and full before/after metadata comparison (size, mtime_ns, bytes-read length) as the publication snapshot, so mid-read growth and in-place mutation fail closed. On Windows, a suspended child whose `ResumeThread` call fails is now reported as a resume failure and terminated instead of being left frozen on its job assignment until timeout.
- Runbook step timeouts no longer orphan descendant processes. Step execution and verification-contract verifier/rollback commands ran via `subprocess.run(..., shell=True)`, whose timeout handler kills only the direct child (the shell), leaving grandchildren alive. Both paths (`runbook_cmd._shell_or_argv_command` and the step loop) now launch children in their own process group and route timeouts through the same shared cleanup path as a normal-exit cancellation (`brigade.proc.terminate_process_tree`: process-group SIGTERM, grace wait, SIGKILL), then still write the step logs and receipt with `exit_code: 124` / `timed_out: true`. Fixes #1190.
- Security sendback hardening for scanner-run subprocesses and the scanner inbox (PR #1207 round 2). Windows child launches now create the owned kill-on-close Job Object before `Popen`, start the child `CREATE_SUSPENDED`, assign it to the job while it cannot run, and only then resume its main thread — closing the grandchild-spawn window between launch and assignment; job-creation or assignment failure is a typed nonzero launch failure that terminates the still-suspended child instead of silently falling back to taskkill on an unguarded tree. Scanner runs now hold a run-wide inbox lock (an adjacent `flock`/`msvcrt` lock file beside `.brigade/work/imports/inbox.jsonl`, reentrant per process) from the pre-run snapshot through stamping or rollback, every inbox writer path in `scanners.py` honors it so honest Brigade writers can no longer interleave with a run, and full metadata (device, inode, size, mtime_ns) plus the bytes-read length are revalidated under the lock immediately before each launch, aborting the run when a same-UID writer replaced or modified the inbox in the snapshot-to-launch gap so substituted rows can never be stamped as scanner-created. A cleanup failure during group termination (`proc.run`) is now converted into a typed nonzero `Result` with an explanatory stderr note and `incomplete_process_group` set, and an exception after `_scanner_run_one` persists its running receipt finalizes that receipt as failed, so neither path can strand a "running" receipt that blocks later runs. The ledger import-inbox snapshot's before/after compare now includes size and mtime_ns and requires the joined bytes to match `st_size`, detecting mid-read appends that keep the inode unchanged.
- The post-exit drain cutoff in `proc.run` no longer lets a terminated process group stand as success. When a scanner or worker child exits while a descendant still holds its output pipes, the group was reaped after the brief drain window but the child's zero exit code survived: the scanner was marked completed and granted a run proof, and any output the terminated group members had produced (or would still have written) was silently discarded as a successful result. The cutoff now records a typed `incomplete_process_group` failure on `proc.Result`, forces a nonzero exit code so `_register_scanner_run_proof` never fires, marks the scanner run failed with an explanatory error, and `run_agent` returns a `failure_kind="incomplete-process-group"` harness failure instead of accepting partial nonempty output. On Windows the tree is now terminated through an owned kill-on-close Job Object bound at launch (falling back to the previous taskkill path only if job creation or assignment fails), instead of `taskkill /T` on an already-exited parent PID with descendants outside any kill-on-close scope.
- Scanner pre-run inbox snapshots no longer swallow `OSError`. Previously any snapshot failure (including the new 4 MiB read limit) degraded to `before_raw=b""`, so a scanner that truncated the oversized inbox below the cap and then failed had rollback publish an empty value over the real pre-run inbox; a missing pre-run inbox was likewise restored as an existing empty file. A single bounded read (`_snapshot_scanner_inbox`) now returns raw bytes, parsed rows, an existence flag, and stable before/after fstat identity; `_scanners_run_payload` aborts before launching the scanner when the snapshot cannot be captured; rollback refuses when the live inbox inode no longer matches the snapshot; a torn same-UID mid-read write is detected via size/mtime metadata comparison; and a previously missing inbox is restored as missing.
- Rollback snapshot helpers no longer accumulate unbounded chunks after their guard checks. `_scanner_inbox_descriptor_bytes` and ledger `_snapshot_import_inbox` enforce their byte caps while reading each descriptor chunk and raise the typed limit exceptions (`ScannerInputLimitExceeded` / `ImportInboxSnapshotLimitExceeded`) instead of relying on an earlier size check.
- Built-in scanner runs no longer use unbounded `subprocess.run(capture_output=True)`. Scanner child processes now run through the bounded streaming capture in `brigade.proc` (`run` with its own process group), so a scanner that floods stdout/stderr trips a typed failure (`output_limit_exceeded: true`, error naming the 1 MiB capture limit, run marked failed) instead of exhausting memory, and on overflow or timeout the whole process group is reaped so a descendant holding the output pipes can no longer outlive the run or block completion past the scanner's own exit. The direct child's exit is no longer held hostage by pipe-holding grandchildren: output drains briefly after the child exits and remaining group members are terminated. Fixes #1197.
- Scanner input reads are now byte-bounded before parsing. Inbox reads cap total bytes (4 MiB) and reject any single record over a per-record cap (256 KiB); scanner JSONL import ingestion enforces the same total and per-record limits while streaming the file; and rollback snapshots of pre-existing run artifacts are bounded instead of reading whole files into memory. Oversized input fails with the typed errors `ScannerInputLimitExceeded` / `ScannerSnapshotLimitExceeded` (subclasses of `OSError`, so existing fail-closed paths surface them as rejections) instead of scaling memory with attacker-controlled files. Fixes #1198.
- A failed scanner run can no longer mint eligible work through the self-import stamping path. `_scanner_stamp_new_imports` previously accepted and rebuilt new inbox rows regardless of run status, so a scanner could append a pending row and still create work after exiting nonzero. On a failed (or timed-out / nonzero-exit) run, the pre-run inbox is now restored exactly and all rows the scanner appended are rejected with `self_import.rejection_reasons.run_not_completed`; only rows covered by an exact successful-run proof are stamped. The file-based `--ingest-output` path already skipped failed runs; the in-memory path now matches it. Fixes #1199.
- Security-review round 6 for #1177: state-root content is never read back by pathname. Install destinations living inside `.brigade` (the built-in `mcp` target's `mcp-resources`) are inspected through the held state-root anchor, so a planted symlinked `SKILL.md` contributes absence instead of leaking outside bytes into `skills diff`, fleet status, compatibility drift, or sync rollback material (`skills sync --write` refuses a destination holding non-plain entries for that run instead of writing over or around it). Selector classification against the skills state root is now lexical and precedes any existence probe: an explicit registry pathname selects the anchored entry for lint/diff/install, and every other location under `.brigade` is refused as a source rather than followed; registry imports and inbox proposals lint their already-collected bytes from private staging (with provenance repointed), so nothing re-reads the freshly written state through the filesystem. `skills sync --write` repeats the eligibility gates (trust level, enablement, supported harnesses) on the projection snapshot itself, so a generation swapped in after planning is re-gated instead of installed. Skill pack paths are classified lexically before resolving — a pack directory replaced by a symlink escaping the state root no longer poses as a trusted external pack for `pack show`/`pack import`, which serve recognized state-root packs only through anchored snapshots. Failed installs and accepted proposals keep ephemeral staging directories out of errors, metadata, and receipts by running the staged-payload repointer on every exit path and passing original operator provenance into proposal imports. Refs #1177.
- Security-review round 5b for #1177: the last pathname-based reads of skills state-root content are closed. `skills sync --write` collects one anchored snapshot per registry entry before lint and derives rendering, fingerprints, receipts, and every mutation from it (history and prior receipts are read through `_read_state_file_bytes`, so a symlinked history or receipt refuses with a typed error naming the file instead of embedding outside JSON); registry read consumers (`skills lint` on `registry:` selectors, `skills diff --against registry`, `skills search --json` metadata, MCP `get_skill`/metadata/changelog/compatibility/lint resources, fleet status, harness profile packages, doctor health, pack build evidence) enumerate entries and read SKILL.md/metadata/changelog through descriptor listing plus `_read_state_file_bytes`, reporting symlinked entry dirs or symlinked SKILL.md/skill.json as refused or absent without echoing outside file contents (configured changelog paths that escape the entry are ignored); `skills pack list/show` enumerate packs by held dirfd and read manifests through `_read_state_file_bytes`; install collects `registry:` sources through the anchor; adapter overlays, fleet receipts, and inbox-proposal fingerprints resolve through anchored reads; and error messages never echo content from refused entries. Refs #1177.
- Security-review round 5 for #1177: every remaining skills command mutation and state read honors the held `.brigade` anchor. `skills publish`, `skills inbox accept/reject`, `skills pack build`, and `skills pack archive` write, import, and move only through the anchored primitives (a symlinked `publish-proposals` or proposal directory refuses with exit 2; a manifest-supplied physical path is never honored as an archive move source; scope and destination names derive from validated slugs), and all of these commands fail closed without descriptor anchoring instead of mutating through an unanchored path. `skills install` collects the source snapshot before any validation so metadata, lint, rendering, and fingerprints always describe exactly the bytes it installs, and records a `rollback_snapshot_fingerprint` at capture time (install and sync alike) that `skills rollback` now verifies — together with fresh-install lint and rendered-text validation on the snapshot bytes — before restoring anything, refusing modified or fingerprint-less legacy snapshots without touching the destination. Receipt replaces and history appends open with `O_NONBLOCK` before the fstat check, so a planted FIFO can no longer block the command. Read paths (`skills diff --json`, `skills history --json`, and inbox list/show/diff) resolve receipts, `history.jsonl`, registry copies, and proposals through `_read_state_file_bytes`/descriptor listing with an lstat-guarded fallback, reporting symlinked entries as refused/absent instead of returning outside file contents. Refs #1177.
- Security-review round 4 for #1177: skills state-root primitives now validate the opened descriptor before mutating. Writes open without `O_TRUNC`, try `O_EXCL` first, require a single-link regular file via `fstat`, and only then truncate and write; appends validate the descriptor before the first byte moves; and reads (anchored and Windows-fallback alike) refuse FIFOs, non-regular files, and hardlinks via `fstat`/`lstat` (`O_NONBLOCK` keeps a planted FIFO from blocking), so a hardlink from a receipt or `history.jsonl` to a same-filesystem victim can no longer be truncated or appended through a skills write. `skills install` reads the canonical previous receipt through the anchored reader and retains only receipts that satisfy the contract — a symlinked receipt now refuses the install instead of copying outside JSON into `previous_receipt`, state, or JSON output. The install source tree is collected once into an immutable byte snapshot and render, source fingerprints, installed fingerprints, and both the anchored and external harness copies materialize exclusively from those bytes (a symlinked `SKILL.md` is skipped by collection and refuses the install rather than being re-read by pathname into the rendered install), and nothing is read back through a pathname to compute receipt fingerprints. On platforms without descriptor anchoring every state-root mutation primitive (write, append, unlink, remove tree, copy into anchor) now fails closed with a typed `SkillsStatePathError` instead of mutating through an unanchored path; reads keep the fallback. Finally, unexpected `OSError`s inside the anchor primitives are translated into `SkillsStatePathError` with the original exception preserved as `__cause__`, so a mid-operation race surfaces as a typed refusal instead of a traceback after partial mutation. Refs #1177.
- Security-review round 3 for #1177: the last path-based `.brigade` mutations in `skills_cmd.py` go through the held state-root anchor. `skills rollback` no longer resolves the receipt-bound snapshot through the filesystem — symlinked `installs` or `rollback/<skill>/<harness>` components now refuse the whole operation before anything is read, copied, written, or deleted; the snapshot is read back through `dir_fd` descriptors (`O_NOFOLLOW`, symlink entries skipped), restored into the install target from those bytes, and the canonical/rollback receipt writes, receipt unlink, and snapshot consumption all run against the anchor (state-internal install targets such as `mcp-resources` are removed and rewritten through the anchor too, and the snapshot binding is matched textually against the un-resolved rollback root so a swapped component can no longer launder an external directory into a "valid" snapshot). `skills inbox add` anchors the proposal copy, metadata write, existence check, and `--force` replacement the same way, so a symlinked `inbox` component can no longer redirect the proposal outside the workspace or make `--force` delete a matching external subtree. Source-tree collection walks sources through held descriptors with single-lstat validation and `O_NOFOLLOW` reads (a file swapped to a symlink between check and use is refused instead of followed) and rejects regular files with more than one link (hardlinks), for registry imports, inbox proposals, and install snapshots alike. Two error diagnostics that referenced an undefined name (`UnboundLocalError` instead of the typed refusal) in the anchored remove/unlink paths now format the anchored state path correctly. Refs #1177.
- Terminal run receipts are no longer unwritable once #578 retry decisions exist. `retry_decisions` was written at the top level of run.json without being registered in the projector's preserved-field ownership inventory, so on journal-authoritative runs the shadow parity projection recorded `projection-error:UnknownSnapshotFieldError`, every later write failed the authoritative prior gate (`authoritative run prior gate not ready: error-recorded`), and failure/interrupt terminal receipts raised `RetainRunLockError` — killing the worker thread before it could record its exit code. `retry_decisions` is now a preserved field in `run_projector.PRESERVED_FIELDS` (alongside `seat_routing` and `health`), so receipts carrying retry decisions project cleanly on every terminal path (success, failure, interrupt, resume) while the closed field inventory still rejects genuinely unknown fields.
- Runbook step timeouts no longer orphan descendant processes. Step execution and verification-contract verifier/rollback commands ran via `subprocess.run(..., shell=True)`, whose timeout handler kills only the direct child (the shell), leaving grandchildren alive. Both paths (`runbook_cmd._shell_or_argv_command` and the step loop) now launch children in their own process group and route timeouts through the same shared cleanup path as a normal-exit cancellation (`brigade.proc.terminate_process_tree`: process-group SIGTERM, grace wait, SIGKILL), then still write the step logs and receipt with `exit_code: 124` / `timed_out: true`. Fixes #1190.
- Skills write paths no longer trust the `.brigade` state root as a plain directory. A symlinked `.brigade` state root that would redirect skill registry imports, install receipts, rollback snapshots, uninstall history, or other skills writes outside the workspace is refused up front with a typed `SkillsStatePathError`, and an ancestor swap between validation and write is detected by reopening the state root with `O_DIRECTORY|O_NOFOLLOW` and comparing device/inode against the descriptor held from validation; receipt and history writes go through `dir_fd`-relative creation against the held descriptor. On Windows, where directory-descriptor writes are unsupported, the state root is resolved strictly (symlink components and external resolution rejected) immediately before each write instead, leaving a small documented check-then-use window on that platform. Read-only fleet status behavior from #1175 is unchanged. Fixes #1177.
- Security-review follow-up for #1177: the remaining path-based `.brigade` mutations in `skills_cmd.py` now go through the held descriptor anchor. Registry import copies and install rollback snapshots use a recursive copy that opens every destination component with `O_NOFOLLOW|O_DIRECTORY` relative to the anchor (source symlinks are skipped, never followed); removal of state-internal installed copies during reinstall/uninstall uses a `dir_fd`-relative recursive delete; receipt deletion uses an anchored unlink; and the duplicate raw path-based receipt write in the install loop was removed so the anchored write is the only one (canonical receipt schema unchanged). A nested symlink such as `.brigade/skills/installs`, `.brigade/skills/rollback`, or `.brigade/skills/mcp-resources` now refuses the whole operation before anything is written or deleted outside the workspace instead of redirecting one step and failing later. On platforms without descriptor anchoring (Windows fallback), state writes additionally reject symlink and reparse-point components across every destination component and strictly re-resolve the parent immediately before each write; the small check-then-use window that remains there is documented on `_StateRootAnchor`. Creating a missing `.brigade` state root now opens and holds the workspace directory first and creates/opens the root relative to that descriptor, refusing an ancestor swapped during the creation window instead of letting both created root and validation land outside the workspace. Refs #1177.
- `brigade run` no longer loses a worker's result or poisons the seat when the combined app-server output stream exceeds the 1 MiB capture cap. An over-cap codex app-server turn (`failure_kind=output-limit`) with salvageable final-answer text is now delivered bounded through the normal envelope gate as a successful worker result, with the truncation recorded via `output_truncated: true` in worker-results.json and the original "combined output exceeded ... byte limit" detail kept; an over-cap turn with no salvageable text stays a typed failure but no longer triggers same-seat retry or quarantine. Direct CLI overflow keeps its existing #1012 contract. Fixes #1144.
- Fleet spool events are no longer lost silently on platforms without descriptor-relative file APIs (Windows; #1157 round 3 regression). `_ensure_private_dir` documented returning `None` there but always returned a directory descriptor, sending `_spool_lock` and `_write_spool_atomic` into the `dir_fd=` / `src_dir_fd=` / `dst_dir_fd=` calls CPython documents as unsupported on Windows; the resulting failure surfaced only as `report_event`'s silent `return False`, so undelivered events vanished without a trace. Descriptor support is now detected once per call (`sys.platform` plus `os.supports_dir_fd` membership of `os.open`, `os.rename` — which registers `os.replace`'s dir_fd support, `os.unlink`, and `os.stat`): unsupported platforms take the documented path-based fallback (`None` returned, a symlinked spool directory still refused, best-effort 0700 path chmod, `tempfile.mkstemp` + `os.replace` rewrite), and any spool write that fails anyway is logged at WARNING with the reason instead of being swallowed. Regression tests simulate the unsupported platform via monkeypatched `os.supports_dir_fd`; POSIX descriptor-scoped behavior is unchanged. Fixes #1157.
- Fleet abort hardening residuals (#1157 round 3). `brigade run`'s credential-failure callback now appends its classification marker before printing the diagnostic and fires `_thread.interrupt_main()` from a `finally`, so a failed stderr write can no longer swallow the callback and let the run continue after the hub rejected this machine's token (receipt kind `fleet-credentials-rejected` either way); the claim-lost callback records its marker before its fallible print, so a dead stderr yields `fleet-claim-lost`, not a user cancel. Fleet spool atomic rewrites are now descriptor-scoped on POSIX: the exclusive unpredictable temp file (`os.open(O_CREAT|O_EXCL|O_NOFOLLOW, 0600)`) and the final `os.replace(src, dst, src_dir_fd=…, dst_dir_fd=…)` address names relative to the validated spool-directory descriptor instead of re-resolving `path.parent`, so a concurrent directory swap cannot redirect the rewrite (`tempfile.mkstemp` remains only for the no-descriptor fallback). The claim-loss abort's watchdog grace fire and the callback-finally fire share one lock-guarded check-and-set on the one-shot fired flag, so a callback ending exactly at the grace boundary interrupts exactly once. Fixes #1157.
- Fleet claim-loss abort no longer depends on the `on_claim_lost` callback behaving (#1157 round 2). The interrupt now fires independently of callback execution: a callback that blocks forever is cut off after a bounded grace period (`fleet_client.CLAIM_LOST_CALLBACK_GRACE_SECONDS`) and a callback raising `SystemExit`/`KeyboardInterrupt` (any `BaseException`, not just `Exception`) is logged — the guarded block is interrupted either way, after the callback has had its chance to record state. A lost-claim abort entered from a non-main thread no longer calls `_thread.interrupt_main()` (which targets the process main thread and would stab an unrelated thread while the owner kept working): the yielded `ClaimDecision.cancel_event` is set instead, and guarded work off the main thread must observe it to unwind (documented in `docs/fleet-sync.md`). The spool directory's 0700 privacy is now enforced through a descriptor rather than check-then-chmod on the path: it is opened `O_DIRECTORY|O_NOFOLLOW`, forced via `fchmod`, re-verified with `fstat`, and lock/spool files are opened `dir_fd` against it; when privacy cannot be applied or verified the write is refused instead of warned about. Hub redirect same-origin checks normalize default ports, so `http://hub` → `http://hub:80` is followed while cross-origin hops stay refused. `brigade run` classifies and records a fleet claim-loss abort on the main thread from the heartbeat's marker, so a heartbeat-thread receipt-write failure can no longer record the abort as "canceled by user"; regression tests assert `run.json`'s `failure.kind`, not stderr. Fixes #1157.
- Raw `scripts/verify-focused` invocations are now denied by Claude and Grok work-loop hooks, matching `scripts/verify`, so focused development checks still run through `brigade work verify` and produce verification and outcome receipts.
- Over-cap codex app-server records are charged exactly once against the capture budget: the ingestion path no longer double-charges every valid parsed record, so normal output between half the cap and the full cap ingests cleanly instead of failing as over-limit (#1200).
- A parsed `turn/completed` record that overflows the shared capture budget now records its completion metadata before the output-limit signal is published, so a turn waiting on the limit can no longer wake, consume its exact `(thread_id, turn_id)` entry while it is still absent, and report `completed_observed=False` for a turn that genuinely completed (#1200).
- An oversized app-server record — anything the reader cannot hand to `json.loads` within the capture cap — never signals turn completion, even when it embeds a well-formed `turn/completed` envelope: a trailing second top-level object, invalid UTF-8 bytes, malformed numbers like `01`, or missing closing delimiters after `"status":"completed"` can no longer forge `completed_observed` through structural salvage. The hand-written bounded record scanner was removed entirely; completion is recorded only for records `json.loads` parses successfully, including a parsed record that then overflows the shared budget (#1200).
- App-server `_observed_completions` is keyed by `(thread_id, turn_id)` and bounded: interleaved turns on one thread each keep their own completion instead of overwriting a single per-thread slot, building an over-cap result atomically reads-and-removes exactly that turn's entry under the state lock (a sibling completion arriving between lookup and consume is no longer deleted), `reset_capture` clears only the entries of the thread being reset so a reused turn id cannot inherit a stale completion, and entries beyond a fixed bound are evicted oldest-first (#1200).
- Over-cap codex app-server salvage is now reachable through the real stream and correctly gated on turn status. Bounded ingestion records the turn id and status of every `turn/completed` notification `json.loads` parses successfully, including a parsed record that then overflows the shared budget, so a turn that genuinely completed while the capture cap was hit still yields `completed_observed=True` instead of being silently dropped before routing. The salvage signal additionally requires `status == "completed"`: an observed `turn/completed` with status `failed` or `interrupted` no longer reaches run transport as a successful worker result or satisfies downstream DAG prerequisites (#1200).
- `brigade run` no longer loses a worker's result or poisons the seat when the combined app-server output stream exceeds the 1 MiB capture cap. An over-cap codex app-server turn (`failure_kind=output-limit`) with salvageable final-answer text is delivered bounded through the normal envelope gate with the truncation recorded via `output_truncated: true` in worker-results.json and the original "combined output exceeded ... byte limit" detail kept; an over-cap turn with no salvageable text stays a typed failure but no longer triggers same-seat retry or quarantine. The salvaged text counts as a successful worker result only when a `turn/completed` event for the turn was observed on the app-server stream; otherwise it rides along as an `ok=False` diagnostic that does not satisfy downstream DAG prerequisites (#1200). Direct CLI overflow keeps its existing #1012 contract. Fixes #1144.
- Pre-run snapshots no longer fail on worktrees with very large untracked path sets. Untracked enumeration now streams `git ls-files --others --exclude-standard -z` through a new NUL-delimited, incrementally decoded capture (`proc.run_delimited`) under its own 16 MiB budget instead of the generic 1 MiB child-output cap, so a listing that exceeds the old cap (for example 21,876 untracked paths) captures ground truth instead of aborting the run before dispatch. Enumeration failures name the exact stage and budget ("untracked-path list exceeded ... byte enumeration limit"), unrelated commands keep the generic cap, and snapshot receipts summarize path sets beyond 200 entries via `*_total` counts so run.json and pre-run-snapshot.json stay bounded. Fixes #1165.
- Center operator reports no longer grow without bound. Each `brigade center report build` embedded the full previous report bundle through `status.operator_report.latest` in the status payload, so every consecutive build folded the entire prior history into the new evidence file (measured: 70 KB, 149 KB, 243 KB, ... per build). Report health now returns a bounded reference to the latest report (id, timestamps, path, counts, digest) instead of the full body, both for center reports (`center_cmd/reports.py`) and repo-fleet reports (`repos_cmd/fleet_health.py`, which fed the same center status payload via `repo_fleet`). Consumers that only need the report identity keep working; the full bundle remains on disk and is loaded on demand by `report show`. Fixes #1164.
- `brigade run --worker <seat>` no longer runs orchestrator health routing on a direct-worker run. Only the selected worker and its declared fallbacks are health-gated, so an unhealthy orchestrator the worker never uses can neither reroute nor abort the run; the seat-routing receipt (when present) is marked `run_mode: direct-worker` and omits unused orchestrator decisions. Fixes #1174.
- The effective sandbox override (`--sandbox read-only`, or read-only mode's implied hard isolation) now reaches the seat-health hard-isolation preflight instead of the probe judging only the static roster declaration, so a Codex worker whose transport passes can satisfy hard isolation through the command-line override. The judged value is recorded in run.json under `health.effective_sandbox`. Fixes #1173.
- The legacy import migration path no longer trusts scanner-reproducible receipt and persisted-proof bindings recorded in an unsigned external authority store. A same-uid writer could forge a post-run `receipt.json` (and matching sidecar proof) and re-bind the unsigned store to the new bytes, granting the forged row a legacy source/content identity during untrusted-identity migration. `_legacy_import_source_content_identity` now requires the workspace's authority record to be a verifier-signed HMAC envelope, so workspaces without `authority_store.isolation = "external-key"` (or after an intentional downgrade) fail closed instead of accepting forgeable provenance. Fixes #881.
### Changed
- The shipped review-heavy `reviewer_flash` preset now uses Gemini 3.8 Flash Low on Antigravity. `brigade run` maps a bare Gemini API id such as `gemini-3.8-flash` to the matching low-effort agy slug, because agy requires `--effort` on the unsuffixed id.
- README comparison cells now name 0.27 named retry/reroute, external harness sessions, and the approved Grok Bot scout feed. Brigade cells were re-checked on 2026-08-27. Beads and Gas Town stay on the 2026-08-26 pass. Other neighbor cells stay on 2026-08-13.
- README comparison matrices and scope now name the 0.27 optional fleet hub, vault project/search/propose, and run child/diff/resume. Beads and Gas Town were re-read on 2026-08-26. Other neighbor cells stay on the 2026-08-13 pass.
- Fleet claims fail closed on lost ownership (#1152). When the mid-run heartbeat learns another owner holds the claim (a renew answered 409 held-by-another), `repo_claim` no longer logs and lets the run continue unarbitrated: by default it logs one WARNING and aborts the guarded run (with no callback, via the same main-thread interrupt path as Ctrl-C), so two machines can never keep working the same repo after a supersede or expiry. Pass `on_claim_lost` to be notified first — the abort still follows the notification even when the callback raises or returns without stopping the run (#1157) — or set `BRIGADE_FLEET_CLAIM_LOSS=continue` (or `claim_loss_policy="continue"`) as the documented opt-out for solo machines, which restores the previous log-and-continue behavior. `brigade run` records a mid-run claim-loss abort as a failed run with failure kind `fleet-claim-lost` instead of "canceled by user". Fixes #1152.
- Fleet hub traffic now bypasses HTTP proxies (#1154): the fleet client's opener is built with an empty `ProxyHandler`, so `HTTP_PROXY`/`HTTPS_PROXY` can neither intercept the plain-HTTP bearer token en route to a tailnet hub nor blackhole its requests. The local event spool is hardened in the same pass: the spool directory is created 0700 and spool, lock, and temp files 0600 regardless of umask, opens are no-follow (`O_NOFOLLOW` where available) and refuse non-regular files such as FIFOs or symlinked spool paths, and atomic rewrites use exclusive, unpredictable temp names in the spool directory instead of the predictable `.tmp` path. Mode assertions are POSIX-only in tests. A symlinked spool **directory** is refused rather than followed, existing permissive spool files are forced back to 0600 on every open, and a chmod that cannot be applied is logged instead of swallowed (#1157). The opener also refuses cross-origin redirects: urllib's default handler replays the Authorization header on every hop, so only same-scheme, same-host redirects are followed and any other 3xx surfaces as an ordinary transport failure (#1157). Fixes #1154.
- Fleet claim orphan cleanup closes its last leak windows (#1157). The background release scheduled after a lost acquire response now attempts immediately before backing off, so a `brigade run` that finishes within one backoff interval still frees the row instead of leaving it for the full TTL; a `missing` answer inside one outstanding-request uncertainty window is retried instead of accepted as definitive, because the abandoned acquire can still commit its row afterwards; at exit, when an abandoned heartbeat re-acquire may still commit, the inline release retry also runs after a definitive `missing` answer — not only on `hub-unavailable` — and each retry is now spaced across one request deadline so back-to-back retries cannot all complete before the straggler POST lands. Exhausted exit retries now log a WARNING naming the residual TTL exposure instead of tearing down silently.
- Dropped the remaining references to the retired `gpt-5.3-codex-spark` review seat from the roster docs and tests; the roster init example now uses `gpt-5.6-terra`.
- Fleet claims fail closed on lost ownership (#1152). When the mid-run heartbeat learns another owner holds the claim (a renew answered 409 held-by-another), `repo_claim` no longer logs and lets the run continue unarbitrated: by default it logs one WARNING and aborts the guarded run (with no callback, via the same main-thread interrupt path as Ctrl-C), so two machines can never keep working the same repo after a supersede or expiry. Pass `on_claim_lost` to be notified first — the abort still follows the notification even when the callback raises or returns without stopping the run (#1157) — or set `BRIGADE_FLEET_CLAIM_LOSS=continue` (or `claim_loss_policy="continue"`) as the documented opt-out for solo machines, which restores the previous log-and-continue behavior. `brigade run` records a mid-run claim-loss abort as a failed run with failure kind `fleet-claim-lost` instead of "canceled by user". Fixes #1152.
- Fleet hub traffic now bypasses HTTP proxies (#1154): the fleet client's opener is built with an empty `ProxyHandler`, so `HTTP_PROXY`/`HTTPS_PROXY` can neither intercept the plain-HTTP bearer token en route to a tailnet hub nor blackhole its requests. The local event spool is hardened in the same pass: the spool directory is created 0700 and spool, lock, and temp files 0600 regardless of umask, opens are no-follow (`O_NOFOLLOW` where available) and refuse non-regular files such as FIFOs or symlinked spool paths, and atomic rewrites use exclusive, unpredictable temp names in the spool directory instead of the predictable `.tmp` path. Mode assertions are POSIX-only in tests. A symlinked spool **directory** is refused rather than followed, existing permissive spool files are forced back to 0600 on every open, and a chmod that cannot be applied is logged instead of swallowed (#1157). The opener also refuses cross-origin redirects: urllib's default handler replays the Authorization header on every hop, so only same-scheme, same-host redirects are followed and any other 3xx surfaces as an ordinary transport failure (#1157). Fixes #1154.
- Fleet claim orphan cleanup closes its last leak windows (#1157). The background release scheduled after a lost acquire response now attempts immediately before backing off, so a `brigade run` that finishes within one backoff interval still frees the row instead of leaving it for the full TTL; a `missing` answer inside one outstanding-request uncertainty window is retried instead of accepted as definitive, because the abandoned acquire can still commit its row afterwards; at exit, when an abandoned heartbeat re-acquire may still commit, the inline release retry also runs after a definitive `missing` answer — not only on `hub-unavailable` — and each retry is now spaced across one request deadline so back-to-back retries cannot all complete before the straggler POST lands. Exhausted exit retries now log a WARNING naming the residual TTL exposure instead of tearing down silently.
- `brigade work brief` and `brigade daily status` now bound their memory working set on large multi-repo workspaces (issue #266): the fleet health section of the brief is compacted before any other section payload is computed, daily-status center sections keep only counts and top-item references instead of full queues and draft lists, per-repo `git status --porcelain` output is streamed line-by-line instead of buffered whole, and per-repo artifact JSON larger than 8 MiB is referenced (`oversized: true`) rather than parsed into memory. Regression tests assert oversized artifacts are never decoded and that dirty-count streaming stays under a fixed traced-memory budget. Fixes #266.
- Center Code Graph now leads with a linked summary strip showing connectivity and change insights, defaults the module map to the top 15 connected modules, and shows plain-language detail cards on selection. (#909)
- `brigade release` readiness now requires security scan evidence to cover the candidate commit HEAD before permitting release preparation. (#675)
- `brigade evidence status` collects source item totals using SQLite's covering index on `items.source_id` instead of scanning table rows. (#873)

### Added
- Fleet run preference overlay (#1223): hub schema v5 stores one `impl`/`review`/`chef` pin, `brigade fleet preference get|set|pull` caches it locally, `brigade fleet status` shows it, and `brigade run` uses `impl` as the default worker when `--worker` is omitted and that seat exists. `--worker` and a spoken roster seat name still win; two named seats leave chef planning. This is a routing overlay, not roster sync.
- `brigade run cloud grokbot reconcile-reports` previews or writes at most `--limit` deterministic Memory Handoff drafts from completed Repository Scout report snapshots. Preview writes nothing. Apply verifies private snapshots, quotes every report line into the canonical owner's review inbox, and records private queue-side idempotency markers so repeats do not duplicate. Report text is never printed or stored in job JSON, receipts, or marker JSON.
- Repository Scout completions can store a private, queue-owned report snapshot of at most 12,000 UTF-8 bytes. Operator-only `grokbot_queue_report` returns the stored text, byte count, and independently recomputed SHA-256.
- Run admission and dispatch now persist retry decisions in the run receipt. Every failed worker attempt judged by the closed retry taxonomy (`decide_retry`) appends a stable record — `attempt`, `seat`, `failure_kind`, `decision`, `reason` — to the run's quarantine state, and `brigade run` writes those records to run.json under `retry_decisions`, so a later run can explain why a seat was skipped or retried. The receipt carries prior decisions forward across a resumed run (record_run_start preserves them, and later writes merge fresh decisions without duplicates), and runs that rerouted a seat through a declared fallback print one summary line (`note: seat reroute: <requested> -> <effective> [<typed cause>]`; skipped seats are named the same way). Direct `--worker` admission routing behavior from #1183 is unchanged. Fixes #578.
- A native Grok Bot MCP adapter now ships with Brigade under the optional `grokbot` extra. `brigade run cloud grokbot` can set up, serve, diagnose, canary, and render a user service for fixed operator, repository-scout, and implementation-worker roles without a second repository. Each role runs in its own process with an exact tool inventory, bearer authentication, host and origin allowlists, bounded requests, safe queue errors, and non-secret configuration. Generated services keep the fleet hub private and grant write access only to the selected target's Grok Bot queue state. Fixes #1163.
- Fleet-sync Phase 5 adds deterministic, read-only hub export with `brigade fleet export [--since <event-timestamp> | --since-received <hub-timestamp>] --format jsonl|csv [--claims [--include-expired]] [--db PATH] [--out PATH]`: events stream in primary-key order with fixed columns and their digest, while claims stream by target without holder tokens and mark expiry explicitly. Claims exports exclude expired rows by default. Named output files are replaced only after a successful export. `--since` filters event timestamps, while `--since-received` includes late-arriving spooled events for incremental archives. Filtered exports report timestamps they could not parse, and human CSV formula-like cells are prefixed with a single quote. The optional `[fleet.sink]` configuration remains disabled by default. When enabled, `brigade fleet sink` imports raw values, incrementally upserts the `fleet_events` append log using an atomic `received_at` watermark, replaces the live `fleet_claims` snapshot with expired rows excluded, and commits changed passes after `dolt add -A` with the UTC export time. `[fleet.sink] timeout_seconds` bounds each Dolt subprocess, and `events_incremental = false` restores full event exports. Fresh Dolt databases use a local `brigade` identity. The subprocess-only sink does not change event reporting, claims, status, or dashboard paths. A missing Dolt binary is a documented no-op that prints the reason and exits 0. Fixes #1127.
- Fleet-sync Phase 3: a hub-served web Fleet dashboard for a laptop or phone on the tailnet. The fleet hub now answers `GET /` and `GET /view/{machines,repos}` with a server-rendered, stdlib-only page in the `brigade center serve` style (no framework, no CDN asset; tables, sort, and filter work with JavaScript off; `<meta refresh>` every 10s, with a small inline script that only ticks elapsed timers and adds a client-side text filter). The machine board shows one card per observed `node_id` (never a hardcoded host list) with its live runs (run, repo, seat/harness, state badge, elapsed, last event), when the hub last heard from it, and the claims it holds; the repo board shows, per repo, where it is running, its claim (owner node, conductor, TTL remaining), its last outcome, and a `collision` flag when two nodes have live runs on one repo or a run is not on the claim owner. Runs are bucketed into `failed` / `awaiting approval` / `stale` (no event for 30 min) / `running` / `queued` / `interrupted` / `succeeded`; the default `sort=attention` puts the first three on top, oldest first, and `sort=age|node|repo|state|seat`, substring filters `node= repo= seat= state=`, `attention=1`, and `all=1` narrow both boards. Same bearer auth as the JSON endpoints, plus phone use: opening the page once with `?token=<fleet token>` 303-redirects to the same URL without the token and sets an HttpOnly, SameSite=Strict, 30-day `brigade_fleet_view` cookie whose value is an HMAC of the token (never the token) and which authorizes only the HTML routes, so a copied cookie cannot read `/status`, `/claims`, or post events or claims; rotating the token invalidates every cookie. No token, cookie, or claim holder token is ever rendered, and responses carry the same CSP / `no-store` / `no-referrer` headers as the local dashboard. The local `center` dashboard is untouched. Fixes #1124.
- Fleet-sync Phase 4: hub-arbitrated cross-machine claims. To work a repo, a machine asks the fleet hub (`POST /claims`): one unique row per target grants `{owner_node, owner_conductor, holder_token, acquired_at, renewed_at, ttl_seconds, expires_at}` to exactly one caller (default TTL 900s), a second caller gets a 409 naming the current owner, an expired claim (crashed or offline owner) is reclaimable (expired rows are pruned on acquire), and an unexpired one is never silently stolen. Every acquisition mints a per-holder fencing token: renew and release must present it (the hub never echoes it to other callers), so a sibling run sharing the same node identity can neither extend nor delete a live claim. `node_id` must be a real identity — the hub rejects `unknown` and malformed ids, and a client without a usable identity stays on the local lock with a WARNING, so two identity-less or cloned nodes are never both granted one target. `brigade run` acquires the claim right after the local run lease (only the run.lock owner ever holds or releases the hub claim), recording the orchestrator seat as `owner_conductor`, holds it with a renew heartbeat (every TTL/3; a claim the hub lost is re-acquired under the same token, and the heartbeat is drained on exit so an in-flight renew can never resurrect a released claim), releases it at run end, and refuses a held target with `repo '<x>' is claimed by node <node> until <expiry>`. A lost acquire response is retried once idempotently, and if the hub stays unreachable a background thread releases any row the lost request committed; a 409 with no owner (target freed mid-request) is also retried instead of failing closed. With no hub configured, or the hub unreachable, dispatch falls back to today's local run.lock with a single log line — a claim failure is never a new way for a run to fail. `brigade fleet claims [--all] [--json]` lists active claims (`GET /claims`; `--all` includes expired), and `fleet_client.acquire_claim` / `renew_claim` / `release_claim` / `repo_claim` expose the client calls. Hub database schema is v2 (adds the `claims` table; a phase-2 database upgrades in place). Fixes #1125.
- Fleet-sync Phase 2: a central fleet hub and event reporting. `brigade fleet serve --host <tailscale-ip>` runs a stdlib-only HTTP service (default port 3774, `--db`, `--token-file`; bearer token from `BRIGADE_FLEET_TOKEN` or the token file, never stored in config) backed by one SQLite file in WAL mode with a versioned schema: `GET /health` is unauthenticated, `POST /events` accepts one event or an array and answers `{accepted, duplicate}` deduped on (node_id, run_id, sequence, digest), and `GET /status` returns the latest state per (node_id, run_id) for live runs (`?all=1` includes terminal ones). Every local journal append also reports the event (node_id, run_id, repo, seat, harness, state, ts, sequence, digest) to the hub configured in `~/.brigade/fleet.toml` (`[fleet] hub_url`, `token_file`; `BRIGADE_FLEET_HUB_URL` overrides). Reporting is best-effort: it never raises into the journal writer, makes one request under a hard 2s deadline (covering DNS), and when the hub is unreachable spools to `<brigade-home>/fleet-spool/<node_id>.jsonl` (lock-guarded, atomically rewritten, capped at 16 MiB, poison 4xx batches dropped with a log line) and flushes in order (no duplicates) on the next successful contact or `brigade fleet flush`. With no hub configured nothing changes. `brigade fleet status [--all] [--json]` prints what is running where: node, repo, run, seat/harness, state, age. Fixes #1123.
- Local fleet-sync Phase 1: each workspace keeps a gitignored `.brigade/node.toml` machine identity (`node_id` uuid4, hostname, roles, platform) created once by `brigade node` (text or `--json`) and never regenerated. New `brigade run` directories are namespaced `{short_id}.{stamp}-{hex}` so two machines cannot collide under `.brigade/runs/`; un-namespaced legacy ids still resolve as the local node. A conservative lease reconcile releases a run lock whose owner process is dead and whose run is already terminal (or older than 24h with no active run), on the next claim and as `runguard.reconcile_run_lock`; `brigade doctor` WARNs on a stale-but-unreconciled lock. Live-owner and in-progress activated-journal locks are left alone. Fixes #1122.
- Research runs now persist a claim-level support audit (`claim-audit.json`, schema `brigade.research.claim-audit.v1`) next to the citation-token audit. The independent reviewer returns one record per checked factual claim (report span, backing source ids, `backed` / `disputed` / `insufficient` / `skipped`, bounded explanation, conflicting sources); Brigade validates spans, ids, enum values, and artifact bounds against the exact report and persisted findings, binds each record to the report digest, finding fingerprints, and synthesis attempt, and derives acceptance itself (reviewer boolean stays a veto, never the decision). A `disputed` / `skipped` / unstated-`insufficient` claim triggers the existing single repair and reruns both checks against the new digest; `insufficient` is allowed only when a limitation is stated next to the claim and the exception is recorded. Resume drops review records whose report digest or synthesis attempt no longer match and re-reviews. `brigade research show` prints one `claim_audit:` summary line and `--json` exposes the artifact ref, verification, and counts without the records; legacy and pre-feature runs report `claim_audit: unavailable`. Fixes #938.
- `brigade runs inspect <run-id>` shows a run's parent-child lineage (parent run id, branch-point event id for children; recorded children for parents) and its terminal lifecycle state, read-only from recorded run artifacts, failing closed on unknown or corrupt runs. `runs show` now prints a `terminal:` line (and `terminal` in the JSON contract), and the doctor runs surface reports parent-child lineage consistency — a child whose parent directory is missing or whose branch-point event is absent from the parent journal is a WARN finding, never a crash. Legacy runs without lineage metadata remain valid roots. A journal terminal event is treated as terminal even when `run.json` still says running (crash after fsync, before receipt replace). `doctor --full` exhaustively rescans lineage (no 50-run cap); the bounded check WARNs with how many runs were not examined. A `parent_run_id` that is absolute or contains `..` / path separators is a finding and is never joined as a path. Refs #594.

### Changed
- Test assertions for private file and directory modes now use one shared POSIX-aware helper, so the Windows suite skips checks for permission bits that Windows does not expose. Fixes #1133.
- The center Code Graph view now defaults to a branch-scoped overlay (changed modules plus their direct neighbors) so the summary strip matches the diagram, collapses `.worktrees/` and similar non-canonical copies unless toggled, adds an Impact-tab "Start from changed modules" shortcut, and notes when the impact layer diagram is capped. Zero local changes fall back to the full map with a banner. Fixes #976.
- The research Oracle seat's browser-auth diagnostic now states that Oracle 0.18+ makes live browser cookie syncing opt-in upstream and points at an explicit cookie strategy (`--browser-cookie-sync`, a configured browser profile, or another supported Oracle cookie source) instead of blaming only a stale jar. `docs/runbooks/research-live-acceptance.md` and the multi-lane research spec document the requirement with upstream links. Refs #939.
- Runs dashboard view answers "what verification ran recently, did it pass, and what command was it?" with a summary strip ("N runs this week, M failed, last run Xh ago"), a primary column of command (task for Brigade runs) with a pass/fail chip, Run ID demoted into a `<details>` expander, and relative timestamps ("3h ago"; future stamps render as "just now", unparseable ones as "unknown age"). Refs #974.
- The center Work view now labels dispatch waves as plain-language steps (for example `Step 2 of 5 - one task at a time (blocks step 3)`) instead of bare `Wave N`, rewords the Parallel tile to Together / yes-or-no with a sentence about whether ready tasks can start at the same time, and draws a small SVG of `blocks` / `parent-child` edges so "what is blocking what" is visible even when waves are absent. The task list stays. Fixes #977.
- The center dashboard Status view now answers "What needs my attention across handoffs, memory care, verify loop, and inbox hygiene right now?" with a plain-sentence summary strip and health tiles (icon + plain word: OK / ATTENTION / MISSING / TIMED OUT, never color alone), following the Memory Operations baseline. Raw ids move into `<details>` expanders, the placeholder rows for latest verify receipt/signal are dropped, and missing brief sections render as readable MISSING tiles instead of `-` table rows. Refs #972.
- Center dashboard Outcomes view states its operator question ("Which skills and cards are actually helping vs hurting the loop?") in the module docstring and renders a one-sentence top-N summary strip (biggest net help vs biggest net drag, derived purely from `ranking[]` entries) above the existing table. The 9-column outcomes ledger itself is untouched. (#973)

### Security
- Fleet hub per-node credentials: a node token is now the node's identity, so one fleet member can no longer post events or acquire, renew, release, supersede, or inspect claims as another. `brigade fleet nodes add <node_id> [--label L]` enrolls a machine and prints its token once (the hub keeps only a SHA-256 in the new `nodes` table; hub schema v4 adds it in place), `nodes list` and `nodes revoke` manage it, and `POST /events` / `POST /claims` derive the caller's `node_id` from the token and answer 403 when a body `node_id` differs (a revoked token answers 401; re-add to rotate). The single shared bearer becomes the admin token: it manages `/nodes`, reads `/status` and `/claims`, enrolls the dashboard cookie, and may post under any `node_id` only when the hub runs with `brigade fleet serve --allow-admin-writes` (off by default, so migration is explicit). Clients gain `[fleet] node_token_file` (`BRIGADE_FLEET_NODE_TOKEN` overrides); without it the client falls back to `token_file` and logs one WARNING per process naming the deprecated shared-token mode, so existing deployments keep working through the documented migration in `docs/runbooks/fleet-hub-proxmox.md`. A 403 keeps events in the spool like a 401 and the claim client surfaces the hub's message. Both token kinds are compared in constant time and never logged; `docs/fleet-sync.md` states the trust model (node token = identity, admin token = control plane, scope/owner fields = honest-client intent). Fixes #1150.
- `run_checkpoint.strip_checkpoint_bodies_for_export` now removes crashed `.checkpoint.*.tmp` temp files from the export copy without reading them (a symlinked or non-regular temp is refused), and `assert_export_tree_has_no_checkpoint_bodies` refuses any temp under a recovery-checkpoint directory regardless of its bytes, including one that happens to parse as an artifact reference. Work-run archives drop their separate pre-strip temp sweep and rely on the shared helper. Local recovery keeps the original temp and bodies untouched. Refs #654, #646.
- Worker and adapter child processes now stream stdout/stderr through a combined 1 MiB capture cap and start in their own process group. Overflow or timeout terminates the POSIX group (Windows process tree). App-server JSONL is read in bounded chunks so a single oversized or malformed record is charged and trips `output_limit_exceeded` before `json.loads` (text-mode line iteration cannot buffer an unbounded line). Queue entries and turn deltas are capped as they arrive (not after `run_turn` returns). Resume hashes the 20 KB wrap body, event writers truncate before persist, stdin is written concurrently with output draining, and returned capture text is cut on a UTF-8 boundary at or below the cap. `run_agent` and ACPX/app-server adapters report `failure_phase=harness` / `failure_kind=output-limit`. Fixes #1012.
- Opt-in `authority_store.isolation = "external-key"` HMAC-signs new directory-authority bindings with a 0600 key stored outside the workspace and scanner-reachable tree (`$XDG_CONFIG_HOME/brigade/authority/store-hmac.key`, or `BRIGADE_AUTHORITY_KEY_FILE`). An existing HMAC envelope is always verified; flipping the flag off cannot skip a MAC check. Once a target is signed, a user-level sticky marker under `~/.brigade/authority-signed/<target-fingerprint>` (override: `BRIGADE_USER_DIR`) keeps raw unsigned records fail-closed even after the repo-writable flag is flipped off or the envelope is stripped. Marker containment is distinct from the HMAC key rule: the marker must live under that user-level brigade directory and outside the workspace, and is not rejected merely because its path contains `.brigade`. Downgrade is `brigade security authority downgrade --target <t> --confirm` only: it converts the signed store to an unsigned record, removes the sticky marker, and turns isolation off so a later read cannot recreate the marker. A key override whose unresolved path sits under the workspace, including a directory-symlink prefix, a file symlink, or a `..` re-entry, is refused before any key bytes are written. `brigade doctor` WARNs when the flag is off, and names the sticky enforcement when a marker exists. Fixes #957.
- JSON run contracts redact Windows `C:\Users\<name>\` and `C:/Users/<name>/` home prefixes to `~` at the same `_clean_str` chokepoint as POSIX `/home/<user>` and `/Users/<user>`, including doubled slashes and usernames that contain a space or apostrophe. An allowlist key without a cleaned counterpart can no longer pass a raw source value. Fixes #1064. Refs #631.
- Evidence-ledger JSON/Markdown/MCP/HTTP exits now pass the complete serialized bundle through one last allowlist walker (`finalizeEvidenceResponse`). Ineligible or quarantined items collapse to `{id?, eligibility_status, reason_code}` — stub `id` is 24 lowercase hex or omitted, so a cached attacker id is never echoed. `grouped_by_source` counts only eligible closed-enum kinds (unknown keys are dropped). `query`/`filters` and other unlisted keys are dropped, not trimmed. Cached `evidence show` / `show_evidence_bundle` regenerate live from item ids. A `kind=url` content hash can no longer authorize swapped artifact text. Fixes #1030, #1031, #1032.
- Opt-in `authority_store.isolation = "external-key"` HMAC-signs new directory-authority bindings with a 0600 key stored outside the workspace and scanner-reachable tree (`$XDG_CONFIG_HOME/brigade/authority/store-hmac.key`, or `BRIGADE_AUTHORITY_KEY_FILE`). An existing HMAC envelope is always verified; flipping the flag off cannot skip a MAC check. Once a target is signed, a user-level sticky marker under `~/.brigade/authority-signed/<target-fingerprint>` (override: `BRIGADE_USER_DIR`) keeps raw unsigned records fail-closed even after the repo-writable flag is flipped off or the envelope is stripped. Marker containment is distinct from the HMAC key rule: the marker must live under that user-level brigade directory and outside the workspace, and is not rejected merely because its path contains `.brigade`. Downgrade is `brigade security authority downgrade --target <t> --confirm` only: it converts the signed store to an unsigned record, removes the sticky marker, and turns isolation off so a later read cannot recreate the marker. The envelope's inner target fingerprint must match the destination; a copied foreign envelope is refused. A failed durability step restores every artifact that already changed. A key override whose unresolved path sits under the workspace, including a directory-symlink prefix, a file symlink, or a `..` re-entry, is refused before any key bytes are written. `brigade doctor` WARNs when the flag is off, and names the sticky enforcement when a marker exists. Fixes #957.
- `brigade runs list --json` and the Run View `GET /api/runs` list now drop a child directory that is a symlink, or whose `resolve()` leaves the runs root, and count it in `skipped_invalid`. Per-run serve routes already 404 those ids; the shared collector did not, so a symlink alias could leak out-of-root task text in the list while its detail route 404'd. Refs #631.
- Scanner lifecycle rewrites now require the verifier-held run proof, `before.source == scanner.source`, and a legal `pending → dismissed` transition. A replacement row is applied as that narrow action, so a matching built-in scanner cannot dismiss another source's finding, resurrect a dismissed row, or assign an arbitrary status. Fixes #1039.
- Explicit `import_path` ingestion retains the same publication snapshot as self-import (pre-commit inbox, persisted proofs, and file bindings) until the final scanner receipt binds. A failed receipt write rolls the import and proofs back together. Fixes #1040.
- `work scanners init` and `--force` publish `.brigade/scanners.toml` through a descriptor-relative no-follow walk and replace only a verified regular file, so a planted dangling or live symlink cannot redirect the write outside the workspace. Fixes #1042.
- Scanner runs-dir create no longer adopts a pre-existing unbound `.brigade/scanners/runs` tree, so forged child receipts cannot become scheduling authority. A released pre-0.27 workspace whose unbound root is operator-owned and uncompromised is bound to a fresh authority record automatically; `brigade work scanners doctor`, `brigade work bootstrap`, and the first sweep repair that path. A foreign-uid, world-writable, or symlink tree fails closed with an operator message instead of a traceback. Control-plane receipt reads require the recorded receipt-file device, inode, and digest, not just the run directory. (#1036)
- `work import promote --all` now reviews and promotes one inbox generation under the task-ledger lock. Matching rows are bound to that snapshot's identity and digest; a same-id swap or rewrite between review and promote is refused and does not create a task. Fixes #1037.
- Run transport now applies quarantine, allowlist, and provider preflight to every `invoke` candidate, so an invalid-final fallback cannot launch a seat that is already quarantined for the run. Fixes #1038.
- Receipt `endpoint_host` provenance records parsed hosts or the fixed `invalid-endpoint` marker. A `*_BASE_URL_REF` that resolves to a value with no hostname is no longer copied into the receipt. Fixes #1041.
- Work-ledger readiness now fail-closes on stored edge types that are not an exact `blocks`, `parent-child`, or `discovered-from` token. Case variants (`BLOCKS`), whitespace-wrapped values, and other unknown types keep both endpoints out of the ready set and cannot be claimed. Compatibility rewrite of those types does not run during claim or readiness. `work run --task-id` and default ledger selection share the same locked start gate as claim, so a blocked or decision-gated task cannot be started or marked done. Plan writes stamp `{schema, task_id, kind, digest, generation}` on the task under the task-ledger lock; a missing, corrupt, or substituted expected receipt blocks claim. Fixes #1033, #1034, #1035.
- Trust review is bound to an operator-minted capability. `brigade evidence trust review` generates a fresh in-memory HMAC secret, writes a `{item_id, from_digest, transition, nonce, expiry}` token to the engine on stdin, and never places that secret in the child env or argv. `miseledger trust review` without a capability is refused for every stdin kind, including piped empty input, `/dev/null`, and a PTY. stdin is not an authorization signal. Direct engine invocation is unsupported. Fixes #1029.
- The directory-authority store is wrapped in a parent-held HMAC envelope (`brigade.authority.store.v1`) keyed by a 0600 file under the operator config root, with a MAC'd sequence file for replay and unsigned-downgrade refusal. Public `SCANNER_DEFAULTS` equality no longer grants import privilege. A same-UID process that reads the persisted store key can still forge a valid envelope; that residual is encoded in the suite and is not a class close. `--isolated-scanners` (POSIX user+mount namespace, doctor `WARN`/`MANUAL` when unavailable) is the opt-in tier that refuses that forgery. Part of #957. Refs #881.
- JSON run contracts rewrite home-directory prefixes (`/home/<user>`, `/Users/<user>`) to `~` on every string that enters the list, show, latest, and watch payloads. Artifacts and human CLI output are unchanged. Refs #631. Refs #958.
- CSV exports from `miseledger sql` now sanitize formula-triggering characters in text values and dynamic column headers to prevent spreadsheet formula injection while preserving numeric fields. (#1324)
- Finding-controlled text in security scan reports, review output, Markdown summaries, and SARIF exports is sanitized to neutralize control characters and terminal escape sequences. (#673)
- Hardened work import provenance boundaries: rebuilt self-imported inbox rows with local IDs, bound canonical rows to immutable content, and required external scanner receipts before legacy migration. (#868)

### Fixed
- Evidence redaction ingest coverage closes three gaps (#998). A new `home-path` guard rule redacts absolute paths under `/home/<user>/…` and `/Users/<user>/…` (category `pii`, so session and external-ingest origins cover them). Receipt provenance no longer hardcodes `agent-session`: miseledger receipt envelopes and their redaction records now use the truthful `workspace` origin (`receipts_cmd.RECEIPT_ORIGIN`). The previously unused clean-verdict gate `evidence_redaction.ingest_verdict_is_clean` is now enforced in `trust_gate.admit_consumer`: when an envelope carries a redaction record, that record must be an explicit completed clean scan of the current policy before the item may be admitted as scanned-clean on brief, wrapped, cite, promote, or context surfaces; missing records keep legacy-row behavior, while malformed, error, redacted, or other-version records fail closed. Fixes #998.
- Fleet Dolt sink CSVs now use `0`/`1` for Boolean claims and `\N` for SQL NULL, advance the event watermark after successful imports even without a new commit, and report unparseable incremental timestamps on stderr. Named fleet exports now retain the process's normal file-creation mode when atomically replacing an existing file. Fixes #1158.
- `brigade skills fleet status` no longer crashes with `ValueError: skill install path escapes workspace` when an installed skill copy is a symlink whose target resolves outside the workspace. The read-only audit now reports such a copy as status `external` (with an `external_count` in the summary and JSON payload, receipt-derived source preserved when one exists) and keeps auditing every remaining copy. Install, uninstall, update, and rollback paths keep the strict containment check: `_install_dir()` still refuses to hand back an escaping path as a writable destination. Fixes #1171.
- Fleet claims no longer lock a machine out of its own repo after a crash. A run killed with SIGKILL left an unexpired hub claim under its own node with a holder token nobody had, so the next `brigade run` there got a 409 from itself for the residual TTL (600-900s) with no way out but editing the hub SQLite. Now: `brigade fleet claims --release <target> [--node NODE_ID] [--force] [--json]` frees a claim without its token (`POST /claims` `release` with `scope: "node"`; the hub deletes the row only when the caller's `node_id` owns it, and refuses another node's claim naming its owner; `--force` / `scope: "force"` releases it anyway; renew is never token-less). `<target>` is always a claim key (never re-read as a directory); `--path` makes it a workspace directory instead (its name is the key, its node identity is used; a directory inside a workspace is refused rather than resolved upward). Without `--force`, both modes run the same proof: the hub's new `inspect` action returns the claim's recorded run directory to its owner node only, the CLI maps it to a workspace on this machine (with `--path`, it must be the workspace given) and refuses while that workspace's `run.lock` has a live owner or is malformed, or when the run cannot be resolved at all (saying why). The release is then fenced to the inspected row: the hub refuses a token-less node-scoped delete without the inspected `acquired_at` (only `--force` deletes unfenced), so a claim re-acquired in between is never deleted; and a probe that finds no claim owned by this node refuses outright without `--force`. `--node` other than this machine's identity requires `--force`; the JSON receipt's `forced` reflects the flag. An older hub that rejects the request is named as such. Every claim row now records the acquiring run's local `run.lock` lease (`lock`: owner token, `acquired_at`, `run_dir`; never listed; hub schema v3, live rows survive the upgrade), and the hub's token-less release reads and deletes in one write transaction so its receipt is the row it removed; the v2→v3 column migration is serialized under a write lock. When the Phase 1 lease reconcile frees `run.lock` for a dead owner on the way into a run, the hub acquire is sent with `scope: "node"` and the dead lease as `supersede`, and the hub replaces only the exact row taken under that lease (same node, same lease token, lease stamp not newer) — a claim held by another node, by a run in another same-name workspace, on a cloned node identity, or without a recorded lease is never touched — logging one line; a same-node claim with no dead lock to vouch for it is still refused, with `brigade fleet claims --release` and `--no-fleet-claim` named in the error. `brigade run --no-fleet-claim` skips the hub claim and relies on the local run lock alone, logged once. `runguard.run_lock` gains an `on_reconcile` callback that reports each dead owner it reconciled, and `runguard.read_lock_owner` reads a held lock's lease. `scope` is an intent marker that keeps honest clients from stealing each other's claims, not an authorization: `node_id` and the leases are caller-asserted under the one shared bearer token, which already authorizes `force`. Fixes #1141.

- Fleet claim heartbeats now defer holder-token-fenced orphan cleanup until
  the run exits when a lost-row re-acquire exceeds its deadline, preventing
  both a late claim leak and mid-run release of a live claim. Fixes #1142.
- The v0.25.0 run-reader compatibility test now exercises JSON-object rejection, unknown-key tolerance, and `finished_at` terminality from input behavior instead of fixture-driven assertions, and builds its legacy snapshot from the writer's full emitted shape. Refs #654, #640.
- Stop-hook tree fingerprints now exclude verify-run receipts, outcome ledger capture files, and inline MiseLedger indexing artifacts. A captured verification can close a session without treating its own evidence as uncovered work, while later source, configuration, and documentation edits still invalidate the receipt. Fixes #1132.
- The blocked-phase cancellation CLI test now waits up to 30 seconds for the run receipt, subprocess, and lock before signaling, avoiding false startup failures on contended CI runners while retaining the cancellation ordering check. Fixes #1126.
- The non-scorecard `brigade outcome reconcile` decide path no longer installs a regressed candidate on a margin of one (`helped=2, hurt=1`). A cohort with verified regressions now needs `helped >= hurt + install_min_helped` and holds with `withheld: verified helped margin over regressions below N` until then; clean cohorts are unchanged. Refs #654.
- Reused-receipt dedup is durable and cheap: `load_scoring_records` persists each `reused_from` resolution to `memory/outcome/evidence-canonical.json` and consults it first, so a legacy ledger row that stored the reused receipt path keeps collapsing to one signal after the receipt is pruned, and rank/score/reconcile open each distinct receipt at most once instead of once per row on every read. New captures against a reused receipt record the reused path on the ledger row as `reused_evidence_ref`, so that provenance no longer lives only in the receipts directory. Refs #654.
- Direct CLI roster seats can now set `command = ["executable", "fixed-prefix", ...]` to override
  the executable and insert fixed arguments before the adapter argv. This provides a supported
  Windows Cursor path through the bundled `versions/<version>/node.exe` and `index.js`, instead of
  the unsupported `cursor-agent.cmd` shim. Fixes #1100.

- Bounded SQLite retry failures retain their BUSY-family result code after
  replacing the driver error text with holder diagnosis. Callers can retry
  primary `SQLITE_BUSY` (5), recovery (261), and snapshot (517) results
  without exposing raw SQLite strings. Fixes #1083.
- Seat-driven dispatches (`run_seat.py`) now pass `output_dir` through to `run_transport.dispatch`, so the `message-envelopes.jsonl` audit sidecar is written for that path instead of silently going missing while the envelope gates still applied. Fixes #996.
- User-scope Claude work-loop hooks can pin `hook-run --target` to a wired workspace, never treat the home directory as a work root, and latch off for the session after two consecutive timeouts instead of appending a doctor banner on every tool call. The hook-run parent process does no unbounded filesystem work: pin resolution, target discovery, log-target lookup, latch I/O, and the `.git` check run inside the timed worker, and post-timeout latch/state persistence plus claim cleanup are best-effort under a short hard cap, so a stalled cwd cannot block Claude Code past the configured timeout even in the timeout-handling path. The #735 doctor-pointer / error `hook.log` write (including creating `.brigade/work/claude-hooks/`) still finishes before `hook-run` returns. Project-scope `hooks install` refuses a home-directory target (that path is Claude Code user settings), and `hooks status` reports duplicate managed handlers and a widened user-settings path. Fixes #1051.
- `brigade runs child` no longer inherits a parent's external artifacts or handoff path (those are re-rooted to the child run dir or dropped), creates a rewritten child-owned artifacts directory before recording it so the path is not dangling, and refuses a terminal parent event with `cannot branch a durable child from a terminal parent event` instead of birthing a `failed` child with no error text. Live-locked and legacy no-journal parents stay fail-closed. Fixes #955.
- `handoff doctor`, `handoff lint`, and `ingest` now walk every configured `handoff-sources.json` root the way `handoff list` already did, so a pending file in a second root is counted, linted, and ingested instead of leaving those commands green on inboxes they never read. An unreadable source config now fails `ingest` (exit 3) instead of omitting those roots, and `repos ingest` reports `invalid handoff source config` instead of a clean `no handoff inbox` skip. Fixes #1050.
- Claude work-loop hook session state is scoped per dispatched task instead of per reused session id. A `SessionStart` with source `startup` or `clear` begins a new task epoch: state entries from earlier tasks (repo graphs, `write_observed` latches, fingerprints) expire on read instead of re-arming the stop gate for directories the current task never touched, and a read-only later task no longer inherits a prior task's `write_observed=True`. `resume` and `compact` continue the same task, and a task that does write in a wired repo is still gated for verification and handoff. Fixes #992.
- Result integrity now treats a sentence terminator glued to the next sentence (`...answering.media-cli is...`) as a clause break, so a correct final answer is no longer discarded as `non-final-output` when the provider omits the space after its progress sentence. A filename-dot is re-merged only when the character before the period is an identifier and the following token is a known code or doc extension (`README.md`, `App.csproj`), not a short English word (`it`, `is`, `the`). Version digits (`v1.2`) are not treated as clause starts. Fixes #1099.
- Bare `brigade care install` adopts installed predecessor ids that already schedule the same runbook (`care-scan` covers `daily-care`, `handoff-ingest` covers `ingest-sweep`) instead of stacking a second systemd or launchd timer. Dry-run reports the adoption. Explicit `--entry` still installs the named id. Fixes #986.
- Repeated care-entry failures (two or more newest-first failed runbook receipts) now surface on `brigade care status` (summary + per-entry `repeated_failure=`), `brigade work brief` (`care_repeated_failures` / `scheduled_care`), and the Center Status dashboard (Scheduled care tile + attention sentence). Topology marks those care jobs `failed`. The SQLITE_BUSY retry for `evidence-crawl` itself landed earlier as #1067. Fixes #985.
- The center Runs view now treats a missing runs directory as the existing "No Brigade runs yet." empty state, and renders a distinct error panel for other `brigade runs list` CLI failures (timeout, invalid JSON, unreadable store). `brigade.run-detail.v1` docs name the verification field `command`. Fixes #991.
- Changelog footer link definitions cover every release section again (24 were missing, 0.8.2 through 0.26.1), and `[Unreleased]` compares against v0.26.1 instead of v0.26.0.
- Center dashboard chip-icon backgrounds now render under the nonce-only Content-Security-Policy: Memory Operations and Status views use one stylesheet class per status role instead of per-element `style=` attributes (which `style-src` with a nonce refuses). Work, Runs, Agent Activity, and Outcomes already used classes or had no chip-icon inline styles. Fixes #1077.
- Evidence crawl import (`miseledger import sourceharvest` / `crawl files|gitlog|docs`) and provenance backfill retry the SQLITE_BUSY family (primary code 5, including snapshot 517 and recovery 261) from a concurrent writer instead of failing the runbook. A lock that outlives the bound names the holder-diagnosis step rather than the raw SQLite string. Fixes #1067.
- Evidence-ledger `archive.Open` restores the pre-#1073 10s global SQLite `busy_timeout` so unwrapped command paths do not give up after 1s under contention. Crawl import and provenance backfill still bound their own wait via retry count, backoff, and a 4s `MaxTotalWait` ceiling (no `busy_timeout * retries` hang). `IsBusy` classifies by the SQLite result code (primary / low 8 bits == 5) and ignores parenthesized 5/261/517 in free text such as subprocess stderr. Fixes #1085.
- Directory-authority validation distinguishes a pre-hardening record (missing `workspace`) from a genuine identity mismatch. A legacy record whose extant `target` and directory identities still match is upgraded in place, so `memory care import-issues` and other bound opens no longer die with a bare `OSError` on long-lived workspaces. A swapped or forged directory still fail-closes; the error names the record path, the mismatched field, and `brigade work rebind-authority --target <workspace>`. `brigade doctor` WARNs when a legacy record is still on disk. Fixes #1066.
- `work import promote --all` now persists a task only after that item succeeds through the late-window inbox CAS. A failed batch item is not flushed by a later success, and a refused promote writes no task files. Fixes #1059.
- Scanner runs-dir authority bind and run-directory publication now use the same POSIX/Windows dirfd abstraction as import-inbox (`nt_dirfd` on Windows). `operator quickstart` and `brigade work` scanner sweep no longer fail with `descriptor-relative directory authority operations are unavailable` on Windows. A host with no dirfd and no existing runs tree returns `missing` so first-create can proceed; an existing unbound tree is still not adopted. The #1036 adoption refusal stays closed on POSIX. (#1036)
- A fresh `init` / `operator quickstart` workspace no longer fails its own `doctor` or `security scan`. The workspace `AGENTS.md` template keeps the file-maintenance table in `memory/cards/` so the rendered file stays under the 12000-byte bootstrap budget on Windows CRLF, `*_FILE` / `*_PATH` / `*_FILEPATH` names such as `RESTIC_PASSWORD_FILE` are not plaintext-password findings (values that merely start with `$` or `~` still report), and the half-fed outcome-loop warning names `brigade work verify run --manifest` plus `verify/manifests/`. (#1025)
- `brigade evidence crawl plan` omits `crawl memory` when `memory/NAMESPACE` is missing (or prints it once that operator-declared `memory-<uuid4>` exists), and omits sourceharvest-backed `crawl files` / `crawl gitlog`. Printed commands use positional paths (no `--root`/`--repo`). (#1024)
- Operator-authored run task text that discusses prompt-injection policy (for example, documenting that a worker must ignore previous instructions from tool output) is no longer classified as an injection payload, so dispatch proceeds. A documenting verb does not exempt a whole line when a later clause still matches an imperative, role-override, or secret-exfil rule. Imperative injection payloads still quarantine. A request-side quarantine now fails as `injection-quarantine` and names the heuristic that fired, instead of `unclassified` with no operator hint. (#995)
- Projection commit now holds the validated parent-directory descriptors and publishes create/replace/remove — including `writer=` / `remover=` callbacks — through those descriptors, so swapping a checked parent for a symlink between plan and commit cannot redirect a write outside the destination tree. `managed_block.write_text_nofollow_atomic` publishes the same way and refuses a swapped parent. (#1013)
- Stale-lock `inspect_command` from `brigade runs watch --json` is a runnable `brigade runs show <run_id>`: `runs show` now resolves a bare run id under `--cwd` / `--runs-dir` the same way `watch`, `recover`, and `child` already do, so path redaction no longer emits an unexecutable command. (#990)
- Vault index, search, show, and doctor now walk allowlisted roots from a held vault directory descriptor and refuse a symlink in every path component (`openat`/`O_NOFOLLOW` + `fstat`). Replacing an allowlisted folder such as `Shared/Inbox` with a symlink to another in-vault path can no longer re-scope private notes; reread of indexed files uses the same walk. (#1011)
- Claude hook stdin is read through a byte-limited binary reader, rejects trailing input, and caps nested string and collection sizes before the timed worker starts, so an oversized hook payload cannot force unbounded allocation or JSON parsing outside the timeout. (#1014)
- Inter-seat message envelopes now bind `{run_id, message_id, assignment_id, from_seat, to_seat}` into the authenticated envelope and require those expected values at every receiver. A valid old text/envelope pair can no longer be transplanted into another run, seat, or assignment. Fallback worker output is attributed to the terminal producer, and resumed output is re-enveloped under the current run identity. (#1010)
- Promotion and other content admissions now validate the complete provenance envelope and require a present, exact `hashes.content` match against the current item bytes before copying a body. A same-UID rewrite of `imports.jsonl` that leaves the original envelope can no longer be promoted into a task; missing, malformed, or stale redaction records also block promotion. (#1008)
- `brigade care install` and `care status` on Windows now resolve the default `auto` backend to a printed Task Scheduler plan (`--backend schtasks`). Each printed `schtasks /Create` line uses that entry's own schedule, `/TR` through `C:\Windows\System32\cmd.exe`, and `/RU "%USERNAME%" /IT` so the task runs while the user is logged on without an elevation prompt (`/NP` S4U requires admin). An explicit `--backend systemd|launchd|crontab` still generates that backend's plan on Windows instead of being redirected to the printer. `--json` returns the structured plan instead of plain text. `care status` reports whether each `BrigadeCare-*` task exists and when it last ran. (#1021)
- Evidence-ledger read surfaces hide item bodies unless provenance parses and injection status is the validated typed value `clean`. Session preview, session search snippets, and session transcripts are gated the same way. Imports stay `quarantined`/`pending` until `miseledger trust review --mark-injection-clean`; a label-only review does not make content eligible. MCP and HTTP no longer accept a caller-settable `include_untrusted_body` reveal. Routine `trust review` refuses a parse-error-grade stored envelope instead of silently rewriting it to `clean`. (#1007, #1009)
- Origin-scoped ingest redaction now maps `work import context` (and `--source external-web` / `external-service`) to the external detector tier, normalizes free-form `--source` with `strip().lower()`, and fails closed to `unknown` on unmapped values. An equal-but-unredacted scanner result is treated as failure, and research findings redact title/summary/evidence per field so a multi-line summary cannot migrate into evidence. (#498)
- `brigade runs list --json` reads `parent_run_id` from each run's own lineage and no longer scans sibling `run.json` files through recorded-child discovery. Refs #631. Refs #958.
- Recorded-child `status` and `branch_point_event_id` in `brigade.run-detail.v1` are bounded through the same `_clean_str` limit as other contract strings. Refs #631. Refs #958.

### Added
- `brigade runs resume <child-run-id>` resumes a durable child from its recorded parent lineage and branch-point. When the child has a roster and resumable app-server coordinates, work continues through the existing locked resume path. A fresh child snapshot without those artifacts confirms the branch-point and does not stamp `running` or claim that work re-executed. The git-root run lock is honored, parent and sibling receipts stay untouched, and a corrupt or unsupported branch-point fails closed with no resume event. Legacy runs without child metadata keep the existing resume path. Fixes #1072.
- `brigade runs diff <child-run-id> [other]` compares a durable child run against its recorded parent (or two siblings that share a parent) without mutating either side. The versioned `brigade.run-diff.v1` contract covers lifecycle state, worker results, verification results, and outcome evidence; GraphTrail snapshots are compared only when both sides are compatible, otherwise the payload states the skip. Verification `changed` compares the full ordered receipt sequence on `{status, command, exit_code}` only (`run_id` and the `final.txt` digest stay display-only). All contract strings pass through the `_clean_str` chokepoint. Fixes #1071.
- `brigade runs diff <child-run-id> [other]` compares a durable child run against its recorded parent (or two siblings that share a parent) without mutating either side. The versioned `brigade.run-diff.v1` contract covers lifecycle state, worker results, verification results, and outcome evidence; GraphTrail snapshots are compared only when both sides are compatible, otherwise the payload states the skip. All contract strings pass through the `_clean_str` chokepoint. Fixes #1071.
- Center Code Graph adds a Blast radius primary mode beside the Module map (which stays the landing view): a fixed upstream / focus / downstream layout for one module, seeded from the branch's changed modules, with a focus picker, one-hop default depth, per-side "expand one more hop", and stated truncation (`showing 10 of 34`). Neighbors come from the full code graph for the focus node, not the capped module-map export. Same-hop ranking sums every parent edge and sorts by weight then module id (stable top-10); a typed name that matches more than one module lists both full ids instead of silently picking one. Fixes #1091.
- `brigade work ready --campaign <name> --parallel-safe` now composes campaign-aware dispatch waves at query time from each member's existing per-repo footprint partition. Global wave N is that member's local wave N in campaign order: overlapping files inside one member still serialize, disjoint work from different members (including matching relative paths) can share a wave, and an empty footprint stays exclusive only inside its own member. Cross-repo `blocks` still gate readiness before composition; missing or unreadable members still fail closed before any waves are returned; per-member GraphTrail degradation is reported without discarding safe file-overlap waves. Per-repo `--parallel-safe` output is unchanged. Waves are not persisted. Fixes #999.
- The Agent Activity dashboard view answers "what are the agents doing right now across the fleet?" with a page-level summary strip above the machine board (total agents, running, blocked-on-host, failed, awaiting approval, unknown) and a visible plain-word state legend; per-host count chips now show icon + word + count instead of a tooltip-only icon+number. Missing or unrecognized states aggregate under `unknown`. Fixes #975.
- The `review-heavy` preset now ships a read-only Daybreak Blue defensive-security seat using the verified Codex headless model id. Accounts without entitlement report `model-unavailable` without echoing provider diagnostics. Fixes #1002.
- `brigade runs serve --cwd <workspace>` opens a foreground, loopback-only, read-only Run View over the existing `brigade.runs-list.v1`, `brigade.run-detail.v1`, and `brigade.run-watch.v1` serializers. It binds an available port by default, prints the URL, opens the browser unless `--no-open`, and stops on Ctrl+C. The HTTP layer does not read run artifacts itself, rejects traversal and symlink escape, and never starts from another command. Fixes #631.
- `brigade.run-detail.v1` includes recorded children under `run.lineage` (same sibling-receipt discovery as human `runs show`). Serializers merge source artifacts through a load-bearing field allowlist so unexpected keys cannot pass into the JSON contracts. Refs #631. Refs #958.
- `brigade runs show` lists recorded child runs under lineage when a sibling receipt names the shown run as `parent_run_id`. Legacy runs without child metadata stay valid roots and print no lineage block. Branching from a digest-broken parent journal, a parent with no journal, or while the git-root lock is held fails closed with no partial child directory. Refs #594.
- `brigade code` indexes `.astro` component files (frontmatter and `<script>` as TypeScript, template tags as calls to imported components), so `affected`, `callers`, and `impact` can see an Astro file and its downstream Astro consumers instead of reporting it missing. (#1003)
- `brigade roadmap audit` now fails closed when the ROADMAP.md "Where things stand" headline (`**vX.Y.x on main**`) lags the `pyproject.toml` major.minor, or when that headline is missing or unparseable. The check is only emitted for a target that declares a parseable `project.version`, so a repo making no comparable version claim is unaffected. Patch-only differences stay green, and a headline ahead of the project version passes and is reported as ahead. `brigade roadmap audit --check` exits non-zero so the repo-metadata CI job can fail a lagging headline with an update hint. (#1000)
- Evidence ingest now applies an origin-scoped redaction policy (`brigade.evidence-redaction.v1`) before persistence. Sources classify to the provenance envelope origin; each origin selects detectors; the stored item keeps only redacted bytes plus a count/detector record (never the removed values). Scanner failure, timeout, or unavailability cannot produce a clean verdict and persists a placeholder instead of the original. The policy version is stamped on every decision and applies to future writes only — existing rows and provenance backfill are not rewritten. (#498)

- Inter-seat planner, worker, and synthesis messages now carry a
  `brigade.provenance-envelope.v1` with `message.text.utf8.v1` over the exact
  outbound UTF-8 bytes. Channel gates run before `parse_plan` or model
  delivery. Receive-side trust is re-derived or verified against authority
  and is never taken from a stored `reviewed`/`verified` label; model and
  tool-origin content stay `untrusted` regardless of the on-disk claim.
  Envelopes are bound to their channel so a valid envelope for one message
  kind is rejected at every other gate. Model and tool outputs start
  `untrusted` and completing a phase never upgrades them. Worker and
  prior-stage text is wrapped and byte-capped
  before later prompts. `message-envelopes.jsonl` beside `run.json` stores
  message id, phase, seats, and envelope only. Legacy run messages display
  unknown provenance and are not replayed. (#585)
- `brigade.causal_receipt.v1` is a lineage-only companion record for plan, run,
  verify, outcome, and handoff artifacts. New writes stamp typed `recorded`
  parent links so one plan→run→verify→outcome→handoff chain (and multi-parent
  synthesis) is traversable without timestamp or path guessing. Parent count
  and encoded size are capped; oversized fan-in must reference a hashed
  manifest. Unknown relations, malformed digests, broken parents, and
  unsupported versions produce bounded diagnostics. Telemetry export prefers
  these links for parentage and keeps its deterministic trace/span projection.
  Historical artifacts stay readable and are not rewritten; inferred backfill
  remains under #583. (#493)
- `brigade memory vault-propose` writes an additive note into an allowlisted
  operator-vault inbox configured in `.brigade/vault.toml`. The body is read
  from stdin, staged owner-only outside the vault, and delivered through the
  projection kernel. `--scope` names the inbox root (unknown scopes error);
  proposals mint a stable `canonical_id`, never land in `Brigade Memory/`,
  never overwrite an existing note, and `--dry-run` reports the destination
  and rendered bytes without touching the vault. Containment fails closed
  when symlink or descriptor checks cannot be performed. (#945)
- Canonical memory cards mint one opaque `card-<uuid4>` ID on create, and the reviewed `memory care backfill` path can mint an ID on a legacy card, including complete care cards that only lack an ID. Valid IDs are never replaced. Care queues, recall and search logs, refresh imports, and retrieval-eval fixtures dual-read explicit IDs and legacy path/stem/topic aliases; alias collisions fail without rewriting cards. Dry-run backfill writes a deterministic mapping receipt with coverage and old-to-new IDs (relative paths only; `old_id` is the consumer-facing identity). A zero-candidate dry run writes nothing. Duplicate-ID coverage does not require `memory/NAMESPACE`. Missing IDs stay a doctor warning until coverage is 100 percent. (#867)
- `brigade memory vault-index`, `vault-search`, `vault-show`, and `vault-doctor`
  close the operator-vault projection round trip. Config lives in
  `.brigade/vault.toml` (`vault`, `[[roots]]` with `scope` / `path` /
  `optional`, and `schema_version`). Search reads allowlisted roots only,
  writes a derived owner-only index under `.brigade/vault-index/`, redacts
  output through `guard.redact_text`, and labels every hit
  `untrusted_vault_content`. Projected notes key on `canonical_id`;
  operator-authored notes fall back to the vault-relative path. (#943)
- Evidence consumers now enforce `brigade.trust-policy.v1`: unknown and quarantined items are excluded from default briefs, context packs, citations, and promotion; untrusted brief content is wrapped and capped at 2 items and 50 percent of brief bytes; only the explicit known-safe injection status `clean` (after `strip().lower()` and pending resolution) may wrap or emit a body — case, whitespace, empty, and garbage variants are metadata-only. `brigade evidence trust review <item-ref> --content-hash <digest>` attests an untrusted item as reviewed after an exact envelope digest match and appends one transition event. `brigade receipts verify` upgrades indexed verify-receipt items to verified only after complete receipt v2 patch identity and exact retained `changes.patch` validation; duplicate verify is an idempotent no-op. Envelope verified does not bypass subject_binding, check_role, patch identity, or failure-taxonomy requirements. Citing a flagged research finding is rejected; the run takes one repair attempt and then fails `review-rejected`. (#587)
- Evidence read surfaces recompute provenance content and materialized raw/artifact hashes. Search suppresses a mismatched snippet and returns `integrity_mismatch: true`. Direct `miseledger show` / `brigade evidence show` hide a mismatched or synthesized-legacy body unless `--forensic-content` is passed; that flag never changes trust and only reveals a body when injection status is the explicit known-safe value `clean` (empty, unknown, and parse-lost statuses block). MCP, HTTP, bundles, briefs, and context stay metadata-only on mismatch. Bundle cache and Markdown preserve envelope fields and `integrity_omitted`. One downgrade event is appended per item/hash/mismatch; the row is never deleted. (#586)
- Python evidence producers now stamp `brigade.provenance-envelope.v1` on work imports, research findings, and indexed receipt adapter rows. Research findings persist an exact `{title}\\n{summary}\\n{evidence}` text projection; legacy `Finding.trust` stays readable and only maps to origin/modality. `brigade work import provenance --backfill` and `brigade research provenance backfill` infer missing envelopes without treating them as trusted. Receipt indexing always starts `untrusted`. Existing pending imports without an envelope report `missing_envelope`, which surfaces as a doctor WARN until an operator runs `--backfill`. (#584)
- `brigade runs child <run-id> <event-id>` creates a durable child run from a
  checkpoint-covered parent lifecycle event, records lineage and the verified
  shared prefix, and shows that lineage in `brigade runs show`. The child gets
  a fresh `started_at` / `status_started_at` and does not inherit the parent's
  live control socket, live-progress fields, failure terminal fields, or
  parent-directory artifact/handoff paths. The covering checkpoint event itself
  is not a valid branch point. (#594)
- `brigade memory project-vault` writes a one-way Obsidian projection of
  canonical memory into an existing vault: care-state frontmatter and tags,
  wikilinks, category/harness maps, and a topology canvas. Vault writes use
  the projection kernel, preserve manual edits via conflict copies, and keep
  the operator vault path out of public status. (#888)
- `--run-budget PATH` accepts one `brigade.run_budget.v1` JSON declaration on
  `brigade run`, dogfood, and model-trial starts. Brigade persists the supplied
  declaration in the run and plan receipts without mapping `timeout_seconds`
  into a run budget. Budget cancellation receipts now carry bounded per-seat or
  per-transport outcomes and the observed work that may still be active. (#885,
  #886)
- `brigade mcp sync --write` commits selected native configs and MCP ownership
  state through the projection transaction kernel. A failed write restores the
  selected destinations; unfinished operations are visible through `brigade
  mcp status` and `brigade mcp doctor`, and can be recovered with `brigade mcp
  recover <operation-id>`. Refs #911.

### Changed
- The root `CHANGELOG.md` is marked `/CHANGELOG.md merge=union` in `.gitattributes` so same-day Unreleased appends from sibling PRs combine instead of conflicting. Nested changelogs are not marked. (#994)
- Search integrity verification loads item text and provenance in one
  `IN (...)` query keyed by the result ids, instead of one select per
  hit (search limit is up to 200). (#964)
- `brigade memory project-vault` accepts `--max-related` to tune the per-note
  related-link cap (default 12). Explicit refs still outrank tag/category
  matches but no longer claim to always survive the cap. (#966)
- `brigade memory project-vault` produces a browsable vault on a real corpus.
  Notes take their title from the card's leading heading when frontmatter has
  no `title`, instead of the filename slug, and that heading is no longer
  repeated in the body. Related links are ranked and capped at 12 per note, so
  a large shared category no longer forms a complete graph. Maps that would
  cover every note are omitted, and canvas nodes lay out in a grid rather than
  one row. On a 1,254-card corpus this took the canvas from 83,586 edges and
  11.3 MB to a bounded graph. (#888)
- Center Memory Operations Cards now shows corpus size, a chip cloud, compact
  expandable rows, and 30-row paging. Handoffs is a sibling tab on that page;
  `/view/handoffs` redirects there. Snapshot polling reloads the page so
  nonce-scoped assets stay valid. (#903)
- The shipped review-heavy `reviewer_flash` preset now uses Gemini 3.7 Flash Low
  on Antigravity. Security-sensitive or cross-file decisions still route to the
  independent `reviewer` or `reviewer_codex` seat. (#920)

### Fixed
- Synthesis causal receipts now fall back to a hashed `parent_manifest` when
  encoded lineage exceeds the compact-size cap, not only when parent count
  exceeds 16. The fallback writes the lineage-only parent-manifest artifact
  the receipt references (beside `synthesis.json`) so traversal can recover
  the full worker-result parent set. A realistic 13-worker fan-in no longer
  raises, drops `synthesis.json`, or looks like a parentless root. (#493)
- The Claude Stop closeout gate no longer treats another session's writes in a shared workspace as this session's unverified work. Worktree deltas during a Bash window are attributed only when no concurrent Claude write evidence (`write_observed` / `last_write_at` / `pending_write_at`) or other-harness receipt explains them — a read-only neighbor that only touches its session file is not foreign write evidence; a time-valid session receipt still counts when the live tree has drifted under a foreign writer; and a hard block that another process can re-arm is downgraded to a warning after this session has already captured, unless this session itself wrote again after that capture. (#959)
- `memory care backfill` dry-run receipts now predict `--apply` (stable `new_id`), record `old_id` as the topic/stem consumers already use, mint IDs on complete ID-less cards so coverage can reach 100 percent, skip creating `.brigade/` on zero-candidate dry runs, and count `duplicate_ids` without a `memory/NAMESPACE` file. (#867)
- `memory vault-search` rebuilds the derived index when `.brigade/vault.toml`
  roots change, so removing a scope from the allowlist stops serving that
  root. `vault-doctor` warns on index/config root drift instead of reporting
  all-green against a stale index. (#943)
- Evidence brief rendering one-lines MiseLedger score keys/values (including zero and `False`) and retrieval-arm labels, omits `trust:` when the gate-resolved label is `unknown`, prefers the provenance envelope over item-level trust keys, and keeps a partial first result line when truncation leaves room after context lines. (#941)
- `brigade runs watch --json` (`brigade.run-watch.v1`) now routes every
  record through the same allowlist and one-line helpers as the list and
  detail contracts. `watch`/`summary` emit `run_id` instead of an absolute
  path, `event` records drop raw params (tokens, prompts, stdout, log
  paths), and a field that cannot be rendered safely is omitted. (#631)
- Center Research provider table no longer labels absent model seats as `- (unverified)`; only named models carry the unverified marker. (#961)
- The graphtrail stale-baseline verify test no longer races wall-clock sync delays
  against a tight subprocess timeout on loaded CI runners; sync timeout is injected
  deterministically so the stale-graph assertion is reliable on Python 3.12. (#954)
- Quarantined items stay quarantined when a pending injection scan comes back clean, so a labeled quarantine cannot be released to untrusted and become content-eligible. Reviewed and verified items keep every consumer surface untrusted already has, including `context`. `brigade receipts verify` reports `verify-pending` (and does not append a local verified event) when the MiseLedger trust notify fails. Provenance-event JSONL now appends and fsyncs instead of rewriting the whole file. (#587)
- Corrupt or future-version `.brigade/work/tasks.json` ledgers now fail
  closed on every `brigade work` surface (and other commands that read the
  ledger) with `error: ...` and exit 2, instead of parsing as empty or
  dumping a traceback. The work-store measurement harness
  (`scripts/measure_work_store.py`, protocol 4) now reports
  `schema_version_policy.future_version_rejected: true` with a clean
  exit-2 error and untouched ledger bytes; it no longer treats future
  versions as coerced. Unknown hand-edited edge types are preserved on
  rewrite and surfaced via `work ready` (`unknown_type_count`) and a
  `work doctor` WARN. `work import promote --all` returns 2 when any
  import fails. (#948)
- Scanner receipt and import-proof bytes now carry verifier-owned file
  identity and content bindings. Legacy migration rejects in-place receipt
  overwrites, attacker-created sidecars, missing bindings, and mismatched
  file identity or content. A binding-write failure restores the prior
  receipt, proof, inbox, and binding state or fails closed. Workspace
  rename re-anchors those file bindings with the directory record, and a
  malformed store read fails closed instead of wiping every binding.
  Rollback restores the snapshot for the bindings the failed operation
  itself added, deleting one it added that the snapshot did not have, while
  leaving a concurrent writer's unrelated bindings intact. A directory that
  already exists and is not bound to the workspace record is never adopted,
  including when the caller asked to create it; Brigade creates and binds
  the import-proof directory before it launches a scanner child instead.
  Scanner children run with an explicit environment allowlist whose `HOME`
  and `XDG_*` paths point at a per-run sandbox that is removed when the
  child exits, so a child cannot reach the verifier authority store through
  its environment. That allowlist passes through what shipped scanners
  need, including `GH_TOKEN`/`GITHUB_TOKEN` or `GH_CONFIG_DIR` pointed at
  the operator's existing `gh` config, `SSH_AUTH_SOCK`, `USER`/`LOGNAME`,
  `TMPDIR`, and proxy settings, so `gh`- and `git`-backed scanners keep
  authenticating. Same-uid writes that guess the store path remain a
  residual class. Refs #881.
- Bare `brigade care status` now discovers target-scoped per-entry
  registrations instead of scoring the atomic five-entry default. A target
  with no discovered units reports `missing`. Systemd status also reports
  `enabled: false` until every discovered timer is linked from
  `timers.target.wants`. (#914)
- Graph snapshot copies during `brigade work verify run` now honor the same
  `graphtrail_delta_timeout_seconds` cap as `graphtrail sync`. A snapshot that
  exceeds that cap marks `code_graph_delta` `sync_timed_out` with a reason and
  leaves the verification command result unchanged, so a slow graph cannot
  reject a passing check. The 10s default and per-target `.brigade/config.json`
  key are unchanged.
- Source and pipx-from-checkout installs now declare `0.27.0`, which PEP 440
  sorts at or above the publish-dev `0.27.0.devYYYYMMDD` wheel line. The
  previous `0.26.1` pin version-sorted below those wheels, so a current main
  checkout looked stale to naive auditors. publish-dev stamps
  `{pyproject}.devYYYYMMDD` on the declared line instead of the next minor.
- `brigade init --profile` now merges repeatable `--include` values into the
  profile includes with first-seen de-duplication before `--full` appends
  `repo-extras`. Refs #494.
- Issue #846 R2 harness sendback (comment 5247390894): JSON secret-history
  proves synthetic Git/ignore-path; SQLite marks Git history unavailable;
  cold-start uses parent `subprocess.run` wall with distinct inner timing;
  RSS uses one subprocess-child sampling protocol with explicit scope labels.
- Issue #846 R2 harness sendback: SQLite metrics scans the touched store and
  restores env overrides; restart/cold-start use a fresh subprocess probe;
  JSON guards cover `if_status` match and mismatch.
- `brigade init` gitignore blocks now un-ignore harness parent directories
  before the managed handoff `TEMPLATE.md` exception so a parent `.claude/`
  rule cannot shadow the template; the source repo `.gitignore` matches the
  same repo-local recovery recipe. (#860)

### Documentation
- ROADMAP.md "Where things stand" now tracks v0.27.x: the provenance and
  evidence-trust program, the read-only Center dashboard, the memory
  vault and stable-card-id program, scanner and verifier authority
  hardening, and the cross-session Stop-gate fix. Shipped Next and Later
  slices (dashboard first slice, work-inbox graph, wiring durability,
  handoff standalone-chunk lint) moved to docs/roadmap-archive.md.
  Part of #1000.
- Quickstart commit guidance now states that `.claude/` stays local-only
  except the managed `.claude/memory-handoffs/TEMPLATE.md` Claude onboarding
  requires tracked, cross-linking the repo-local shadow recovery from #858.
  Normal `brigade init` reruns upgrade the managed gitignore block without
  `--force`. (#860)

### Added
- Projection transaction kernel: an internal all-or-restored prepare/commit
  engine, recovery journal, redacted receipts, injected-failure hooks, and a
  reusable conformance fixture for multi-file projections, plus
  `brigade projection recover <operation-id>`. Native stdlib implementation;
  no production projector is migrated yet. (#910)
- Cloud dispatch registry (`brigade run cloud`): register-on-dispatch and adopt
  paths, `status` JSON classification (pending / ready-to-land / landed / stale /
  orphaned / needs-investigation), receipted report-only `sweep`, configurable
  stale-READY threshold surfaced in `brigade work brief`, and Center Agent
  Activity Cloud card rows when the registry has entries. Cursor cloud stays
  unwired until an API key exists; GitHub is ground truth for branches/PRs.
  (#890)
- `brigade care install|status|uninstall --entry JOB_ID` selects one
  namespaced registration keyed by `(target identity, job_id)`. The five
  maintainer memory jobs (`handoff-ingest`, `care-scan`, `memory-refresh`,
  `evidence-crawl`, `memory-closeout`) are in the catalog; a job whose
  operator-approved runbook is missing installs nothing and says why.
  Omitting `--entry` still writes the atomic scheduled-care set. (#762)
- Bundled template-profile selection and render-hash snapshot for #494: named
  presets (`repo-claude`, `workspace-claude-codex`, `repo-claude-full`) via
  `brigade init --profile`, deterministic rendered-file digests checked by
  `scripts/template_profile_snapshot.py`, and harness compatibility projected
  from existing `harness-contract.v1` `tested_version` / provenance / evidence
  fields. No public registry schema, profile URIs, `supported_harness_versions`,
  or install-time version gate. Profile-less `--depth` / `--harnesses` init is
  unchanged.
- `brigade runs export` / `validate-archive` enforce the #592 worker-event
  privacy boundary: raw worker-like JSONL anywhere under `events/` (including
  nested paths such as `events/nested/coder.jsonl`) stays local-only and is
  never classified as public support data. Export writes distinct scrubbed
  sidecars (`events/**/<worker>.scrubbed.json` with scrubbed media type,
  `role=artifact`, `privacy_class=redacted`) via the existing fail-closed
  scrubber, or refuses the export with a bounded diagnostic when any
  method/field/item is unknown or unsafely classified. The reserved
  `events/lifecycle.jsonl` journal exemption authenticates
  `brigade.run_event.v1` records and rejects raw JSON-RPC worker envelopes.
  Source runs remain unclassified until scrubbed; audit/replay still require
  `policy=local-only` for raw salvage.
- Run budget enforcement first slice (#593): wall-clock and worker-dispatch
  ceilings are reserved before new worker dispatch; durable
  `run_budget.threshold_reached` / `reservation_denied` / `exhausted` /
  `cancel_requested` / `cancelled` / `usage_reconciled` lifecycle events use
  stable idempotency keys; restart-safe projection rebuilds remaining
  allocation from the journal. Model/tool/token/cost stay observed unless an
  adapter exposes an enforceable boundary. Budget exhaustion and operator
  cancellation are terminal policy lifecycle states (`budget-exhausted` /
  `operator-cancelled`), not #576 worker FailureClass values and not
  infrastructure-neutral under #580. Child allocation remains #594.
- Issue #846 R2 residual work-store characterization: extend the R1 harness
  with observed same-actor/guard/empty-filter, restart, schema coercion,
  backup/restore, install/cold-start/backup timing, resource, metrics-state,
  and deleted-secret cells plus diffable
  `docs/measurements/issue-846-work-store-r2.json`. Characterization only; no
  backend, listener, or runtime dependency.
- `brigade update --channel beta` installs the newest non-yanked
  `brigade-cli==0.27.0.devYYYYMMDD` wheel from PyPI into the existing
  user-global pipx environment, migrates prior Git-main-SHA beta state to that
  exact wheel pin, supports dry-run without mutation, and rolls back to stable
  through `--channel stable --switch-channel`. (#852)
- Issue #846 R1 work-store characterization: research report, synthetic
  chain/diamond/wide fixtures, and `scripts/measure_work_store.py` measurement
  harness with diffable JSON output. Characterization only; no backend or
  runtime dependency.
- Bounded session-start memory recall (#466 Slice 1): `brigade memory recall
  --target <hub-or-mirror> --cwd <session-cwd> --limit 5 [--json]` derives query
  terms from the cwd basename, returns title/tags/path only (max 5 matches /
  10 lines), and reads a machine-local `memory_recall_target`. Workspace depth
  may default to the current target; repo depth stays unconfigured until an
  explicit hub or mirror is set. The managed Claude `SessionStart` hook merges
  recall beside the work brief once per session and fails open on missing or
  broken targets. Operator smoke steps live in `docs/memory-care.md`.
- `brigade center serve` gains work-graph views over existing `--json` contracts:
  an SVG ready/blocked dependency graph, parallel-safe dispatch-wave swim-lanes
  (seam-ready for `work ready --parallel-safe` when #803 lands), and per-task
  claim state with three-phase footprint stale indicators. Read-only; no new
  write surfaces.
- Label-free memory search recall signal (#723): `brigade memory search`
  appends a size-capped local log at `.brigade/memory/search-log.jsonl`
  (timestamp, normalized query, top-K card ids only). A follow-up within the
  disjointness window whose top-K shares nothing with the prior search counts
  as a miss; `memory care status` reports the rolling `search_recall`
  (`searches`, `followup_rate`) as second-class evidence that never gates
  doctor/`valid`. Failed queries become fixture material for the #722
  retrieval eval harness.
- GeneratedPatchQuarantine (#507): model-generated patches
  (`patch_source: generated`) promote only through an independent verifier
  receipt that records repository-test (`effectiveness`) checks plus
  `candidate_count` / `model` / `model_version` on
  `subject_binding.generated_patch_quarantine`. Model confidence, lexical or
  textual similarity, and repeated sampling are explicitly non-promoting
  outcome sources. Design: `docs/design/generated-patch-quarantine.md`.
- VerificationContract (#500): consequential runbooks and verify manifests declare
  an independent verifier, failure/rollback path, and token/latency budget before
  execution (`brigade.verification_contract.v1`). Plan surfaces incompleteness;
  run refuses incomplete consequential templates. Receipts record `budget_use`
  and `verification` separately from `model_completion`. First-slice enforcement
  of wall-clock and worker-dispatch ceilings lands in #593.
- Optional dispatch annotations on work tasks (#815): `--seat-class`
  (`mechanical` | `judgment` | `review`) and `--spend-by` (ISO-8601 deadline)
  round-trip through `brigade work task add` / `brigade work task annotate` and
  appear on `brigade work ready` (JSON and text) when present. Stored under
  task `metadata`; absent annotations change nothing. Brigade does not dispatch
  from them. Forward-plan skill documents emitting the hints.
- Seat-health admission now projects `brigade.seat_health_summary.v1` onto
  `run.json`, records typed incomplete worker summaries, persists
  same-seat-once retry attempts before re-probe, quarantines non-retryable
  seats for the run, receipts Codex app-server→exec as
  `brigade.transport_routing.v1`, re-probes on resume, and rejects read-only
  seats whose declared isolation is only soft/none. (#474)
- Cross-repo campaign ready view (#814): `brigade work ready --campaign <name>`
  aggregates per-repo work graphs through a named lens under
  `.brigade/campaigns/<name>.json` (member targets + optional cross-repo
  `blocks` edges). Task ids are repo-qualified (`member:local-id`); member
  ledgers stay authoritative and unchanged when the campaign file is removed.
  `--parallel-safe` with `--campaign` composes those per-repo waves at query
  time (see #999); member ledgers stay authoritative and waves are not stored.
- Work brief as wiring source of truth (#733): static harness instruction
  blocks shrink to depth profiles rendered from the same ordered brief
  sections as `brigade work brief`. Hook-capable Claude installs default to
  a short `minimal` pointer when SessionStart injects the live brief; file-only
  installs keep `full`. Each integration records its required depth; shared
  files take the richest active requirement (full-then-minimal and
  minimal-then-full converge; uninstalling the last full consumer drops back
  to minimal). `--profile minimal|full` overrides selection. Doctor warns when
  a minimal profile remains after brief-injecting hooks go inactive. Live
  `brigade work brief` opens with the session-close checklist.
- Optional evidence-backed decision checkpoints on work plans (#496):
  `brigade work task plan <id> --write --decision <id> --decision-prompt "..."`
  with repeatable `--option` declares a checkpoint; `--resolve-decision <id>`
  requires `--selected`, `--rationale`, and `--evidence-ref` before the plan can
  be `--accept`ed or before `work task claim` / `work claim` / `work task done`
  begin dependent work. Receipts gain a `decisions` list; plan.md renders a
  Decision checkpoints section. Absent `decisions` on legacy receipts stays
  valid; malformed non-list `decisions` or invalid entries fail closed instead
  of being dropped. `--evidence-ref` stores an opaque receipt path or external
  evidence id (no local-file existence check).
- Claude compaction restores the work brief on `SessionStart` with
  `source=compact`, reinjecting the live brief beside bounded session-start
  recall (#736). A one-shot marker still backs `UserPromptSubmit` when compact
  reinjection is unavailable; the compact path clears stale markers so prompt
  submit does not double-inject.
- Advisory `brigade skills audit <run>` compares skill.json process
  obligations (`check` / `review` / `handoff`) declared by a run's
  `selected_skill_ids` against captured verify, review, and handoff ingest
  receipts (#499). Missing required evidence is reported as findings with
  exit 0; verify commands may stamp `obligation_id` for precise matching.
  Bundled `brigade-work` declares `verify-through-brigade` and
  `session-handoff` obligations. Orchestrator run identity is exported to
  workers as `BRIGADE_RUN_ID`; verify, review, and handoff producers stamp
  optional `producer_run_id`. Per-run obligation matching requires exact
  `producer_run_id` equality (never timestamp proximity); legacy unstamped
  receipts are labeled unattributed and cannot satisfy. Public JSON/text
  output uses repo-relative or collision-resistant `external:<name>-<digest>`
  path labels and never prints host-private absolute paths, including in
  `load_error` / `load_warnings` text for missing path-based skill selectors.

### Fixed
- Run budget resume gate (#593 / PR #864): `brigade runs resume` evaluates the
  persisted declaration, original `started_at`, and authoritative budget
  lifecycle projection before `AppServer.start()`, refusing expired or
  already-exhausted declared runs without provider or synthesis calls.
  Undeclared and non-expired declared resumes keep existing recovery behavior.
- Run budget restart reclaim (#593 / PR #864): evaluate the wall-clock ceiling
  before reclaiming a durable pending dispatch can authorize external work, so
  an expired run cannot relaunch attempt 1 after recovery.
- Run budget declared-only contract (#593 / PR #864): document that hard
  wall-clock / worker-dispatch ceilings apply only when a run carries
  `run_budget` or `verification_contract.budget`; ordinary undeclared
  `brigade run`, dogfood, and model-trial paths remain unbounded for
  backward compatibility (no invented flags, defaults, or timeout→budget
  mapping). Add undeclared-path regressions alongside the declared
  enforcement test.
- Run budget normal-run wiring (#593 / PR #864): persist declared
  verification-contract / run_budget through start and plan receipts so the
  coordinator enforces real wall-clock and dispatch ceilings; allocate
  same-seat attempts inside the reservation lock so `worker_dispatch_count=1`
  launches once; bridge optional `input_tokens` / `output_tokens` while keeping
  aggregate `token_budget` distinct.
- Run budget `token_budget` bridge (#593 / PR #864): preserve aggregate
  VerificationContract `token_budget` / receipt `tokens_used` semantics; do not
  map the legacy ceiling onto `observed.input_tokens`. Explicit `input_tokens` /
  `output_tokens` remain separate observed dimensions.
- Run budget dispatch reservations (#593 / PR #864): serialize parallel
  same-stage reservations under the coordinator lock; use stable
  `dispatch:{seat}:{attempt}` request identities with durable
  reservation→`run.dispatch.requested` ordering and restart-safe idempotency;
  live Ctrl-C during dispatch terminalizes as `operator-cancelled` (not
  infrastructure-neutral). Wall-clock, usage labels, compatibility diagnostics,
  and #594 exclusion unchanged.
- Claude harness readiness now fails when a global `core.excludesFile` rule
  such as `.claude/` hides the managed handoff `TEMPLATE.md`.
  `operator verify-harness --harness claude` returns `ready: no` with a nonzero
  exit, and `operator quickstart` propagates a non-ok status plus the exact
  non-mutating recovery command `git check-ignore -v
  .claude/memory-handoffs/TEMPLATE.md`. Brigade never edits global Git
  configuration; docs/new-user-quickstart.md documents narrow repo-local
  un-ignore rules. Codex-only onboarding keeps the warning-only contract.
  (#855)

## [0.26.1] - 2026-08-09

### Changed
- `brigade work` task ledgers now track dependency edges, a computed ready set, atomic compare-and-set claims with fail-closed filters, and three-phase file and symbol footprints from task creation through verified completion. (#783, #796, #797)
- Memory care now ships two review skills and three pinned runbook templates, reports read-only archive candidates, and documents cron, systemd, and GitHub Actions scheduling. (#771, #785, #786, #789)
- `brigade center serve` adds an explicit loopback-bound read-only dashboard transport with host, CSP, and token safeguards, and the docs define Brigade's external-scheduler boundary. (#729, #784)
- `brigade handoff lint` warns about context-dependent durable facts, supports `--strict`, and distinguishes salvageable non-template notes from invalid input. (#730, #773)
- Stable publishing accepts only exact release tags, while a separate daily main-branch workflow publishes development wheels without version commits or GitHub Releases. (#782)
- Error and refusal copy is treated as an agent-facing API (#739): destructive
  and gate-bypassing flags (`--force`, `--allow-*`, `--operator-confirm`,
  `--allow-dirty`) are no longer named as remediations; wrong or platform-only
  advice (init `--force` for missing templates, redundant sweep `--force`,
  `chmod +x` hook guidance) is corrected. Convention lives in CONTRIBUTING;
  audit table in `docs/audit/2026-08-09-error-refusal-copy.md`; contracts in
  `tests/test_error_refusal_copy.py`.

### Added
- Ingest reinforces an existing memory card when a `create-card` handoff
  repeats a known fact (#724): content fingerprints (plus ~0.9 token Jaccard
  near-match) avoid near-duplicate files. Exact matches bump `last_reviewed`,
  increment `reinforcements`, and append an evidence pointer; near-matches are
  proposed to the review inbox (never auto-merged); opposite-polarity
  near-matches route as contradiction candidates. `memory care backfill`
  also stamps missing `fingerprint` values (dry-run by default).
- Parallel-safe ready partition (#778): `brigade work ready --parallel-safe`
  wraps the native ready set with a query-time greedy partition into dispatch
  waves by footprint intersection (exact file overlap first; one-hop
  GraphTrail symbol impact as refinement when the binary and index exist;
  file-overlap-only degrade otherwise). Same wave = parallel-safe; cross-wave
  = serialize. Empty effective footprints get an exclusive wave. Waves are
  never stored.
- CandidateSetGate for the portable tool catalog (#504): optional tool
  `domain` / `capability` / `risk_class` labels and plan-step
  `domain` / `capabilities` / `max_risk_class` requirements filter
  `.brigade/tools.toml` before worker dispatch. Admissible tool ids and scores
  are recorded in `candidate-set.json` and stamped onto the plan; an empty
  required set fails closed with typed `no-admissible-tool` and one bounded
  replan instead of letting the model improvise tools.
- `brigade care install|status|uninstall` (#759): opt-in scaffold that writes
  target-namespaced systemd user timers on Linux or launchd agents on
  macOS, matching `docs/scheduled-care.md`. Memory-care recipes invoke the
  shipped runbooks so scheduled fires write normal receipts; other platforms report explicit unsupported scheduling; tampered blocks are
  reported and not clobbered without `--adopt`.
- Worker event stream field classification and fail-closed scrubbed projections
  for the Codex app-server transport (#592): closed scrub/export classification
  matrix (documented for every known method and item type; raw non-delta capture
  remains open-ended and is not a recorder allowlist), scrubber with adversarial
  golden fixtures per matrix item type, distinct raw/scrubbed media types and
  artifact classes, and audit/replay rejection of raw streams unless an explicit
  local-only policy allows salvage. Legacy raw streams stay locally inspectable
  and marked unclassified until scrubbed. First slice keeps legacy backfill and
  export/archive sidecar wiring out.
- Work verify plan ranks candidate verification commands from GraphTrail
  affected-test impact (#486): `brigade work verify plan` attaches
  `graph_impact` / `ranked_candidates` with hop-distance confidence and
  via-symbol evidence, optional `--file` seeds changed paths (otherwise
  `git diff --name-only HEAD`), and the worker still chooses the command.
  When GraphTrail is unavailable the ranking degrades cleanly to an empty
  advisory list.
- Versioned `brigade.work-run` archive schema for portable run import/export
  (#487): `work-run.json` + `payload/` directory layout, published JSON Schema
  at `schemas/work-run.v1.schema.json`, stdlib validator, and explicit
  compatibility rules (refuse unsupported archive versions, strip private
  recovery-checkpoint bodies on export, ignore unknown nested receipt keys).
  Additive CLI: `brigade runs export`, `brigade runs import`, and
  `brigade runs validate-archive`.
- `forward-plan` bundled registry skill (#779): turns the native ready set,
  `brigade outcome rank`, and repo `ROADMAP.md` into a dependency-filed plan
  artifact under `.brigade/work/plans/`. Proposes GraphTrail-derived edges
  (`blocks` from def/use, `conflicts-with` from write overlap) marked derived
  and confirmation-gated; degrades to zero proposed edges when GraphTrail is
  unavailable. Ships `plan.template.json` as the artifact contract. Skill only,
  with no new CLI subcommand.
- Hash-stamped managed instruction blocks (#732): Brigade frames harness
  instruction spans with `BEGIN/END BRIGADE INTEGRATION` markers that carry
  marker-format version, profile, and a full SHA-256 of the body. Install is a
  true no-op when recorded, actual, and desired digests match; check
  (`brigade harness sync --check` / `harness doctor`) classifies
  missing/stale/locally_modified/malformed/current and prints the fix command;
  removal is surgical. Locally modified or malformed spans fail closed unless
  `--force`. Symlinked targets are skipped (including a post-`lstat` symlink
  swap). Legacy `brigade:user-profile` markers are recognized and upgraded in
  place.
- Local memory-retrieval eval harness with a grep baseline (#722): checked-in
  fixture corpus under `evals/memory-retrieval/` (40 cards, paraphrase-weighted
  gold queries) and `python -m brigade.memory_retrieval_eval` comparing the
  current keyword scorer, a naive grep floor, and an optional on-device
  semantic adapter (skipped unless `sentence_transformers` plus a local model
  are present). Reports Precision@K / Recall@K / hit rate / first-gold rank
  per adapter and category, plus an oracle ceiling. Dev tooling only; no new
  public CLI command.

### Fixed
- `brigade work task claim` now atomically creates a CAS `claim_id` that guarded
  reassign and release recognize, rejects foreign-holder footprint refine with
  exit 13, and rolls back blocked claims instead of leaving `in_progress` rows
  without a usable claim record. (#856)
- DAG runs no longer deadlock on shared cover sets, accept mutual-wait cycles, misreport one-node execution, route to unhealthy seats, or miss shared-checkout isolation breaches. (#742, #788, #791, #794)
- `brigade run --wait` uses a fair FIFO queue, reclaims dead or stale holders, and retries the head waiter after a failed handoff instead of starving or timing out at 600 seconds. (#781, #790)
- Release and security checks now reject stale readiness receipts across HEAD, base-ref, or tracked-tree changes, limit release content scans to tracked files, tolerate broken external scanners, point failures to `brigade scrub`, and coalesce overlapping open security findings while preserving per-fingerprint suppression and hiding internal metadata. (#753, #756, #774, #775, #776, #793)
- On Windows, repository text reads use UTF-8 with a safe decode fallback, and `brigade work verify run` hosts shebang, PowerShell, CMD, and batch scripts with native interpreters. (#767, #769)
- Required notify checks remain reportable on unrelated pull requests, and roadmap command audits ignore rejected design alternatives without suppressing accepted examples. (#755, #787)

## [0.26.0] - 2026-08-05

### Added
- Roster resolution now understands linked git worktrees: when a worktree has
  no `.brigade/roster.toml` of its own, `brigade run` and `roster doctor` fall
  back to the parent clone's roster (new `worktree-parent` source, reported in
  the run provenance line) before the user roster at `~/.brigade/roster.toml`.
  A fresh worktree of a wired repo keeps the repo's seats instead of silently
  downgrading to whatever the user roster or a scaffold provides. (#741)
- `brigade roster init` refuses to scaffold a starter roster when a fallback
  roster already applies to the target (the parent clone's roster in a linked
  worktree, or `~/.brigade/roster.toml`), since the codex+ollama-only starter
  would shadow every seat configured there. Pass `--force` to scaffold a
  workspace roster anyway. `brigade research` now resolves its roster through
  the same fallback chain instead of requiring a workspace roster. (#741)
- One-and-done onboarding: `brigade work hooks install|update|status|uninstall`
  gain `--scope user` (default `project`, prior behavior unchanged), installing
  the managed Claude work-loop hooks once at user scope so every repo the agent
  opens gets the work brief injected and an unwired git repo gets the exact
  `brigade init` command printed at session start. User settings merge
  non-destructively, Brigade-owned entries are tagged so uninstall removes
  only them, and status detects a stale script by hash. `operator quickstart`
  now suggests the user-scope install once when wiring the claude harness, and
  the README documents the flow. (#740, #743)
- `brigade status --json` carries the same `available_update` field the work
  brief gained, so machine consumers can watch for releases without parsing the
  brief. Cache-read only; never touches the network. (#717)
- The publish workflow verifies https://check.brigade.tools/v1/version reports
  the just-published release after the PyPI upload and fails loudly with
  update-and-rerun instructions on drift, and `release doctor` warns when
  pyproject declares a version with no matching git tag, catching
  version-bump-without-release before it ships. (#719)
- `docs/outcome-scoring.md` documents read-time re-scoring and the full failure
  kind-to-verdict table. (#718)
- `brigade runs audit` provides a read-only audit of recorded lifecycle evidence
  for coordinator decisions. (#649)
- Verify-run retention now archives receipt evidence before pruning (#598).
  When `.brigade/work/verify-runs/` grows past the retention cap, each run
  directory is copied into the verify archive (default
  `.brigade/work/verify-archive/<run-id>/`) and an append-only
  `index.jsonl` entry (`brigade.verify_archive_index.v1`) records the
  receipt's digest, signature, key id, and schema version before the local
  copy is deleted. Archival re-verifies the copied receipt bytes and the
  receipt's self-declared `digests.receipt_sha256`; a run directory whose
  archival fails or whose receipt no longer re-hashes is kept locally, so
  pruning never destroys unpreserved evidence. Archive paths that overlap the
  local run root, partial or symlinked archive destinations, and source trees
  containing symlinks or special files are rejected without pruning. New
  `.brigade/config.json`
  keys: `verify_runs_keep` (default 50), `verify_archive_enabled` (default
  true), and `verify_archive_dir` (default `.brigade/work/verify-archive`).

- `brigade work resolve-target --cwd PATH [--harness NAME]` prints the
  Brigade-wired project root (requires `.brigade/config.json`) so shell hooks
  share Claude's discovery contract instead of matching any `.brigade/` dir.
- Shared `brigade.wiring.resolve_wired_target` helper used by Claude hooks and
  the new resolver CLI; optional harness filter.
- Grok work-loop hook templates under `src/brigade/templates/grok/hooks/` that
  use `resolve-target`, timeout the session brief, and do not deny edits while
  a brief is running (#537).
- Imported `stations/notify` Go module into the Brigade monorepo. Unified release
  manifests now enumerate five native components (25 platform assets plus
  `component-manifest-v1.json` and `checksums.txt`) with managed resolution
  through `brigade setup`.
- Beta pre-pin compatibility: beta installs the CI-green `main` Brigade wheel but
  reuses the last verified stable component manifest; `agent-notify` is omitted
  from setup until a stable manifest publishes its assets. Stable validation
  stays strict.
- Public update-channel and component-manifest policy docs.
- Agent Pantry compatibility probe: `brigade pantry` requires `agentpantry
  version --json` `>=0.5.0` before invoking doctor, status, or inventory
  surfaces. Pantry remains an external Go binary.
- The append-only lifecycle journal now defines the canonical `brigade.run_event.v1` event contract, closed payload allowlists, deterministic digest chains, idempotent appends, partial-tail quarantine and recovery, and private journal artifacts. (#607)
- Enrolled runs record lifecycle transitions before their compatibility `run.json` snapshots advance, while legacy and opt-out runs remain snapshot-only. (#608)
- A pure, deterministic run snapshot projector derives journal-backed fields while preserving the existing `run.json` contract. (#609)
- Shadow projection comparison records bounded digest-only evidence of whether each snapshot matches its lifecycle-journal projection, including lag, mismatch, error, and crash-gap states. (#610)
- Crash-safe recovery checkpoints cover lifecycle events and `run.json` replacements, let recovery restore a verified record after interruption, and add read-only Doctor checks for repairable and unsafe states. (#615)
- Lifecycle events now pair every worker dispatch request with its observed completion or failure, carrying the selected seat and attempt identity and surfacing unobserved requests as recovery work. (#625)
- Enrolled runs can pause at Daily and Tool approval boundaries, then resume and redeem the approved action exactly once with redacted, bound approval facts. (#626)
- `brigade runs redact` provides an operator-confirmed, fail-closed procedure to redact terminal lifecycle journals, reproject their snapshots, and retain chained anchors for overlapping rewrite history. (#627)
- `brigade runs events` exposes lifecycle-only NDJSON with opaque durable cursors, and `runs steer` and `runs interrupt` accept idempotent request IDs with journaled requested and observed control facts. (#641)
- Runs probe declared seat health after writing the initial run receipt and before planning, recording `seat-health.json` beside the receipt; a probe that fails, hangs, or raises is itself receipted as a failed result for every declared seat, so probe trouble cannot take down an already-recorded run. (#685)
- A failed orchestrator seat now selects a healthy declared fallback, or aborts
  with its typed cause, before planning begins. (#688)
- Four packaged roster presets (`minimal-single-cli`, `budget-open-weight`, `full-multi-lane`, `review-heavy`) and optional per-seat `purpose`, `requires`, `fallback`, `stats`, and `caveats` metadata, with one-hop capability-based fallback resolution when host requirements are not met. (#454)
- `brigade roster suggest --preset NAME` assembles a preset roster from installed host capabilities, emitting per-seat resolution lines and substituting local receipt stats when available, and `brigade roster stats` summarizes per-seat median duration and failure rate from local worker receipts. (#463)
- `brigade model trial regrade OUTPUT_DIR` re-runs graders from stored trial output without re-running seats. Trial summaries record `measurement_failures` and `partial_scores`, cells carry `failure_reason`, and trial processes exit `3` when measurement failures are present, `1` for rejected or execution errors, and `0` otherwise. (#457)
- The `oracle` CLI adapter (`cli = "oracle"`) runs browser research via `oracle --engine browser`, reports stale cookie auth with a `brigade pantry` next step instead of `workspace-trust`, and lets a researcher seat's `timeout_seconds` raise the research engine LLM timeout floor. (#535)
- `run.json` records a `scheduler` block with `requested`, `used`, and `fallback_reason`, written at run start and filled when dispatch resolves the effective scheduler (`--scheduler`, then `limits.scheduler`, then `waves`), so a receipt distinguishes a real DAG dispatch from a silent degrade to waves. (#543)
- Failed worker results and per-attempt records in run receipts include a typed `failure` object (`brigade.worker_failure.v1`) with `failure_class`, `phase`, and `retry_disposition`, mapped from the legacy `failure_kind` and `failure_phase` fields. (#589)
- `brigade roster doctor` runs a shared seat health probe per declared seat, covering declaration, executable identity, authentication, transport liveness, version gates, model reachability, and isolation compatibility, without model smoke prompts. (#590)
- `brigade mcp pi-bridge` exposes `discover`, `call`, `install`, and `uninstall` to project the canonical `.brigade/mcp.json` catalog into Pi's managed extension at `~/.pi/agent`, names tools `server__tool`, rejects catalog server names containing the reserved `__` separator, preserves server and tool identity on MCP failures, and tracks the generated extension and catalog projection through install receipts. (#438, #453)
- `brigade harness install` accepts `--surface cursor-cli|cursor-gui` to select a vendor surface explicitly, and `--projection-only` (which requires `--surface`) to let a runtime-absent surface receive Brigade projections without claiming a native runtime; install records the decision in `.brigade/surface-evidence.json`. (#464)
- `brigade harness sync`, `brigade harness uninstall`, and `brigade harness doctor` support Claude Code and Codex user-scope profiles (`--target claude|codex|all --scope user`), dry-run by default, with receipt-based ownership, idempotent sync, and uninstall that removes only Brigade-owned artifacts. (#465)
- User-scope harness profiles extend to OpenClaw, Kimi, Grok, Cursor, and OpenCode under the same sync, doctor, and uninstall contract, so `brigade harness sync --target all --scope user` covers all seven harnesses. (#467)
- `brigade evidence show`, `brigade evidence explain`, and `brigade evidence stats` query the evidence ledger without invoking the engine binary directly; help, status, doctor, and plan text use the evidence engine branding, timeouts honor `BRIGADE_EVIDENCE_TIMEOUT_SECONDS` and `BRIGADE_EVIDENCE_STATUS_TIMEOUT_SECONDS`, and exit `124` prints a retry hint that doubles the current timeout. (#430)
- `brigade mcp sync --user-scope` and `brigade operator sync-mcp --user-scope` no longer write stdio MCP servers into a user-wide client config silently: interactive runs show the destination, the stdio count, and the servers-times-sessions process formula before asking for confirmation, non-interactive and `--json` runs require `--allow-global-stdio`, and plan and sync items now carry `transport` and `scope`. (#425)
- After a successful command, Brigade prints a one-line stderr notice at most once per 24 hours when a newer release is on PyPI. It is anonymous, TTY-only, skipped in CI, and disabled with `BRIGADE_NO_UPDATE_CHECK=1`. (#426)
- `brigade code` exposes the full GraphTrail verb set (`callers`, `callees`, `affected`, `search`, `neighbors`, `dead-code`, `cycles`, `evaluate`, `explain`, `export`, `stats`, `doctor`, `diff`) and accepts `--target <dir>` on every verb, so engine paths resolve against the wired project root. (#427)
- `agent-notify` ships inside the Brigade monorepo under `stations/notify/` with its standalone history preserved and monorepo CI coverage, and installs as a native component through `brigade setup`. (#433, #439)
- `brigade model trial execute --resume` freezes eval cell identity (`cell_id`, `manifest_digest`, attempt numbering) so resumed cells keep stable identity, reports stale cells on stderr, and allocates the next attempt from both the attempt directories and `cell.json` instead of reusing deleted numbers. (#436)
- `brigade handoff lint --content-guard` flags prompt-injection heuristics with line numbers alongside the existing leak scan. (#477, #515)
- The default scanner registry schedules `brigade friction scan --json` weekly (`weekly@05:00`) with cadence-aware stale thresholds for seven-day freshness. (#526)
- `brigade handoff lint` accepts synonym section headings (for example `TL;DR` for `Summary`, `Kind` for `Type`) and resolves them in draft summaries. (#527)
- `agent-notify` resolves the per-turn Codex model from notify metadata or the matching local rollout and includes provider identity in Discord, Telegram, and Signal messages. (#533)
- `brigade run` gains `--scheduler {waves,dag}`, a ready-queue DAG scheduler that keeps independent branches moving and skips transitive dependents of failed workers. Fail-fast stage gating is now the default and `--keep-going` restores the old run-everything behavior; a run with any failed or skipped worker records `status: "incomplete"` in `run.json` and exits `3`. `brigade work verify run` reuses the newest completed receipt when the tree fingerprint and planned commands match, recording `reused_from`, unless `--no-reuse` is passed. (#447)
- Verifier-bound skill scorecards: `brigade work verify plan` and `brigade work verify run` accept `--manifest <id>` to run tracked verifier manifests (`brigade.verify_manifest.v1` under `verify/manifests/*.json`) with effectiveness and utility-guardrail check roles, receipts stamp the manifest and patch binding plus per-command check identity, skill promotion in `brigade outcome reconcile` and `brigade outcome fork` is decided by a receipt-only scorecard (`scorecard.v1`) with `route-policy.v1` bands from `unseen` to `promoted`, and the new `brigade outcome backfill scorecard` verb audits receipt eligibility read-only. (#591)
- `brigade run` writes a `route-decision.json` artifact (`brigade.route-decision.v1`) into every run output directory recording the chosen route, confidence, template version, and the admissible worker seats, and the `route` object in `run.json` gains `confidence` and `template_version`. (#547)
- `brigade work verify run` receipts are bound to patch identity: each run directory gains a `changes.patch` artifact and `receipt.json` (schema version 2) records `baseline_commit`, `tree_fingerprint` (the real git tree hash, computed without touching `.git/index`), and `changes_patch_sha256`. All three degrade to `null` rather than failing the run, and `brigade work verify show` prints the verified-tree binding line. (#563)
- New `.brigade/config.json` key `capture_before_retry` (`warn` default, `block`, `off`) gates `brigade work verify run`: retrying a plan whose newest matching receipt failed without a captured `brigade outcome` record prints a warning naming the capture command, `block` exits `1` instead, `off` skips the check, and an invalid value exits `2`. (#560)
- `brigade receipts export` telemetry projects verify receipts as `brigade.work.verify` tool spans (executable name only, never the full command text) and outcome records as `brigade.outcome.capture` spans carrying artifact, signal, and cohort-scored attributes, both attached to the owning run's trace with a synthetic trace id for orphans. Run-span output is unchanged. (#569)
- `brigade work brief` reports an `update_available` line when the cached update check knows of a newer release, and the brief's JSON payload carries the same data under an `update` key (`null` when none). The read is cache-only, never touches the network, and honors `BRIGADE_NO_UPDATE_CHECK`. (#699)

### Changed
- Outcome scoring counts model-quality failures against seats instead of
  neutralizing them: `FailureDomain` gains `MODEL_OUTPUT` (empty-output,
  malformed-final-output, tool-only-output, non-final-output), the blanket
  FailureClass-to-infrastructure fallthrough becomes an explicit allowlist,
  run-level and worker-level verdicts agree for the same kind, unknown kinds
  fail closed to negative, and a suspected no-op run scores negative. Scores
  recompute from stored receipts at read time, so rankings can shift after
  upgrading; re-read them rather than comparing across the upgrade. (#718)
- Newly journal-authoritative runs derive their `run.json` compatibility snapshots from validated lifecycle events and recovery checkpoints, with strict projector readiness and recovery gates. (#622)
- Lifecycle journals are now authoritative for every new run; existing snapshot-only directories retain their prior behavior, and compatibility snapshots remain readable by the previous v1 reader contract. (#630)
- The previous-v1-reader compatibility check now uses a versioned fixture derived from `v0.25.0`, covering authoritative, paused, and legacy run snapshots. (#640)
- The lifecycle journal ceiling is measured for real, representative, and configurable worst-case runs, with the current decision to raise the bound rather than segment journals. (#642)
- Worker infrastructure failures no longer lower skill scores. Friction records
  carry `failure.class` as `error_class` with an optional adapter `cause_code`,
  and a run that failed only for infrastructure reasons produces neutral
  artifact evidence while retaining its evidence reference. Verifier failures
  remain negative evidence and verifier success remains positive. (#682)
- **Outcome scores are recomputed from stored receipts at read time, so the two
  classifier changes below re-score existing evidence with no new runs.** Two
  vocabularies now feed one classifier: run-owned failure kinds and typed
  `FailureClass` causes. Most visibly, `timeout` exists in both, so historical
  timeout failures that previously scored negative now score neutral. Rankings
  produced before upgrading will differ afterward; re-read them rather than
  comparing across the upgrade. (#686, #689)
- Outcome capture reads the nested `failure.kind` taxonomy, retaining the legacy
  top-level `failure_kind` as a fallback. Of 56 recorded failed runs, 19 carried
  only the nested field and 16 carried both, which is why the fallback stays.
  Classification also consults the failure phase: `agent-error` and
  `orchestrator-error` are catch-all else-branches rather than infrastructure
  evidence, so kind alone would have forgiven real model failures. (#686)
- Typed `FailureClass` causes are classified as infrastructure, so a run aborted
  with a typed cause is no longer scored as a skill failure. Unknown kinds still
  resolve to a real failure, and model-contract kinds are matched before the
  typed parse so `non-final-output` stays a model failure. (#689)
- The `kimi` adapter targets Kimi Code 0.28 prompt mode (`kimi -p`), dropping `--print`, `--final-message-only`, and the interactive `--plan` and `--yolo` flags that cannot combine with `-p`. Read-only kimi dispatches now rely on prompt-based enforcement, and `READ_ONLY_ENFORCEMENT` for `kimi` is `soft` rather than `hard`. (#401)
- `brigade doctor` scopes default output to `--target`, hiding host-global operator checks (components, managed tools, OpenClaw, content guard) unless `--operator` is passed, and prefixes target-scoped finding detail with `target=`. (#478)
- `brigade doctor` no longer assumes Claude for targets without Brigade config or an explicit `--harness`. Harness-native checks run only when a harness is declared, and handoff, skills, and orphan projection checks are labeled with an `adapter:` prefix. (#449)
- `brigade friction` regex scans skip default denylisted suggestion and skill paths, damp prose hits in markdown outside run artifacts, and collapse identical snippets into one candidate with occurrence counts and source paths. (#473, #513)
- `brigade receipts export miseledger` now carries stable `project` and `workspace_dir` metadata so MiseLedger project filters work across generated worktree paths. (#540)
- `brigade release` readiness treats open security findings and unreadable security evidence as blockers rather than warnings, naming the top finding with severity, location, and remediation. (#670)
- `brigade skills diff` defaults to the bundled package template so a stale registry copy cannot mask drift from newer bundled skills; pass `--against registry` for the previous baseline. (#514)
- `miseledger init` no longer writes an unused `config.toml`; it creates only the private data and cache directories and opens the archive database. (#538)
- `agent-notify` validates `defaults.timeout_seconds` at config load with stable field-scoped errors, and Claude Code Stop-hook `cwd` and `session_id` disclosures are now opt-in through `[claude_code_stop]` (`include_cwd`, `include_session_id`, both default false). (#671)
- `brigade security scan` excludes `.brigade/**`, Brigade's own state directory, by default, including when `security.toml` omits `exclude_paths`. Writing `exclude_paths = []` opts back in, `brigade security init` writes the exclusion into generated configs, and unknown keys in `security.toml` are rejected instead of ignored. (#544)
- `brigade handoff lint --content-guard` fails the lint with exit `1` on any warning-severity injection heuristic hit instead of passing with a printed warning, and reports split `content_guard_egress` and `content_guard_injection` verdicts in both text and JSON output. Info-only hits still pass. (#559)

### Fixed
- The security scanner's secrets detectors no longer flag their own regex
  literals (the old guard pointed at a file path retired by the package split),
  runtime reads (`os.environ`, `os.getenv`, `secrets.*`, `*_from_env`),
  attribute reads, or guard example fixtures, and one ranked finding is emitted
  per line instead of duplicates. Committed credentials still report: quoted
  literals are never exempted, dotted JWT-shaped values stay flagged, and
  regression tests pin both directions. (#701)
- The passive update check's background version refresh runs even when stderr
  is piped, so agent-only installs populate the version cache and the work
  brief's `update_available` line can actually fire; the printed notice stays
  TTY-only. (#716)
- A bare `brigade` invocation prints the start-here command block before the
  usage error instead of a dead end. (#720)
- `agent-notify init` now refuses a symlinked parent before it creates or publishes its config file, publishes by hard link so an existing path is never followed, and applies permissions and fsync through the open descriptor rather than by path, closing symlink and TOCTOU write races. (#643)
- Dispatch outcome writes are now bound to configured targets and report a
  bounded error naming the resolved root. Missing Python interpreters suggest
  `python3`, stale command inventory names its regeneration command, and
  coverless root assignments no longer fall back to the wave scheduler. (#662)
- `brigade outcome repair` diagnoses and repairs completed-ledger digest-chain
  breaks, preserving and re-signing self-consistent records after a break
  instead of discarding them. Repairs quarantine first and use binary
  `O_EXCL` writes. (#645)
- A failed run that holds uncommitted work retains its worktree, reports the
  retained path and patch-file count, and prunes that retained entry after the
  next successful run for the same target. (#663)
- Outcome capture now attributes receipts to the exercised skill instead of the
  generic `brigade-work` bucket, preferring a failed receipt's own stamp over
  the current capture value. (#647)
- Grok/T3 work-loop discovery no longer treats `~/.brigade` (user-level aboyeur
  roster) as a project work root. Hooks and `work resolve-target` require
  `.brigade/config.json`, so sessions under `$HOME` or unwired dirs do not
  background `brigade work brief --target $HOME` (#537).
- `brigade run` no longer dies on the first unparsable plan when the chef's final
  message is prose. The corrective plan turn now restates the output contract
  ("reply with the JSON plan object and nothing else") alongside the parse error,
  and orchestrator seats that launch in a harness plan mode (claude, cursor, grok
  under read-only) are told not to write a plan, design, or context file: the
  failed write is what let user-level hooks replace the plan JSON with hook
  rebuttal prose. Retries stay bounded at one correction. (#520)
- Agent Pantry version parsing stays non-throwing and bounded for arbitrarily
  long numeric segments: the parser accepts ASCII-numeric semver triples only,
  enforces a conservative per-segment digit bound, catches `int()` conversion
  `ValueError`, and surfaces the fixed `invalid-version` label for oversized or
  non-ASCII-digit input without ever echoing raw content. Added huge-segment and
  non-ASCII-digit regressions covering observed/detail and adjacent stdout.
- `docs/component-manifest-policy.md` no longer presents the five-component /
  25-asset contract as a current stable release. It is now stated as the future
  first stable manifest contract after `agent-notify` publication, with current
  bundled `agent-notify` assets empty/unpublished and no stable release claimed.
- Lifecycle journal append, recovery, checkpoint, and shadow paths now serialize their critical sections, persist directory updates, enforce event and byte limits, and retain locks when durable enrollment state cannot be safely classified. (#624)
- Journal enrollment, partial-tail recovery, atomic writes, and redaction anchors are hardened against mid-run enrollment loss, interrupted writes, symlink races, and overlapping redaction histories. (#633)
- Recovery-checkpoint bodies are stripped from exported verification archives and replaced with privacy-safe artifact references; local recovery retains the private bodies it needs. (#646)
- Candidate outcome artifacts can recover from earlier regressions when later verified clears outnumber them in the same unchanged content-fingerprint cohort. (#648)
- Outcome scoring treats receipts linked by `reused_from` as one verification signal while retaining the append-only ledger rows. (#650)
- Redaction anchor refresh is idempotent, validates split-retirement digests, and safely resumes if cleanup stops between parent retirement and child realignment. (#644)
- Outcome-ledger writers serialize digest-chain appends and preserve completed rows while recovering interrupted writes. (#612)
- Journal redaction cleanup remains retryable across the parent-retirement and child-realignment crash window. (#655)
- `brigade handoff lint --content-guard` no longer reports a clean injection
  verdict for a handoff whose text could not be read. An unreadable path now
  reports `unscanned` and fails that check, so neither the printed line nor the
  `content_guard_injection` field can be mistaken for a passing security scan.
  Readable handoffs are unaffected. (#679)
- Proxy seat `_REF` overrides accept `env-file:/absolute/path#VARIABLE` references to load secrets from systemd-style env files. Roster validation and runtime resolution share one classifier, so a malformed `env-file:` reference fails dispatch instead of silently falling through to a parent environment variable. (#411)
- Branch and HEAD drift detection compares against a snapshot from the resolved `lock_workspace` (the canonical checkout) rather than the assigned worktree `cwd` when the two differ, so canonical-checkout movement is detected without false positives from an isolated worktree. (#599)
- Cursor model inventory captures the full `cursor-agent models` list through a tempfile, since piped output truncates at 8 KiB, and treats the list as complete only after the `Tip: use --model <id>` trailer, so `brigade roster doctor` `allow_models` checks see every installed model. (#601)
- Read-only Claude workers insert `--` between the `--disallowedTools` deny list and the positional prompt, so plan text is no longer parsed as additional deny rules. (#451)
- Read-only Claude workers deny `WebSearch` and `WebFetch`, so non-interactive runs under `--permission-mode plan` no longer stall on an approval prompt. (#461)
- `brigade operator` adopt plans count guidance inventory entries by filesystem type, so directories such as `memory/cards` no longer inflate the `guidance_files` count. (#479, #509)
- Agent Pantry health messages report dev and prerelease builds as a deliberate version-policy rejection rather than a parse failure, keeping the fixed `invalid-version` label and never echoing raw version strings. (#480, #511)
- The Claude work-loop closeout gate no longer treats detached `brigade run` output under `.brigade/` or `brigade run` bash commands as verification writes that bump `last_verification_write_at`. (#483, #516)
- `brigade harness sync` upgrades a legacy version-1 `~/.cursor/brigade/install-state.json` to profile schema v2 instead of deadlocking, preserving files, hooks, and MCP attestations so uninstall still works. (#510)
- `brigade work hooks install` adopts legacy `brigade-work-loop.py` handlers found in an unwired `~/.claude/settings.json`, and refuses when `.brigade/config.json` is present but unreadable. (#525)
- The Claude work-loop closeout gate treats an unavailable worktree fingerprint as a pending write and records the unavailable sentinel on every `Bash` pre-hook, so mutations are not missed when git state cannot be read at bash start. (#600)
- `brigade work verify run` names the supported fix when it rejects a shell interpreter as the verification executable. (#416)
- `brigade outcome reconcile` preserves colliding decision receipts by writing each one exclusively with microsecond timestamps and a random token instead of overwriting the prior file. (#603)
- Run sidecar files such as `worker-results.json` and `synthesis.json` append immutable revisions under `revisions/` instead of replacing prior evidence when updated mid-run. (#605)
- The content-guard `pre-push` hook derives the exclusion set for brand-new branches from `git ls-remote` rather than stale local tracking refs, scans only commits not advertised on the remote, and batches them through `--revs-stdin`. (#606)
- Content guard rejects unsafe `--range` operands for history scans while preserving valid Git revision grammar, and fails closed on `git rev-list` errors. (#677)
- Component reports and install checks honor the verified exact-release manifest from `brigade setup` instead of falling back to the bundled compatibility manifest whenever `installed.json` disagrees. (#408)
- GraphTrail, MiseLedger, and other managed engine consumers resolve binaries through a shared `component_bins` resolver (`GRAPHTRAIL_BIN`, `MISELEDGER_BIN`, then `installed.json`, then `PATH`, then legacy locations) and emit absolute paths in generated Cursor MCP config and search plans. (#419)
- `brigade work verify` catches Ctrl-C, terminates the child process group, and writes a `canceled` receipt with preserved evidence instead of leaving a running receipt or printing a traceback; a repeated interrupt retries termination with zero grace. (#448)
- `brigade work verify` archive export validates recovery-checkpoint references, omits crashed `.checkpoint.*.tmp` bodies without decoding them, keeps existing archive trees immutable, and records privacy-safe `checkpoint_errors` in the verify archive index when a checkpoint cannot be exported truthfully. (#668)
- Lifecycle journal appends serialize across processes through `lifecycle.jsonl.lock` (`fcntl.flock`), `brigade runs steer` and `brigade runs interrupt` refuse completed runs instead of appending into a terminal chain, and `control.failed` payloads carry a bounded error class plus digest reference rather than free transport text. (#651)
- The lifecycle journal event ceiling rises from 512 to 2048 with bounded runtime reads that enforce byte and event limits before allocation, and authoritative dispatch recovery reuses a stranded dispatch checkpoint instead of duplicating journal events after a crash between checkpoint write and event append. (#672)
- `agent-notify` caps hook and stdin reads at 256 KiB and truncates or refuses Discord, Telegram, and Signal payloads against documented ceilings, returning a bounded `payload_limit` diagnostic instead of failing delivery silently. (#618, #666)
- `agent-notify` channel transport failures return bounded provider, stage, status, and cause fields instead of wrapped transport errors that could carry webhook URLs or bot tokens into stderr. (upstream agent-notify #8, imported via #433)
- `brigade run` records a `pre-run-snapshot.json` of branch, HEAD, and content fingerprints, aborts with exit `2` and `failure_kind: "branch-head-drift"` when git state drifts mid-run, rejects `--allow-dirty` write runs in a dirty primary checkout unless `--worktree` is passed, and refuses Claude sandbox modes it cannot enforce: `workspace-write` and implicit sandboxes now fail dispatch as `unsupported-sandbox` instead of launching unsandboxed. (#440)
- Security finding fingerprints are content-addressed by rule, path, normalized excerpt, and occurrence index, so suppressions survive edits above a finding. Legacy line-based fingerprints are kept as aliases and auto-migrated through `.brigade/security/fingerprint-migration-map.json`, and `exclude_paths` patterns with a trailing `/**` now exclude the prefix and all of its descendants. (#539)
- `brigade work verify run --capture` auto-exports pending receipts and imports them into MiseLedger after capture, fail-open and never changing the verify exit code, and the `brigade receipts export miseledger` cursor advances only after a successful import, so a failed or missing `miseledger` binary no longer silently drops receipts from later exports. (#552)
- `brigade handoff lint` accepts near-miss section headings such as `## TYPE` or `## Summary:` and recognized synonyms, resolving them to the canonical section with a warning and a `use ## <canonical>` hint instead of failing, and warns when sections appear out of canonical order. (#558)
- Reused `brigade work verify run` receipts stamp `harness_session` from the caller's `BRIGADE_CLAUDE_SESSION`, so a receipt-reuse cache hit no longer drops the session fingerprint that a fresh run records. (#551)
- Research config loading no longer fails under a no-site Python 3.10 where the third-party `tomli` fallback is unavailable; `brigade.research.config` now reads through the bundled `toml_compat` parser. (#549)
- Evidence-ledger search with a `--project` filter no longer scans the whole archive: the FTS query pins the materialized candidate set as the outer loop, so project and other correlated metadata filters are evaluated only against FTS candidates. (#548)
- `brigade repos` sweep subprocesses put the source root rather than the package directory on `PYTHONPATH`, so built-in sweep commands can import `brigade` from a source checkout without installation. (#545)

### Removed
- Removed the opt-in `brigade run --deliberate` grounded-deliberation mode
  (planner, `brigade.deliberation.v1` artifact emission, and related runs
  show/watch/resume surfaces). Recoverable from git history; see #442 / #471.
- The `BRIGADE_RUN_JOURNAL_AUTHORITY` environment flag is no longer needed or read at runtime because journal authority is the default for new runs. (#630)

## [0.25.1] - 2026-07-21

### Added
- `brigade evidence` now owns crawler runtime selection: it resolves the crawler binary, runs a read-only compatibility check before delegating to MiseLedger, refuses an incompatible runtime, persists per-source last-run, and makes evidence doctor/status health the worst of MiseLedger health, crawler compatibility, and the latest crawl - so a `NO_PENDING` queue no longer masks a failed producer. (#404)
- `brigade roster doctor` and worker dispatch preflight `cloudflare-ai-gateway/` seats for `CLOUDFLARE_ACCOUNT_ID` / `CLOUDFLARE_GATEWAY_ID`, failing before the child launches and classifying the failure as provider configuration. (#394)
- `brigade doctor` warns when more than one enabled producer can write the same memory-care queue artifact, with a read-only migration plan. (#403)

### Fixed
- Install/update reconciliation now detects and removes the obsolete standalone `brigade-work-loop.py` hook (anchored to executable position so unrelated hooks are never touched) and reports a stale legacy registration with the exact repair command, preventing the legacy hook's unbounded untracked-file read from OOMing a session. (#397)
- Evidence ledger import skips an oversized JSONL line instead of aborting the whole import. (#420)

### Removed
- The `solo-mise` command alias, deprecated since the Brigade rename. Use `brigade`. The `.solo-mise` workspace-directory read fallback is unaffected. (#421)

## [0.25.0] - 2026-07-20

### Added
- Native component releases now publish one verified asset set for `brigade setup`, and `brigade update` has explicit `stable` and `beta` update channels. (#364 / #398)
- The Phase 4A policy documents migration and archive gates for GraphTrail, GraphTrail MCP, MiseLedger, and SessionFind. The compatibility window is active as of the v0.25.0 publication, and the policy does not authorize archival. (#399)

### Deprecated
- GraphTrail's direct `miseledger` Cargo feature, `graphtrail context --evidence`, and `graphtrail links` are deprecated. They remain functional for at least two minor GraphTrail releases or 90 days after the first GraphTrail release containing this deprecation, whichever is longer. Use `brigade code sync`, `brigade code context`, `brigade code impact`, `brigade evidence crawl`, `brigade evidence search`, and `brigade evidence doctor`. (#392)

### Fixed
- In-house repository and release-input guard policies preserve only approved agent co-author trailers; outbound public policies still strip them. (#386 / #396)
- Concurrent sessions no longer mistake another session's write for their own, so completed closeout does not re-arm. (#380 / #395)
- Published-artifact acceptance recognizes SessionFind command-list help output. (#393)

## [0.24.0] - 2026-07-20

### Added
- `brigade version --components` reports managed native component installation state, and Windows native acceptance now installs and exercises the supported component paths. (#374 / #375)
- GraphTrail v0.4.0 and MiseLedger v0.6.0 source is available under `engines/`; their standalone repositories and release pipelines remain unchanged until Phase 4. (#379 / #381)
- The `brigade.code-reference.v1` schema and canonical vectors define the exact code-reference contract for evidence lookups before lexical fallback. (#361 / PR #389)
- Executable `brigade code sync|context|impact` and `brigade evidence crawl|search|doctor` are available; `brigade search` keeps tested aliases for two minor releases or 90 days, whichever is longer. (#362 / PR #390)

### Fixed
- `brigade run` terminalizes interrupted and stale runs, refuses read-only execution for seats without that capability, and rejects empty tasks. (#382 / #383 / #384)

## [0.23.3] - 2026-07-19

### Fixed
- Post-install smoke now accepts sessionfind v0.6.0's released command-list `--help` shape while retaining legacy usage-text compatibility, so clean Windows setup succeeds without Go or Cargo. (#355 / PR #372)

## [0.23.2] - 2026-07-19

### Fixed
- Post-install smoke now accepts sessionfind v0.6.0's successful `--help` exit while retaining absolute managed-path and usage-text validation. (#355 / PR #370)

## [0.23.1] - 2026-07-19

### Added
- Versioned component manifest v1 with pinned GraphTrail platform artifacts and provenance checks. (#353 / PR #367)
- `brigade setup` installs those pinned native components transactionally, with dry-run, offline cache, rollback, smoke checks, and state recording. (#355 / PR #368)

### Changed
- Clarified cross-platform installation documentation. (#351)

## [0.23.0] - 2026-07-18

### Fixed
- ACPX late permission prompts after a completed `end_turn` answer no longer discard the usable final result. Brigade now preserves the terminal text, stdout/stderr, exit code, session/request metadata, and records a typed `transport_warning` while marking the worker `ok` with warning instead of failing the run. Post-permission assistant chunks and permission errors without pre-final text no longer qualify as usable-with-warning. Pre-final permission failures remain failed. Stderr fallback infers late `-32072` only when the stream has no structured JSON-RPC error; any structured error takes precedence over stderr markers. (#337)
- `proc.run()` decodes child stdout and stderr as explicit UTF-8 on every platform, including `TimeoutExpired` partial output, normalizes missing streams to empty text, preserves recoverable bytes with deterministic UTF-8 replacement, exposes explicit decode status on `proc.Result`, and surfaces typed harness decode failures through `run_agent` and ACP without counting a model verdict. Provider, output-validation, and chronology failures stay distinct. (#336)
- Run control selects a supported local transport per platform: Unix-domain sockets remain the default on Linux and macOS when the socket path fits platform limits; otherwise Brigade falls back to authenticated loopback TCP with an ephemeral port. Run metadata records a typed `control_transport` descriptor for clients, legacy `control_socket` remains for Unix runs, and malformed descriptors fail closed without leaking private paths or owner tokens into public summaries. (#341)
- Agent adapter detection and dispatch now share one resolved executable identity on Windows, so doctor can mark a seat runnable without `run_agent` later failing with `command not found` after discarding an npm shim. Only native `.exe` files and extensionless paths with a verified PE signature are runnable; `.cmd`, `.bat`, and unverified npm shims fail before inference with `unsupported-command-shim` and path-safe remediation to put the package native executable directory earlier on PATH. Launched provider workspace-trust refusals classify as `provider-preflight` instead of command resolution, and remaining process-creation failures no longer leak absolute paths. (#340)

### Added
- `brigade repos adoption` separates configured harness wiring from observed work-loop use across the local repository fleet. It reports `unwired`, `partial`, `advisory-only`, `enforced-idle`, `active`, `bypassed`, and `stale` rows, correlates Claude session writes with brief, verify, outcome, GraphTrail, MiseLedger, and handoff evidence, exposes fleet denominators and stable JSON monitor keys, and provides a read-only `repair` plan. (#317)
- `brigade operator checkup` accepts repeatable `--surface` selectors, lists stable surface names with `--list-surfaces`, and provides an `evidence-loop` preset for work receipt integrity and outcome capture, GraphTrail health and receipt deltas, and MiseLedger work-receipt import state. Scoped JSON separates `selected_ready` from unevaluated `overall_ready` and reports selected, skipped, and per-surface elapsed data. The default six-doctor checkup is unchanged. (#298)
- The versioned `harness-contract.v1` schema provides safe availability and opt-in version probes with bounded, redacted output. Codex Desktop and Cursor GUI remain external-only, while Antigravity availability resolves only the `agy` and `antigravity` command candidates without executing vendor commands. (#342)
- Project-scoped Claude Code work-loop hooks can brief once per session and repository, route direct verification commands through Brigade, and check completion receipts after write work. `brigade work hooks install|update|status|uninstall` manages the package without replacing unrelated Claude settings or hooks.
- `brigade receipts export miseledger --json` prints a typed `brigade.miseledger_export_result.v1` summary (status `empty`, `nothing-new`, `exported`, or `failed`, plus export/skip/error counts) to stdout while writing JSONL to a named `--out` file. A zero-receipt non-JSON export exits 0 with no output. `--fleet --json` runs the same export across enabled `[[repo]]` entries from `.brigade/repos.toml`, aggregates rows into one batch, keeps per-repo cursors, imports valid partial output at most once, returns `brigade.miseledger_fleet_export_result.v1` with per-repo counts and privacy-safe errors, and treats aggregate import failure as top-level `failed` without incrementing `failed_count`. (#254)
- Route as a third outcome-ledger cohort axis (Phase 1: record and surface, no scoring change). `brigade outcome capture` stamps a coarse route manifest (`followed`, `path`, `size`, sorted `signals`, `coverage`) and a `route_fingerprint` on each record, resolved from the owning `brigade run`'s route. A bare verify-capture with no owning run is the honest unrouted cohort. `brigade outcome explain` splits records into routed-vs-unrouted cohorts with a help-rate, so the ledger can finally answer whether runs that followed a composed route verify greener. Ratchet unchanged; see `docs/design/route-outcome-cohort.md`.

- Recency-weighted outcome ranking (context-aware outcomes follow-up): `outcome rank --recency` (default half-life 45 days, `--recency-half-life DAYS` to override) weights the score it sorts by over recency-weighted counts, so a signal's weight halves every half-life and credit earned under a drifted-away environment fades without rewriting the append-only log. It applies to the pooled score by default and the capability-shrunk score with `--by-capability`; the two flags compose. Off by default, so rank output stays byte-identical. The Wilson bound and shrinkage prior now accept fractional counts; the promotion ratchet never uses recency. See [docs/design/context-blind-spot.md](docs/design/context-blind-spot.md).

## [0.22.0] - 2026-07-13

### Fixed
- Security health and release readiness now honor the latest accepted-risk closeout by exact finding fingerprint, while new or changed findings become active again. Harness-wiring health also follows the configured template-inclusion policy instead of blocking a release on excluded managed-template URLs.
- Route calibration from a task corpus: a docs rewrite (`rewrite the QUICKSTART`) no longer takes the full code route (a bare `rewrite`/`redesign` no longer beats a docs hint), UI words `panel`/`dashboard`/`sidebar`/`widget`/`view`/`tooltip` now pull the UI review lenses, and a migration earns tests the way an auth change does.
- Route derivation no longer misroutes conventional-commit code work as docs: `fix(install): ... referencing docs` and `feat(skills): ship ... CHANGELOG.md` route as code, while prose like `fix typo in README` and `fix(docs): ...` stay docs. A code fix losing its review lenses to a stray "docs" keyword was the one misroute direction that cost coverage.

### Added
- `brigade run --route-signal +auth-surface` / `~ship-requested` and the same flag on `brigade route` force-add or suppress a derived route signal when the heuristic is wrong on one task, a middle ground short of `--no-route`. Forced signals still pull their dependents (a forced `+auth-surface` earns tests and a security review).
- `brigade route --json` now carries `triggered_by` (the live signal that pulled each stage in) and `overrides`, alongside the existing signals/approvals/route/waves/held.
- Property tests fuzz the router over 2,000 random catalogs, asserting the invariants a route must always hold (held stages never route, inputs always satisfiable, waves a strict topological order, cycles raise). Plan attempts now record `unknown_covers`: a `covers` tag naming a stage not in the route is surfaced instead of silently ignored.
- `--route-signal` refuses to add or suppress a path signal (`code`/`docs`/`system`): the path is a derive-time decision, and stripping it collapsed the route to a pathless remnant. Found by dogfooding the new `latent-premises` review skill against the router's own diff.
- `brigade run --wait[=SECONDS]` can wait for an active per-target run lock with a finite timeout; lock conflicts remain fail-fast unless the flag is present.
- Explicit adapter execution modes keep read-only and writable CLI flags aligned, reject unsupported direct Cursor Composer plan runs, and point those seats to the reviewed ACP transport. Authenticated ACP checks passed `composer-2.5` and `grok-4.5`; Brigade does not rewrite the direct-only `grok-4.5-xhigh` alias because acpx exposes no separate reasoning flag.
- The GraphTrail personalized ranking benchmark corpus adds checked synthetic MRR and latency thresholds; the managed snapshot pins that reviewed revision.
- Internal typed run transport and receipt modules separate adapter execution from artifact writing without changing CLI or artifact contracts.
- Capability-aware outcome scoring (Phase 2 of context-aware outcomes): `outcome rank` and `outcome explain` resolve the current runtime capability and score each artifact's content-current records earned under it, with a thin cohort pulled toward the pooled rate by deterministic shrinkage `(helped + kappa*pooled_rate)/(total + kappa)` (one documented `kappa=4.0`). `outcome rank --by-capability` sorts by "what worked under my current context" (shrunk estimate, then on-capability sample size, then pooled). Records with no capability fingerprint are grandfathered into the current-capability cohort, so a pre-context ledger's default rank/explain output stays byte-identical until signals under a different capability accumulate. The default sort and the promotion ratchet are unchanged; the ratchet still scores the pooled current-fingerprint cohort only. Recency decay and a regression-attribution guard are tracked as follow-ups in [docs/design/context-blind-spot.md](docs/design/context-blind-spot.md).
- Runtime-context manifest on outcome records (Phase 1 of context-aware outcomes): `outcome capture` and `outcome record` now stamp each new record with a coarse `context` manifest (Brigade version, interpreter major.minor, platform, plus best-effort harness and model tagged with their source) and a `capability_fingerprint` over the low-cardinality vector `{harness, model_family, python, platform}`. `outcome explain` surfaces a per-capability breakdown. This is capture-and-surface only: scores, rank order, and the promotion ratchet are unchanged, and records without a manifest are grandfathered. A content hash cannot see the harness a signal was earned under; this is the data foundation for cohort-aware scoring. Design and three-model rationale in [docs/design/context-blind-spot.md](docs/design/context-blind-spot.md).
- Transitive card-link fingerprints: a card's `content_fingerprint` now folds in the transitive closure of the cards it `[[links]]` (each reachable card's content hash), so editing a linked card invalidates every card that reaches it, the same logic_tracking idea applied to memory cards. The walk is cycle-safe, deterministic, and tolerant of dead links (a `[[missing]]` link contributes nothing until that card exists). A card with no resolvable links hashes to exactly `sha256(card content)`, byte-identical to the prior scheme, so existing single-card records are never invalidated.
- Skill bundle fingerprints (the ledger's `logic_tracking`): a skill's `content_fingerprint` now covers its whole bundle (every file's path plus content hash, skipping `.DS_Store` and the `skill.json` sidecar), so editing a bundled helper invalidates the skill's signals the same way editing `SKILL.md` does. A skill whose only content file is `SKILL.md` hashes to exactly `sha256(SKILL.md)`, byte-identical to the prior scheme, so existing single-file records are never invalidated. Cards stay single-file; transitive `[[wiki-link]]` tracking remains future work.
- Fingerprint-aware promotion ratchet: `outcome reconcile` and `outcome fork` now score the current-fingerprint cohort when deciding, so an edited skill must re-earn `install_min_helped` against the text that now ships instead of coasting on signals for text that no longer exists. The decision rules (thresholds, cooldown, forward-only ratchet) are unchanged; only the score fed in narrows, and grandfathering keeps a never-edited artifact's decision, receipt, and output byte-identical to before. Decisions that drop proven-stale evidence carry audit fields (`content_fingerprint`, `lifetime_*`, `stale_records`, `legacy_records`).
- Skill content fingerprints in the outcome ledger (CocoIndex's memo-key idea applied to the ratchet): `outcome capture` and `outcome record` stamp each new record with the SHA-256 of the artifact's content (`content_fingerprint`, the installed copy first and the registry master as fallback), and `outcome rank` / `outcome explain` default to a score that drops proven-stale records (fingerprinted against a different revision) while keeping lifetime counts visible, so an edited skill earns its score back instead of inheriting signals for text that no longer exists. Pre-fingerprint records are grandfathered into the current score, so a never-edited skill keeps its score and `rank` output stays byte-identical until a skill is actually edited. The ledger remains append-only and the digest chain unchanged; reconcile/promotion rules are untouched.
- Deterministic route brief for `brigade run`: `route_catalog.derive_signals` maps the task to signals (auth surface, UI, migration, bug, docs, system, ship request), a pure-function router (`brigade/router.py`, algorithm adapted from alp-river, MIT) composes the required stages into dependency-ordered parallel waves with while/until holds, and the plan prompt carries the result. Plans tag assignments with `covers`; a plan missing required stages gets one corrective retry, and gaps are recorded in `plan-attempts.json` rather than failing the run. The route lands in `run.json` telemetry. Opt out with `brigade run --no-route`; release a held ship stage with `--approve-ship`.
- `brigade route <task> [--template --changed-path --approve-ship --json]` shows the composed route for a task: signals, stages with the signal that pulled each in, parallel waves, and held stages. At run time the route also reads git-changed paths (segment-matched) and an optional `--route-template` hint.
- Registry skills `latent-premises` (unguarded-assumption review across input/contract/environment/ordering/cardinality) and `retry-safety` (side-effect re-run safety, migration deploy-window lens), vendored from skillet.
- `brigade stations verify <path>` checks an explicitly selected `station.json` without running install commands. On POSIX, read-only surfaces and narrowly checked help/version probes run without a shell in an isolated temporary home, with process-group cleanup, finite positive timeouts, strict JSON parsing, and a 64 KiB combined output ceiling. Windows fails closed before probe creation. JSON results omit raw child output, control characters are removed from details, unreadable manifests return structured exit 2, and managed-catalog drift is advisory unless `--check-managed` is set.

## [0.21.1] - 2026-07-09

### Added
- Embedded memory-doctor verbs under `brigade memory`: `status`, `lint` (dead wiki-links), `compact` (flatten/tighten MEMORY.md), and `init-git`. Also available as `python -m brigade.memory_doctor`. Handoff promotion stays on `brigade ingest`.
- Search station CLI: `brigade search status|doctor` and review-only `sync plan` for GraphTrail plus optional code-search. Shared station health schema (`station_health`) powers status/doctor/plan across stations.
- Tokens station CLI: `brigade tokens status|doctor` and review-only `wire plan` for Token Glace (current name; TokenJuice is the old name) plus optional `usage-tracker` managed tool under the tokens station.
- `brigade stations discover` finds local `station.json` catalogs (`brigade.station.v1`) and prints `brigade add <path>` next commands.
- Plating managed tool under the guard station for optional demo render / leak scan / drift verify helpers.
- Evidence station CLI: `brigade evidence status|doctor`, review-only `crawl plan` and `export plan`, and `brigade add evidence` next-step banner. MiseLedger stays a process-boundary Go binary; crawl/import are operator-run.
- Pantry station first-class path: `brigade pantry doctor`, status/expiry/setup plans emit explicit `next` commands and product docs links, and `brigade add pantry` prints the multi-machine setup sequence. Agent Pantry stays a process-boundary Go sidecar.
- MCP adapter for Hermes: user-scoped `~/.hermes/config.yaml` under `mcp_servers` (YAML subset, zero-dep surgical merge). Requires `--user-scope`.
- MCP adapters for Grok CLI: project `.grok/config.toml` (`grok`) and user `~/.grok/config.toml` (`grok-user`, requires `--user-scope`). Same Codex-like `[mcp_servers.<name>]` TOML shape. (#183)

### Fixed
- Release preflight now bumps and synchronizes every version stamp before the full verification and cold-start gates, and the checklist confirms the published PyPI version after tagging.
- MCP Codex/Grok empty-`args` fingerprint conflicts: `to_provider` no longer emits `args: []`, matching TOML render which omits empty arrays, so force-sync stays idempotent. (#181)
- MCP import of url-only servers no longer lands as invalid `stdio`+`url`; coerce to `http`/`sse`, including OpenClaw sources with a bogus `transport: stdio` or a command that is actually a URL. (#182)
- Productized GraphTrail ↔ Brigade ↔ MiseLedger dogfood path: `brigade operator checkup` reports optional loop health (`graph` / `ledger` / last and mean `brief_hit_rate` from run receipts) without blocking readiness; `brigade add graphtrail` installs the code-graph tool under the search station; QUICKSTART documents install → checkup → export → rank.
- Outcome rank/reconcile surfaces mean `context_eval.brief_hit_rate` per skill as a quality signal (secondary sort among equal Wilson scores; install/rollback thresholds still use verified exit codes only).
- `brigade-work` skill teaches the full loop: verify with capture → outcome from run → MiseLedger export → evidence brief next time.
- `brigade add` accepts a managed tool name (e.g. `graphtrail`) as well as a station name, so optional loop sidecars install without pulling every tool on the station.

### Changed
- Memory station no longer installs the external memory-doctor package. Maintenance is built into brigade-cli; bootstrap-doctor remains an optional sidecar for OpenClaw prefix trim.

## [0.21.0] - 2026-07-08

### Added
- Verified binary pins for runbooks: a runbook can pin each step's binary by sha256, `runbook plan` shows pin status without executing, `runbook run` refuses on mismatch unless `--allow-pin-mismatch` is passed and records the check (and any override) in the receipt, and `brigade runbook pin <runbook.json>` writes pins from the current binaries. Unpinned runbooks behave exactly as before. (#156)
- Tamper-evident receipts: verify, runbook, and outcome artifacts carry a sha256 `digests` block binding the receipt payload and every referenced log file; `memory/outcome/records.jsonl` is hash-chained with `prev_digest`/`digest` so an edited or deleted middle record breaks visibly; `brigade receipts verify` walks everything and reports OK, MISMATCH, MISSING, or legacy, wired into `brigade doctor` and `outcome doctor`. Pre-existing artifacts report as legacy, not failures. (#157)
- Optional receipt signing: `brigade receipts keygen` writes a local 0600 key, receipts then carry an HMAC-SHA256 `signature` and `key_id` over the receipt digest, and `receipts verify` reports SIGNED-OK, flips SIGNATURE-MISMATCH nonzero, or marks foreign-key signatures unverifiable. Without a key, receipts are byte-identical to before. (#164)
- Code-graph delta receipts: verify runs and `brigade run` snapshot the target's GraphTrail database (WAL-safe), re-sync and diff after, and attach a compact `code_graph_delta` summary to the receipt with the full diff as a digest-covered `graph-delta.json` sidecar. Snapshots are deleted after the diff with their sha256s kept as attestations. Fail-open everywhere; read-only, dry-run, and `--no-code-graph` runs skip capture. (#158)
- MiseLedger receipt export: `brigade receipts export miseledger` emits one `miseledger.adapter.v1` JSONL line per verify-run and run receipt, with `raw.hash` reusing the receipt digest so re-imports dedupe on content identity. `--new-only` adds a cursor so reruns export only new receipts, and `--import` pipes the export straight through `miseledger import adapter`, fail-open when the binary is absent and skipping the import subprocess entirely when nothing new was exported. (#159, #162, #165)
- Git provenance on receipts: verify receipts and run payloads capture `head`, `branch`, and `dirty_files` inside the digest-covered content, and the MiseLedger export emits the GitHub commit URL as a link. (#162)
- Delta-aware outcome ledger: `outcome capture` copies the receipt's compact code-graph delta onto the hash-chained record, `outcome capture --run-receipt <run-id|latest>` captures from run receipts (where code actually changes) with status mapped to signal, and `outcome rank`/`reconcile` report `graph: N changing / M no-op` per subject. Promotion decision rules are unchanged. (#163, #169)
- Context evals on run receipts: when a run had a code-graph brief and its delta captured cleanly, `context_eval` records whether the pre-run context named the files the run actually touched (`hits`, `missed`, `brief_hit_rate`). Absent when there is no brief, no clean delta, or nothing changed. (#167)
- Evidence briefs close the receipts-to-context loop: `brigade run` can attach a capped, fail-open brief of recent verified evidence for the target repo pulled from MiseLedger, always framed as untrusted evidence rather than instructions, gated by `--no-evidence`, and recorded in `run.json`. `brigade work import context --from-miseledger '<query>'` runs the same fetch on demand. (#171)
- Workflow sequence scanner: mines run artifacts for recurring command sequences and proposes runbooks (with empty pin stubs) from what operators actually repeat. (#155)
- Detached run control: `brigade run --detach`, live watch, and steer/interrupt verbs for app-server runs. (#154)
- Vendored content-guard under `brigade.guard`, added `python -m brigade.guard` and `brigade guard ...`, and packaged the guard policies so scrub works without a separate checkout. (#166)

### Changed
- `brigade scrub` now defaults to the embedded `brigade.guard` scanner and policy set. Setting `CONTENT_GUARD_DIR` still preserves the old external checkout behavior, including `python -m content_guard` with that checkout's `src` on `PYTHONPATH`. (#166)

### Fixed
- Run receipts are written atomically, closing a race where a concurrent reader could catch a partially written `run.json`. (#161)
- Friction imports use shared import identity keys, so re-imports no longer duplicate items. (#153)
- The technical guide no longer overstates runbook approval semantics. (#152)

## [0.19.0] - 2026-07-05

### Added
- Managed station tools now declare machine-readable surfaces: live doctor JSON, bounded markdown briefs, summary JSON, and verify commands where each tool supports them. `brigade stations list --json` includes those surfaces so profiles can show what a station can feed into automation before it is installed.
- `brigade add <path>` can discover a local `station.json` manifest, report its install command and machine surfaces, and refuse to run manifest install commands unless `--install` is passed. Built-in station installation by station name still works as before.
- `brigade run` now has a run-level brief budget. Code graph and upstream drift impact briefs are ranked by task type, clipped when needed, and recorded in `run.json` with attached brief names, sizes, and truncation flags.
- `brigade run` can attach a fail-open upstream drift impact brief when a repo has a GraphTrail database and pending upstream-drift state. The brief combines drift report excerpts with `graphtrail impact` output so workers see likely blast radius before editing.
- `brigade memory care scan` now validates `evidence:` frontmatter for receipt-like paths and MiseLedger evidence refs, reporting `missing-evidence-ref` issues and evidence reference counts.
- `brigade pantry expiry-alert` reports near-expiry Agent Pantry sessions and plans an `agent-notify` message by default. It sends only when `--send` is passed.
- `brigade outcome rebuild-status`, `fork`, and `diff` add a drift oracle for outcome receipts. Operators can rebuild `status.json` from records and compare alternate reconciliation configs without mutating live state.

### Changed
- The three largest modules are now packages: `repos_cmd` (6,417 lines -> 8 files), `tools_cmd` (6,079 -> 18), and `phases_cmd` (5,621 -> 7), each behind a facade that preserves the full external surface and monkeypatch semantics; no source file exceeds 2,000 lines. The mypy override list dropped from 45 entries to 21, and type-ratcheting surfaced and fixed four latent defects along the way (a friction-show crash on malformed JSON, a notifications tuple-shape crash, a fleet-health wrong-module read, and a research runner returning an object where callers expected text).
- Agent Pantry health now prefers `agentpantry doctor --json`, uses the old status JSON as a compatibility fallback, and exposes the new `inventory --markdown` brief surface for near-expiry session summaries.

## [0.18.0] - 2026-07-03

### Migration
Upgrading from 0.17.x, two breaking changes need one-time action:

- **Extras surface.** If any script, cron, or habit runs `release`, `center`, `repos`, `research`, `roadmap`, `friction`, `chat`, `context`, `projects`, `learn`, `runbook`, `dogfood`, `pantry`, `notifications`, `budgets`, `untrusted`, either fragments group, or `work phases`, run `brigade extras on` once per machine before upgrading (or export `BRIGADE_EXTRAS=1` in that environment). Disabled commands exit 2 with this guidance rather than failing silently.
- **Minimal repo installs.** `brigade init` and `operator quickstart` at repo depth now write only `AGENTS.md` and `SAFETY_RULES.md` plus gitignored state. If your setup expects `rules/`, `hooks/pre-push`, `INSTALL_FOR_AGENTS.md`, or the default tool packs, pass `--full` (or add the `repo-extras` include). Existing repos are untouched by the upgrade itself; re-running quickstart on an existing repo only adds missing files and never overwrites.

### Docs
- README opening reframed: the one-liner now leads with the per-tool config and memory sprawl instead of abstract nouns, "What it does" defines a receipt as the file it is, and a sample verify-receipt ticket shows one. The receipts wording elsewhere in the opening was trimmed so the word appears where it is the feature, not as a refrain.

### Added
- `brigade run` artifacts now carry brigade-computed ground truth (issue #125): `worker-results.json` and `synthesis.json` include a `ground_truth` block with `git diff --stat`, the changed and untracked file lists, a `changes.patch` reference, and verify exit codes parsed from the actual `.brigade/work/verify-runs` receipts. The synthesis prompt gets a compact brigade-computed facts section, so the chef synthesizes against facts instead of worker narration.
- `brigade run` now attaches a fail-open GraphTrail code-graph brief when `.graphtrail/graphtrail.db` exists. The read-only markdown context is prepended to orchestration and worker prompts, can be disabled with `--no-code-graph`, and records `code_graph_brief` metadata in `run.json`.

### Changed
- `brigade work phases` (the phase-execution ledger) moved behind the extras wall with the other operator-suite surface; it stubs out with enable guidance when extras are disabled.
- The shared `render.emit` renderer now backs the mechanical output sites in `tools_cmd`, `daily_cmd`, `center_cmd`, and `phases_cmd` (110 sites converted, byte-identical output); sites with interleaved logic or stderr remain on direct prints.

### Breaking
- The CLI surface is now split into core and extras. 18 operator-suite command groups (`release`, `center`, `repos`, `research`, `roadmap`, `friction`, `chat`, `context`, `projects`, `learn`, `runbook`, `dogfood`, `pantry`, `notifications`, `budgets`, `untrusted`, `openclaw-fragments`, `hermes-fragments`) register only when extras are enabled. Enable once with `brigade extras on`, per invocation with `BRIGADE_EXTRAS=1`, and check with `brigade extras status`. A disabled extras command exits 2 with that guidance instead of a parse error. The 24 core groups (init, mcp, tools, skills, handoff, ingest, memory, work, outcome, operator, run, roster, runs, daily, security, scrub, doctor, status, add, stations, profiles, reconfigure, completions, extras) are unchanged.
- Repo-depth installs default to a minimal footprint: `AGENTS.md` and `SAFETY_RULES.md` plus gitignored `.brigade/` state and the selected harness inboxes and skills. The full kit (`rules/`, the inactive `hooks/pre-push`, `INSTALL_FOR_AGENTS.md`, and the four default tool packs projected into `tools/` and `scripts/`) moved behind `--full` on `brigade init` and `operator quickstart`, or the `repo-extras` include. Workspace-depth installs are unchanged and always get the full kitchen.

### Fixed
- `doctor`'s memory-card budget check now honors `.brigade/memory-care.toml` (`max_card_bytes` and `exclude_paths`), matching `brigade memory care`. It previously used a hardcoded 8000-byte limit and scanned excluded directories (e.g. `archive/`, `decay/`), so the two subsystems disagreed. The default card budget is now 12000 (single-sourced in `budgets.MEMORY_CARD_BUDGET_BYTES`), reconciling the prior 8000-vs-12000 default mismatch.
- `brigade run --worktree` could write a corrupt `changes.patch`: trailing-whitespace trimming dropped a diff's final blank context line, and `git apply` rejected the file (issue #124). Patches are now written byte-faithful, validated with `git apply --check --reverse` before the run is declared done, and a failed validation keeps the worktree as the recoverable copy and exits nonzero.
- Repo-depth installs no longer point agents at files that do not exist: the generated `AGENTS.md`, `CLAUDE.md`, and `INSTALL_FOR_AGENTS.md` referenced `SOUL.md`, `USER.md`, `MEMORY.md`, and `memory/cards/`, none of which a repo-depth install creates. Repo depth now gets repo-scoped variants; manifest file entries accept an optional `depth` key for per-depth template selection.
- `brigade mcp sync` no longer grows a `.vscode/` directory unasked. VS Code is included only when the repo already has `.vscode/`; the skip is reported in the plan notes and `--harness vscode` still forces it.
- An empty tool catalog (`.brigade/tools.toml` with no `[[tool]]` entries) is now valid, and default tools that were never projected no longer block `operator doctor` readiness. Once tracked sources exist under `tools/`, a missing projection blocks again as drift.

### Added
- `brigade operator quickstart --dry-run` prints the file-by-file plan the README always promised: each planned step lists its dir and file writes, including the operator-init artifacts and the MCP catalog path.
- `brigade extras on|off|status` manages the extras surface via a user-level marker under `$XDG_CONFIG_HOME/brigade/extras`.

### Documentation
- README: the sample doctor output now matches a fresh install (content-guard reported missing until `brigade add guard`), and the no-background-process claim names its one scoped exception (`brigade tools runtime start`).

## [0.17.0] - 2026-07-01

### Added
- Per-adapter model pinning: `agents.<name>.model` now pins a model on `grok`, `opencode`, `pi`, `kimi`, `cursor`, and `antigravity`, in addition to `claude` and `codex`. A registry places each adapter's model flag where that CLI expects it, so `model = "grok-composer-2.5-fast"` on a `grok` agent runs Grok Composer under a Brigade run. `brigade roster doctor` validates every pin and fails before dispatch when an adapter can't pin a model or when an `ollama:` ref carries a stray `model =`. Ollama cloud models run through the existing `ollama:<model>` ref, e.g. `ollama:qwen3-coder-next:cloud`.

### Fixed
- The `grok`, `amp`, and `crush` writer adapters invoked a `--prompt` flag that none of those CLIs accept, so every run through them failed at argument parsing. They now use each CLI's real one-shot form: `grok -p`, `amp -x`, and `crush run`.
- `brigade roster doctor` no longer crashes on endpoint-mode agents (`endpoint` + `model`, no `cli`); it reports the endpoint and skips the CLI model-pin check.

## [0.16.0] - 2026-06-30

### Added
- Built-in station profiles with `brigade profiles list/show` and `brigade stations list`, so new installs can see the default repo, workspace, and fleet station bundles before adding sidecars.
- A default `skills` station that wires `brigade-work` and `ultra-work-scout` into selected harnesses during `brigade init`. `ultra-work-scout` gives agents a broad Scout workflow for large or ambiguous work, and `brigade add skills` now points users at the optional Skillet roster instead of pretending Skillet is a local binary.

### Changed
- The token sidecar is now Token Glace throughout live templates, managed-tool wiring, docs, and tests. `brigade add tokens` installs and doctors `token-glace`.
- The default repo profile now selects the core sidecar set for memory, guard, security, tokens, evidence, search, and skills while leaving host-global or heavier stations such as MCP, pantry, and notifications opt-in.

## [0.15.0] - 2026-06-29

The verified-learning loop can no longer silently mislead. An onboarding audit
adversarially traced the loop end to end and found several ways it reported
success while doing nothing, or quietly poisoned its own ranking. This release
closes them and makes the loop's own health visible.

### Fixed
- `outcome reconcile --apply` no longer marks a skill `promoted` when the physical install fails. The forward-only ratchet never re-emits install for a `promoted` artifact, so a false promotion was permanent and invisible; a failed install now keeps the skill a `candidate` (with cooldown) so a later `skills inbox accept` + reconcile retries. `--apply` output surfaces the execution result so it is never byte-identical to a dry-run that did nothing, and a skill missing from the registry reports `install-skipped: not in registry`. Cards (status-only promotion) are unaffected.
- A verify command rejected by Brigade's own parser (shell metacharacters, a high-risk executable, an unresolvable binary) is recorded with status `rejected`, not `failed`. Invalid input is neutral (0) to `outcome capture`, never a verified regression (-1). The overall receipt is `failed` only when a command actually ran and failed or timed out.
- `outcome` no longer collapses distinct manual signals. A record written without `--evidence` cannot be proven a duplicate, so it counts as its own signal instead of merging into one, and manual `friction`/`learnings` producers can reach the install threshold. Records that carry an `evidence_ref` still dedup as before.

### Added
- `brigade work verify run --capture <id> [--capture-kind skill|card]` captures the run's outcome in the same command, so the loop closes without a separate manual `outcome capture` step.
- `brigade work brief` reports `outcome_loop` health: verify-run, record, scored, and promoted counts, plus a warning when verify runs exist but the ledger is empty (loop half-fed) or neither exists (loop dormant).
- `outcome capture` warns when the artifact id is not a known installed skill or memory card, naming what is available, so a hallucinated id stops silently poisoning the ranking (the record is still written).
- `.brigade/work/verify-runs/` is capped (newest 50 retained) so receipts and raw command logs no longer grow without bound.

### Changed
- The `brigade-work` skill, README, and QUICKSTART teach the daily loop directly: capture against an id you actually have (a real skill, a memory card, or `brigade-work` itself), the one-step `--capture` form, and the registry-accept prerequisite for autonomous promotion.

## [0.14.1] - 2026-06-29

### Changed
- `brigade init` is now additive on an existing target instead of aborting: it keeps existing files (never clobbers without `--force`), writes only the missing ones, and **always wires the `brigade-work` skill into the harness skill dirs**. This fixes the upgrade/brownfield gap found in onboarding smoke tests, where a repo that already had `AGENTS.md`/`CLAUDE.md` (every previously-onboarded repo, and most real projects) got nothing wired because `init` refused to overwrite and exited. Re-running `init` after upgrading now wires the work loop without losing local edits; `--force` still overwrites to refresh the bootstrap directives.

## [0.14.0] - 2026-06-29

### Added
- `brigade init` and `operator quickstart` now wire the new `brigade-work` skill into each selected harness's skills directory by default (`--no-wire` to opt out) and print agent-facing onboarding, so a fresh install is actually used by the agent's work loop instead of sitting dormant. The skill teaches the loop: `brigade work brief` at the start, verify THROUGH `brigade work verify run` (not raw), `brigade outcome capture <skill-or-card-id> --run-id latest` after each verify, and a handoff at the end. Mandatory "Daily Work Loop" directives were added to the `AGENTS.md` and `CLAUDE.md` templates (replacing the conditional internal-dogfood block, keeping `AGENTS.md` under the bootstrap budget), plus a "use it, don't just sit next to it" section in `INSTALL_FOR_AGENTS.md`.

### Changed
- README now leads the quickstart with a recorded, clean-machine demo (`docs/assets/quickstart.svg`, reproducible from `docs/assets/quickstart.cast`) and adds real `operator doctor` readiness output plus a real `brigade mcp sync` dry-run diff, so the headline claims are shown working, not just described.

## [0.13.0] - 2026-06-23

### Added
- `brigade mcp` syncs one canonical MCP server catalog (`.brigade/mcp.json`) into each tool's native MCP config file. `init`/`add`/`list` build the catalog; `plan` previews; `sync` (dry-run unless `--write`) merges into Claude (`.mcp.json`), Cursor (`.cursor/mcp.json`), Codex (`.codex/config.toml`), VS Code (`.vscode/mcp.json`, with `inputs`), OpenCode (`opencode.json`), and Antigravity (`~/.gemini/config/mcp_config.json`, user-scoped via `--user-scope`); `doctor` validates; `import` reads an existing tool's config back into the catalog. The merge is by server key with ownership tracked in the gitignored `.brigade/mcp/state.json`: servers the user added are preserved, a server edited outside Brigade is a conflict (skipped unless `--force`), orphans are removed only with `--prune` and only when still pristine, and env values are always written as `${VAR}` references (or VS Code `${input:VAR}`), never inlined secrets. `brigade operator sync-mcp` wraps it with a validate->sync->summary receipt. New `mcp` station (alias `brigadier`).
- `brigade mcp` user-global adapters `codex-user` (`~/.codex/config.toml`), `claude-user` (`~/.claude.json`), and `openclaw` (`~/.openclaw/openclaw.json`), so a machine's daily tools can be synced alongside repo-local configs. `brigade mcp import --keep-secrets` keeps literal env secrets verbatim instead of demoting them to `${VAR}` references, for unioning existing working configs whose tools do not expand `${VAR}`.
- `brigade outcome` verified-autonomous learning loop (`capture`/`record`/`score`/`explain`/`reconcile`/`rank`): a learned skill is promoted only when a model-unauthored signal (verify exit codes, friction/learnings deltas) confirms it helped, Wilson-scored so thin evidence never out-ranks vetted skills. `reconcile` is dry-run by default; with `--apply` it installs across harnesses or rolls back on a measured regression. The durable ledger is git-tracked under `memory/outcome/`, readable without Brigade.
- `brigade context` packs now carry a `code_graph` section from GraphTrail's read-only `graphtrail context` (entry points, related files, caller/callee counts for the selected task). The binary resolves via `$GRAPHTRAIL_BIN`, PATH, then `~/.cargo/bin`, and the section is skipped gracefully when the binary, the repo's `.graphtrail` db, or a task is absent.
- `brigade operator quickstart` now scaffolds the MCP on-ramp: it creates the canonical `.brigade/mcp.json`, previews the projection with a read-only `mcp plan`, and surfaces `brigade mcp init` / `sync --write` in the printed next steps. It never writes harness MCP configs automatically and respects `--dry-run`.

### Changed
- The "Brigade does not write runtime MCP configs / auto-sync harness configs" statements in the tool catalog and technical guide are reworded: runtime MCP *server* config sync is now an explicit, bounded capability provided by `brigade mcp` (dry-run by default, merge-by-key, never inlines secrets, never automatic). The tool-catalog `mcp` *family* projection remains a documentation stub.
- The Hermes adapter is graduated from experimental to validated (verified against a real Hermes v0.17 install): the handoff contract and reviewed skill install both work, and the "experimental" framing is dropped across the harness picker, doctor, install/fragment notices, templates, README, and QUICKSTART.

### Fixed
- MCP sync is now idempotent for remote (http/sse) servers on Codex (`_codex_render_table` renders the `type`/transport, so a re-sync no longer falsely reports a `conflict`). Dotted/quoted server names (e.g. `io.github.example`) round-trip without duplicating into invalid TOML, and the Python 3.10 `toml_compat` fallback reader now parses quoted dotted table keys. Authorization headers for remote servers are emitted and parsed for codex, vscode, opencode, and openclaw (previously dropped), kept as `${VAR}` references.
- `brigade outcome` no longer double-counts a re-captured or retried verify run: scoring counts distinct verified evidence keyed on `(source, evidence_ref, task_id)`, so an identical receipt cannot cross the auto-install threshold on its own.
- `brigade runbook run` honors only an operator-supplied `--approved`; a file-embedded `approved: true` no longer authorizes shell execution. Commands are validated whole (not just the first token), inline-script shell wrappers that negate the allowlist are rejected with a warning, and `SECURITY.md` documents that runbook steps are arbitrary shell, as trustworthy as the file author.
- Hermes skill installs now reach the real Hermes store (`$HERMES_HOME/skills/brigade-imports/<id>`) with rendered frontmatter, gated on the Hermes home existing, so reviewed skills appear in `hermes skills list` instead of an unread repo-local path.

## [0.12.0] - 2026-06-16

### Added
- `brigade doctor --json` and `brigade status --json` emit machine-readable output (target, harnesses, owner, depth, per-check status/name/detail, summary counts, and a `ready` flag), so the two most diagnostic surfaces can feed scripts and a future fleet aggregation instead of being text-only.
- `brigade security diff --base <dir> --against <dir> [--json]` compares two security reports and reports new, resolved, and persisting findings (matched by the scan's stable per-finding fingerprint). It returns nonzero when there are new findings, so a change that introduces a finding can be caught in review or CI.
- `brigade operator checkup [--json]` runs every read-only first-run doctor (`doctor`, `operator doctor`, `handoff doctor`, `tools doctor`, `skills doctor`, `security doctor`) in one pass and rolls them up to a single `ready` / `blocking_surfaces` verdict with the next command to run, so a new operator no longer has to copy-paste each doctor from the first-10-minutes guide.
- `brigade doctor` now lints AGENTS.md quality: it warns (never fails) when the file lacks a "Definition of Done" section or a memory-handoff section, since agents work better with explicit done criteria and a handoff footer. The Brigade-seeded AGENTS.md now ships a Definition of Done section (issue #84).
- `brigade run --read-only` now warns when an assigned agent cannot hard-enforce read-only. Each adapter is classified hard (native sandbox or tool allowlist), soft (prompt-only), or none; soft and none agents are listed on stderr and recorded in a `read-only-enforcement.json` run receipt, and a writable `--sandbox` override downgrades even natively-sandboxed CLIs to best-effort (issue #87).
- `brigade repos doctor --deep` runs the operator checkup (every first-run doctor) in each enabled fleet repo and aggregates a fleet-wide `ready` / `blocking_repo_count` verdict, so you can health-check many repos in one pass instead of cd-ing into each (issue #78).
- `brigade completions bash|zsh|fish` prints a static shell completion script generated from the CLI's own command tree (zero runtime dependencies; the tree is embedded, not shelled out). bash completes the full command tree, zsh reuses it via bashcompinit, and fish completes the top level plus one subcommand level (issue #89).
- `brigade memory search <query> [--json]` runs a deterministic keyword search over memory cards, ranking title, tag, and summary matches above body matches (the stdlib precursor to the roadmapped on-device semantic retrieval) (issue #90).
- `brigade memory serve-mcp --stdio` exposes memory cards over a read-only MCP stdio server under a `card://` scheme (resources plus `list_cards` / `get_card` / `search_cards` tools), reusing the proven skills MCP pattern; reads are scoped to the configured card roots and it never edits canonical memory (issue #88).
- `brigade projects doctor` adds the station doctor that every peer station already has, reporting project-consolidation health and exiting nonzero on issues (issue #90).
- `brigade skills uninstall <skill> --target <harness|all>` removes an installed skill (the inverse of `skills install`), records an uninstall receipt in the install history, and refuses cleanly when nothing is installed (issue #90).
- `brigade friction show [--severity] [--json]` reads back the latest friction scan, which previously could only be written (issue #90).

### Changed
- Internal: the skills and memory MCP stdio servers now share one harness (`mcp_server.serve_stdio`) instead of carrying near-identical JSON-RPC loops. No command surface or protocol change; a new read-only MCP surface is now a few callbacks.
- The repo fleet scan now summarizes repos on a small thread pool instead of serially. Each summary is independent and IO-bound (git calls plus file stats), so a multi-repo fleet scans noticeably faster while output stays in config order; a single-repo fleet is unchanged.
- `brigade doctor` now groups host-global findings (OpenClaw config, the content-guard clone, uninstalled managed tools) under a "machine-level (not specific to this repo)" header in text output and tags each check with a `scope` of `repo` or `machine` in `--json`, so a single-repo run no longer reads as if the repo is responsible for machine-wide state (issue #80).
- `brigade security suppress` and `brigade security unsuppress` gain `--json`, so an agent or CI step can parse the suppression result (issue #90).
- `brigade scrub` gains `--json` and now writes a summary-only `.brigade/scrub/latest.json` receipt (verdict, policy, exit code, never the matched snippets), with `--no-receipt` to opt out, giving the egress gate the audit trail every other station already has (issue #90).
- `brigade runbook closeout --import-issues [--dry-run]` routes each failed step of a runbook run into the work import inbox (deduped by run id and step), so a mid-run failure reaches the same review queue every other station's failures land in (issue #90).
- `brigade init --git-exclude` writes Brigade's ignore block to the local-only `.git/info/exclude` instead of the tracked `.gitignore`, for third-party clones you do not want to commit Brigade ignores into (issue #81). Automatic detection of a third-party clone is intentionally deferred: there is no reliable repo-owner signal, and a guess would false-positive on your own published repos.

### Fixed
- `brigade init --no-gitignore` is now honored. The flag was parsed but never forwarded to the installer, so a `.gitignore` was written anyway; `install_selection` now takes `update_gitignore` and skips the write when asked.
- `brigade init --git-exclude` now resolves a linked-worktree `.git` file (`gitdir: <path>`) to write the exclude under the worktree's own git dir, instead of silently falling back to a tracked `.gitignore`.
- The seeded `pre-push` content-guard hook no longer reports a scanner error as a content leak. It now discriminates content-guard's exit codes: exit 1 (findings) blocks with the leak message, any other nonzero exit is reported as "scanner failed to run" rather than mislabeled as violations (issue #82).
- `brigade doctor` memory-care freshness no longer reports a same-day scan as "in the future". The scanner stamps `scan_date` in UTC, but doctor compared it against the host's local date, so an evening run in a behind-UTC timezone warned falsely (issue #83). Doctor now compares in UTC.
- `brigade init --depth workspace` now creates `.brigade/memory-care/decay`, the directory doctor actually checks, instead of the legacy `memory/cards/decay`. A fresh workspace no longer draws a "staleness scanner not wired" warning on first contact (issue #79).
- `localio.write_json` now writes receipts atomically (temp file plus `os.replace`), so a reader or a crashed writer never observes a half-written JSON receipt; on failure the original file is left intact and the temp file is removed.
- Apply the v0.11.0 subprocess hang-guard (closed stdin plus a timeout) to the remaining direct `subprocess.run` sites that lacked it: `localio.check_git_ignored`, the work-family `_git` helper, and scrub's git probes. A stuck git call now fails fast instead of hanging the command.
- `brigade scrub` fails closed when the content-guard scan times out: a hung scanner is reported as `blocked` (exit 124) instead of being able to let content past the egress gate.

## [0.11.0] - 2026-06-13

### Added
- `brigade run` now guards dirty git worktrees by default, supports `--allow-dirty`, prevents concurrent runs with a local lock, and can run agents in a detached `--worktree` while capturing the resulting `changes.patch`.
- `brigade run` plans can stage worker assignments so dependent workers receive earlier-stage results while same-stage workers still run in parallel.
- Roster agents can pin a model with `model = "..."` for the claude and codex adapters (`claude --model`, `codex exec -m`), so one roster can split an architect model from builder models; pins are recorded in run artifacts.
- Rosters can set `limits.sandbox` (`read-only`, `workspace-write`, or `danger-full-access`) as the default native Codex sandbox for `brigade run`, and runs without a repo roster now fall back to `Path.home()/.brigade/roster.toml`.
- `brigade run --sandbox` to override the native Codex sandbox mode while keeping `--read-only` available for prompt-level review rules.
- `brigade friction`: mine workflow friction from notes and session artifacts into a reviewable report.
- Three new writer harnesses: Grok CLI (`grok`), Amp (`amp`), and Crush (`crush`), each with its own `.{harness}/memory-handoffs` inbox, tool projections, skills adapter, and agent argv, bringing the writer-harness total to eighteen.

### Fixed
- Agent subprocesses now run with stdin closed, so `codex exec` no longer hangs until the roster timeout when brigade itself runs with a piped, never-closing stdin (background launchers, CI wrappers).
- `brigade research sources` no longer misreports route status when a configured source adapter is missing its `type`: the malformed entry previously shifted every following source's executable check onto the wrong adapter.

### Changed
- Split the `work_cmd` test suite into per-area modules while preserving facade patch coverage.
- Internal restructuring with no command surface changes: `operator_cmd` and `work_cmd/services` split into focused modules, the CLI moved to per-command dispatch modules, and shared git-ignore/TOML/datetime helpers moved to neutral homes. The lint gate now includes ruff bugbear (B).

### Documentation
- README opens with a "Try it in 60 seconds" block (install, wire one repo, verify) above the project story, so a first-time visitor reaches a runnable command without scrolling.
- Added `.github/PULL_REQUEST_TEMPLATE.md` covering the contributor checklist already documented in CONTRIBUTING.md: tests, changelog discipline, the content-guard PII gate, zero-runtime-deps, and conventional commits.

## [0.10.3] - 2026-06-10

### Fixed
- `brigade operator verify-harness` treats warn-status checks (host advisories like a global gitignore shadowing an inbox template) as informational: `ready` now flips only on failures, a new `warning_count` reports advisories, and quickstart consequently returns `status: ok` on such hosts while surfacing the advisory count and the verify command on the step line.
- `docs/first-10-minutes.md` explains the `status: warn` / exit-code semantics on first apply and warns that activating the repo hooks path would override an existing global `core.hooksPath` that already runs content-guard.

## [0.10.2] - 2026-06-10

### Added
- `brigade handoff migrate`: converts near-miss homegrown handoff notes (loose `- Type:` style metadata) into the Brigade template through the standard draft renderer. Dry-run by default; `--apply` rewrites convertible notes, preserves originals under `migrated-originals/`, verifies the result passes lint, and records a receipt. Injection-flagged notes are never converted. This closes the top adoption friction: existing notes no longer fail lint identically to garbage.

### Fixed
- `brigade handoff lint` now reports prompt-injection signal counts per file (content-guard checks egress, not instructions), so a poisoned note can no longer read as fully clean from the lint path.
- `brigade doctor` collapses missing optional managed tools into one summary line instead of a per-tool wall, keeping single-workspace reports readable on first contact.
- `brigade operator adopt plan` counts guidance files and guidance directories separately instead of calling `memory/cards/` a file.

## [0.10.1] - 2026-06-10

### Fixed
- `brigade operator doctor --profile local-operator` no longer reports `ready: no` when content-guard is installed but its pre-push hook is inactive. The hook ships inactive by design, so a fresh setup now treats `content_guard_hook_not_enabled` as advisory under the local-operator profile; the strict `internal-dogfood` profile still blocks on it. Found by the new cold-start gate running with neutralized git config: the maintainer's global hooksPath had masked the clean-machine behavior in every earlier test.

### Added
- Cold-start release gate: `docs/runbooks/cold-start-gate.{json,sh}` executes the documented new-user journey (install, quickstart, handoff draft and lint, doctors, gitignore regression guards) in a neutral sandbox, wired into the RELEASE.md pre-tag checklist. `docs/cold-start-testing.md` documents it plus the three agent-driven cold-start scenarios that have been finding first-contact bugs all week.

## [0.10.0] - 2026-06-10

### Added
- `brigade memory care backfill`: bulk-repairs cards that predate the freshness convention by deriving `last_reviewed` from each card's last git commit date (file mtime outside git, labeled as such) and proposing `fresh_until` from the configured stale window. Dry-run by default, `--apply` writes frontmatter without ever overwriting existing values and records a receipt under `.brigade/memory-care/backfills/`.
- New `evidence` station (alias `ledger`) wiring the MiseLedger family of managed tools: `miseledger` (local-first evidence ledger, doctored via `status --json`), `stationtrail` (agent-session log exporter, doctored via `doctor --json`), and `sourceharvest` (source-system record exporter, presence-checked via `version`). All three install from `github.com/escoffier-labs/...` and are advisory: they inspect host-global state and never FAIL a workspace doctor run.

### Changed
- Corrected stale `github.com/solomonneas/...` install URLs and doc links for `content-guard`, `agent-notify`, and `solos-cookbook` to their actual `escoffier-labs` org, so `brigade add` installs from the right repos.
- Internal refactor, no behavior change: shared helpers extracted into `brigade.localio` (JSON/JSONL/timestamps/hashes/slugs), `brigade.actionqueue` (action-queue lifecycle for center/repos/release stations), and `brigade.reportstore` (report-bundle lifecycle for center, repos, release candidates, and fleet release trains); and the 11.8k-line `work_cmd` module is now a package (`constants`, `helpers`, `ledger`, `config`, `services`, `session`) behind an explicit re-export facade guarded by a frozen surface test. Station variants with genuinely different semantics (phases actions/reports, release-train privacy conventions) intentionally stay local.

## [0.9.3] - 2026-06-09

### Fixed
- Content Guard hook detection no longer reports "installed" when `core.hooksPath` is inherited from global or system git config and points at a pre-push that does not run content-guard. Inherited hooks that do run content-guard stay green; unrelated ones report a new `external-hooks-path` mode with a `content_guard_hook_unrelated` warning and the activation command.
- `brigade operator verify-harness` now detects when an inbox's deliberately un-ignored `TEMPLATE.md` is shadowed by an external ignore source (commonly a global gitignore entry like `.claude/`), which git cannot override and which was previously silent.

## [0.9.2] - 2026-06-09

### Fixed
- `brigade handoff sources init` now scopes the default watched inboxes to the workspace's configured harness selection instead of enrolling all fifteen writer inboxes; explicit `--inboxes` values are unchanged.
- `brigade untrusted scan` refuses a bare file path as positional text (it was silently scanning the path string itself) and points at `--from-file`.
- `brigade handoff doctor` collapses absent, unwatched writer inboxes into a single summary line instead of printing one row per unselected harness.
- `brigade operator quickstart` says when the memory owner was auto-selected and how to override it, and operator boundary text now says Brigade never *activates* hooks (the templates ship an inactive `hooks/pre-push` file).

### Documentation
- `docs/first-10-minutes.md` gains a "Handoff Concepts In 60 Seconds" section covering inbox choice, types, card versus no-card actions, and the required content flags.

## [0.9.1] - 2026-06-09

### Fixed
- `brigade security scan` now screens pending handoff notes for prompt-injection signals as a dedicated `handoff-injection` check with a `handoff-inbox` surface. Inboxes were previously skipped entirely so note content was not attributed to the repo author, which also meant a malicious pending note scanned clean; it now reuses the same untrusted-context signal the ingest gate uses. `processed/` and `TEMPLATE.md` stay excluded.
- `brigade memory care scan` writes its state under `.brigade/memory-care/decay/` instead of inside the user's `memory/cards/` tree. Explicit `output_path` configs are honored unchanged, and readers (import-issues, status, doctor) fall back to the legacy `memory/cards/decay/` location when it still holds the latest scan.

## [0.9.0] - 2026-06-09

### Fixed
- `brigade tools defaults`, `tools init`, and `tools pack import` no longer rewrite the managed `.gitignore` block with a hardcoded codex-only selection, which was silently dropping the other selected harnesses' handoff-inbox ignore entries (found by a cold-start test: a codex+claude quickstart left `.claude/memory-handoffs/` commit-prone).
- `operator quickstart --dry-run` now reports every step as `planned` instead of printing `[ok] brigade-init` for a step it did not run, and `brigade init --dry-run` marks files that already exist as refused-without-force instead of implying it would overwrite them.
- `brigade handoff draft` now states clearly when a draft was written but failed lint, with the kept path and next step; the `--content/--content-file` help text now says one of them is required; and the README handoff example includes `--content` so it works verbatim.
- `docs/first-10-minutes.md` no longer pins an old version string, describes the doctors' real per-check output shape, and documents everything quickstart actually writes (bridge files, safety rules, the inactive pre-push hook, and the deliberately un-ignored inbox templates).

### Changed
- `brigade --help` now opens with a short start-here block and lists commands in five groups (core memory loop, daily operator loop, stations and tools, review/security/research, wiring and advanced) instead of one flat 36-command dump. Subcommand help screens are unchanged, every command stays functional and listed, and `docs/command-inventory.md` is unaffected.

## [0.8.2] - 2026-06-09

### Fixed
- Quickstart and `brigade tools defaults` now scope built-in tool projections to the workspace's selected harnesses plus the neutral `scripts/` folder, so a `--harnesses codex` setup no longer writes folders for every supported harness.
- The workspace `AGENTS.md`, `INSTALL_FOR_AGENTS.md`, and `handoff-flow` card templates now render the selected writer handoff inboxes instead of hardcoding `.claude/memory-handoffs/`, and the missing-template fallback now points at `brigade handoff-template` instead of a host-private path.
- The Hermes adapter README now opens issues against the correct repository.

### Documentation
- Added a first-10-minutes guide and compact support response templates for install, quickstart, doctor, commit-scope, homegrown setup, and security questions after launch.

### Added
- Dogfood runs can now use configured `--agent-cli` adapters for Claude Code, Codex, OpenCode, Antigravity through `agy --print`, Pi through `pi -p`, Cursor through `cursor-agent -p`, Aider, Goose, Continue, GitHub Copilot CLI, Qwen Code, Kimi Code, AdaL, OpenHands, or `ollama:<model>` instead of being hardwired to Codex.

## [0.8.1] - 2026-06-08

### Added
- Operator adoption migration rollup: `brigade operator migration status/doctor/import-issues/consolidate` summarizes redacted adoption progress across operator config, external surfaces, review receipts, pending imports, and pending tasks, then lets current migration rollups supersede tiny record-level follow-ups without exposing raw scheduler or process details.
- Redacted operator surface registry: `brigade operator surfaces capture/list/doctor/review/reviews/import-issues` captures shell crontab, OpenClaw cron, and PM2 coverage as counts, status totals, ordinal labels, review decisions, and fingerprints under `.brigade/operator/surfaces/`, omitting raw cron lines, job names, process names, command paths, host details, and environment values.
- Existing-operator adoption loop: `brigade operator adopt plan/capture/import-issues` inventories guidance files, harness roots, handoff inboxes, local state folders, and count-level external scheduler/process surfaces before changing a homegrown operator workspace.
- Research handoff export: `brigade research export-handoff`, `brigade research handoffs doctor`, and `brigade research handoffs import-issues` route completed research runs into selected writer harness inboxes as linted Memory Handoffs, track export fingerprints, and surface missing or stale exports without ingesting memory.
- Operator notification visibility: new `brigade notifications status` and `brigade notifications setup plan` commands inspect optional `agent-notify` wiring and print reviewed Codex/Claude hook snippets without sending messages, editing hook files, or storing channel secrets. Notification health now appears in `brigade doctor`, `brigade center status`, `brigade work brief`, and `brigade daily status/plan` as an advisory setup item.
- Internal dogfood bootstrap: `brigade operator init --profile internal-dogfood` writes repo-local production dogfood config defaults and refreshes read-only security evidence, while `brigade operator status --profile internal-dogfood` reports machine-vs-repo wiring, gitignore state, dogfood readiness, daily health, security evidence, notification config, and local readiness.
- Internal dogfood guidance: `brigade operator guide`, `docs/internal-dogfood.md`, and the workspace `AGENTS.md` template now document the explicit Brigade loop, repo onboarding command, handoff expectations, and no-daemon/no-remote-mutation boundaries.
- Cross-harness operator tool sync: `brigade operator sync-tools` projects tracked `tools/*.md` sources into local Claude, Codex, and OpenCode folders while keeping generated harness files ignored.
- Portable workspace bootstrap: `brigade operator bootstrap-portable` imports optional tool and skill packs, merges built-in portable tools, writes missing built-in source files, projects managed tool outputs across local harness folders, and reports tool plus skill health.
- Security scanner secret response guidance: secret findings now include redacted response options for `.env` or environment storage, scrub/rotate, KeePass review, and session transcript redaction. Session and chat transcript paths are classified as `surface: session-chat` for exposed API keys, tokens, passwords, and private keys.
- Reviewed self-learning skill proposals: `brigade learn skill-candidates` groups repeated learning evidence into reusable skill candidates, and `brigade learn propose-skill <candidate-id>` writes an unreviewed generated skill source plus a normal skill inbox proposal without accepting, installing, publishing, or editing memory automatically.
- New-user quickstart: `brigade operator quickstart` runs the repo template install, local operator config, portable tool and skill bootstrap, and selected harness checks as one local-only first-user flow, with `--dry-run` support.
- New-user issue support: quickstart JSON now includes a compact `issue_report`, the README links a first-run troubleshooting guide, and GitHub issue templates use Brigade naming with a dedicated quickstart setup form.
- Learning skill proposal usability: `brigade learn skill-candidates --source <source>` filters repeated evidence by producer, security scan imports preserve safe grouping metadata and response options, and `brigade learn propose-skill --dry-run` previews generated source plus inbox writes before creating a proposal.
- Compact operator readiness verdict: `brigade operator doctor --profile internal-dogfood` prints a short ready/not-ready summary, blocker count, next command, and local tracked-vs-generated reminders.
- Handoff draft writer: `brigade handoff draft` writes a linted Memory Handoff draft using Brigade's card/no-card section style, so agents can record workflow changes without hand-writing boilerplate.
- Plan-First Operator Loop: `brigade work task plan <id> --write` now writes a plan.md + JSON plan artifact (assumptions, acceptance, risks, steps, next safe command); `--meta` writes a plan-for-the-plan; `--from-research <run-id>` attaches a research report as quarantined evidence. New `brigade work plans`, `brigade work import context` (untrusted raw-context intake), `brigade work plan-promote`/`work plan-proposals` (accepted plans -> local draft template/rule/skill proposals, never installed). `work doctor`/`work brief` surface pending tasks lacking a plan artifact.
- First-class OpenCode handoff support: `.opencode/memory-handoffs/` is now a built-in writer inbox (install scaffolding, ingest, doctor, handoff doctor source coverage, fleet sweep, security skip-list, and the interactive selector), so OpenCode handoffs ingest without a manual `--handoff-inbox` flag. The writer-inbox map is now centralized in `brigade.selection.WRITER_INBOXES`.
- Shared untrusted-context policy helper (`brigade.untrusted`): `wrap_untrusted` frames external content as data-not-instructions with a content-derived fence, and `scan_untrusted` reports injection signals. Adopted in the research extractor, and handoff ingest now routes injection-flagged content to the review inbox instead of auto-filing it into a card or document.
- New `research` command group: local-first iterative deep research grounded in a
  trusted local corpus, with an opt-in, quarantined web tier (Playwright, no API
  keys required). Emits a self-contained HTML report and a memory handoff; runs
  persist under `.brigade/research/` and are resumable/cancellable. Uses the
  roster `researcher` model (cloud); Brigade never runs a model locally.
- New `pantry` station (alias `larder`) and the `agentpantry` managed tool. `brigade add pantry` installs agentpantry via `go install`, and `brigade doctor`/`brigade status` health-check it by shelling out to `agentpantry status --json`. Like the memory satellites, agentpantry inspects host-global state, so its checks are advisory and never FAIL a workspace run: an unwired install (no config) is a `WARN`, a missing pre-shared key is a `WARN`, otherwise `OK`.

### Fixed
- `brigade operator quickstart` now scopes `.brigade/handoff-sources.json` to the selected writer harnesses and writes an initial local handoff-ingest latest-run log, so a fresh one-harness setup does not warn about unwired side harnesses or a missing ingestor log.
- `brigade operator doctor --profile local-operator` no longer points brand-new local setups at release-readiness import work as the primary next command when the only readiness item is a missing release receipt.
- `brigade daily status` now uses a lightweight daily center snapshot and bounded status sections so slow readiness subsystems report warnings instead of hanging the daily loop.
- Operator migration imports now supersede stale rollup imports when a source fingerprint changes, preventing older replacement batches from staying ahead of current rollups in the daily queue.
- `brigade operator doctor --profile local-operator` no longer treats a generated but unenabled Content Guard hook as a blocking issue in fresh quickstart installs. Missing Content Guard remains visible as advisory setup state.
- Removed an unsafe temp-directory cleanup example from the init failure issue template and contributor smoke-test docs, replacing it with `mktemp -d` fresh target setup.
- The security scanner no longer reports its own plaintext-password detector variable names as findings while scanning Brigade source.
- Corrected the stale `github.com/solomonneas/...` install URLs for the `memory-doctor` and `bootstrap-doctor` managed tools to their actual `escoffier-labs` org, so `brigade add memory` installs from the right repos.
- Dedupe guard in `brigade ingest` document routing. A `no-card` route whose content (or its first meaningful line/anchor) is already present in the target document is now sent to the review inbox instead of being appended again, matching the canonical pipeline and preventing duplicate content on re-routed handoffs.

### Documentation
- Documented the existing-operator adoption path, migration rollup, redacted surface reviews, and security scanner first-response workflow in the README, quickstart, technical guide, and agent-assisted setup guide.
- Added a first-response checklist for likely real credential findings, covering redacted review, `.env` or environment storage, KeePass preservation, transcript scrubbing, rotation, and closeout.
- Refreshed contributor setup guidance for Brigade naming, current source paths, quickstart issue reports, and intended GitHub label taxonomy for public support triage.
- Added expected quickstart and operator doctor output to the new-user quickstart guide.


## [0.8.0] - 2026-06-01

### Added
- Canonical flat bootstrap thresholds in `brigade.budgets` (`DEFAULT_BOOTSTRAP_SOFT_LIMIT`, `DEFAULT_BOOTSTRAP_HARD_LIMIT`, `BOOTSTRAP_HARD_LIMIT_CEILING`) for the whole-file auditor model, so downstream bootstrap tooling can source one set of limits instead of redeclaring its own.
- Handoff backlog detection. `brigade handoff doctor` (and the memory station in `brigade doctor`) now emits a `handoff_backlog` warning when an inbox has pending handoffs whose oldest entry is older than three days, i.e. handoffs are being written but nothing is ingesting them. `InboxHealth` gained an `oldest_pending_age_seconds` field. At the fleet level, `brigade repos scan`/`doctor` now emit a `repo_handoff_backlog` warning for any fleet repo with an un-ingested, stale handoff pile-up. This catches the silent gap where a repo's inbox is never reached by the canonical ingester (for example an uncovered repo missing from the ingest config).
- Canonical budgets module `brigade.budgets` is now the single source of truth for bootstrap-file byte budgets, memory-card budgets, the MEMORY.md index line limit, and the handoff-backlog and memory-care staleness thresholds. `doctor`, `ingest`, `handoff`, and `repos` all consume it so preventive guards and post-hoc warnings can never disagree. Satellite tools (bootstrap-doctor, memory-doctor) are intended to depend on brigade and consume these definitions rather than redeclaring them.
- Bootstrap budget guard in `brigade ingest`. A `no-card` handoff that would push a bootstrap file (e.g. `TOOLS.md`, `USER.md`) past its byte budget is now routed to the review inbox instead of appended, so the ingester can no longer silently bloat a session-prefix file past its truncation ceiling. Non-bootstrap targets such as `.learnings/*` are unaffected.
- `brigade repos ingest` fleet driver. Sweeps every registered, reachable fleet repo, routing each repo's handoffs into the canonical owner's memory and archiving the processed handoffs back in the source repo. Defaults to a dry run; pass `--apply` to write. `ingest.run` gained an `owner` parameter and a reusable `ingest.ingest_into` core to support the many-writers/one-owner model.

### Changed
- `brigade work backup init` now writes wider staleness thresholds for the `cloud` destination (`snapshot_stale_hours = 192`, `check_stale_hours`/`prune_stale_hours = 336`) so an off-site copy on a slower cadence such as a weekly backup does not report stale every day. The `nas` destination defaults are unchanged. Existing `.brigade/backups.toml` files are not modified.

### Documentation
- `docs/backup-health.md` now documents the two-tier NAS-frequent plus cloud-weekly threshold pattern and clarifies that backup health monitors snapshot-history backups (restic/borg), not bidirectional last-writer-wins file syncs such as a KeePass database mirror.

## [0.7.0] - 2026-05-31

### Added
- `brigade work phases session checkpoint <session-id|latest>` for local AFK session recovery points that record safe summaries, notes, current next-step state, and suggested commands without executing anything.
- `brigade work phases session checkpoints list/show/compare` for text and JSON inspection of local AFK session recovery points and stale next-step detection.
- `brigade work phases session checkpoints import-issues` for routing blocked or stale checkpoint issues into deduped `source: phase-session-checkpoint` work imports.
- `brigade work phases session next/resume` now include the latest checkpoint summary and checkpoint issue counts when a session has recovery metadata.
- `brigade work phases session recovery-note` plus `recovery-notes list/show` for local AFK recovery notes with safe summaries, notes, evidence labels, session references, and activity timeline events.
- `brigade work phases session recovery-notes closeout` for reviewed, deferred, blocked, or archived closeout metadata on AFK recovery notes.
- `brigade daily plan` now includes active phase session checkpoint issues as `phase-session-checkpoint` candidates with suggested checkpoint import commands.
- `brigade daily run` can write one safe local phase session checkpoint as its selected bounded action.
- `brigade work phases session risk` for a read-only AFK session risk summary across next-step blockers, checkpoint drift, recovery notes, and phase doctor issues.
- `brigade work phases session verification` for read-only verification rollups across the phase records in an AFK session.
- `brigade work phases session privacy` for read-only privacy-check rollups across the phase records in an AFK session.
- `brigade work phases session handoffs` for read-only handoff coverage rollups across the phase records in an AFK session.
- `brigade release doctor` now warns when active phase-session checkpoint evidence is blocked or stale.
- Release candidate evidence now includes the latest phase session checkpoint and checkpoint compare summary.
- `brigade center reviews` now includes blocked or stale phase-session checkpoint review items.
- `brigade work brief` now includes latest phase-session checkpoint and checkpoint compare evidence.
- `brigade work phases actions plan/build` now creates local actions for blocked or stale phase-session checkpoints.
- `brigade work phases session checkpoints archive` for moving an old checkpoint into local archive metadata.
- Phase session reports now include recovery evidence for latest checkpoints, checkpoint compare issues, and recovery notes.
- `brigade work phases schema` now publishes AFK session health contracts for session next, resume, checkpoints, recovery notes, risk, verification, privacy, handoffs, reports, progress, and gate outputs.
- `brigade work phases session protocol <session-id|latest>` for a wrapper-safe AFK resume protocol that summarizes next step, risk, progress, checkpoint state, completion gate, allowed command prefixes, forbidden actions, and whether `session resume` is safe to record.
- `brigade release candidate compare` now detects phase session checkpoint, checkpoint-compare, and completion-gate drift after a candidate bundle is built.
- `brigade work phases session audit <session-id|latest>` for a read-only AFK session self-audit across resume protocol, progress, risk, verification, privacy, handoff, and completion-gate evidence.
- Phase 226-250 AFK hardening closeout recorded the final session gate path for checkpoint, recovery, wrapper protocol, release compare, and self-audit work.
- `brigade work phases session start/list/show/closeout` for local AFK phase execution sessions that track a requested range, current phase, phase status, commit and test counts, report references, closeout state, and next command.
- `brigade work phases session next/resume` for read-only or metadata-only AFK session recovery that identifies the safest next phase command without executing it.
- `brigade work phases session report build/list/show` for local Markdown and JSON evidence bundles over phase execution sessions.
- `brigade work phases session activity <session-id|latest>` for a chronological read-only activity ledger across phase records, starts, completions, tests, commits, reports, actions, imports, closeouts, handoffs, and resumes.
- `brigade work phases session progress <session-id|latest>` for read-only percent complete, status counts, blockers, current phase, next command, test coverage, commit and push coverage, and remaining-step summaries.
- `brigade work phases session import-issues <session-id|latest>` for routing unresolved AFK session blockers into deduped `source: phase-session` work imports.
- `brigade work phases goal scaffold --range <range>` for local editable `/goal` drafts from ledger state, session evidence, blockers, and roadmap references.
- `brigade work phases session gate <session-id|latest>` for the final read-only AFK session claim check, with release doctor and candidate evidence carrying the latest gate result.
- `brigade daily status/plan/review/run/doctor` now surface active phase sessions, and daily run can build one phase session report or close out one completed reviewed session as its single safe step.
- Release doctor, release candidate evidence, center status/reviews, and work brief/doctor now include compact phase session and session report state.
- `brigade work phases evidence add <phase-id>` for appending local evidence attachments to phase records, with doctor warnings for missing referenced evidence files.
- `brigade work phases verify plan/record` for local phase verification matrices that show expected commands and record operator-supplied outcomes without executing tests.
- `brigade work phases reconcile <phase-id|range|latest>` for read-only local git reconciliation of phase commit hashes, push refs, branch containment, and dirty worktree state.
- `brigade work phases privacy <phase-id|range|latest>` for local phase evidence privacy scans with redacted findings and recorded clean or blocked summaries.
- `brigade work phases handoff <phase-id|range|latest>` for drafting and optionally linting a Memory Handoff from selected phase evidence without editing canonical memory.
- `brigade work phases closeout <phase-id|range|latest>` for local reviewed, deferred, blocked, or archived phase ledger closeouts, plus stale unreviewed completed-phase doctor warnings.
- `brigade work phases compare <phase-id|range|latest>` for read-only phase evidence freshness checks against local HEAD, referenced files, report age, test evidence, and doctor issue counts.
- `brigade work phases actions plan/build/list/show/start/done/defer/archive` for metadata-only phase-ledger action queues sourced from doctor issues and closeout blockers.
- `brigade daily plan/review/run` now considers phase-ledger actions and unresolved phase issues, and can start one phase action or build one phase report as a bounded local daily step.
- Release doctor, release candidate bundles, and release candidate compare now include phase-ledger closeout and report evidence, with warnings for unresolved closeouts, stale reports, and unreviewed pushed phases.
- `brigade work phases report closeout <report-id|latest>` for local reviewed, deferred, superseded, or archived phase report bundle closeout metadata.
- `brigade work phases report compare <report-id|latest>` for read-only checks of saved phase report freshness against current ledger status, doctor issues, HEAD labels, and report closeouts.
- Phase ledger health now includes phase action queue counts and top action details, with visibility in daily status, work brief, work doctor, and center status.
- `brigade work phases actions import-issues` for routing open phase action records into the work inbox as deduped `source: phase-ledger-action` task imports.
- Phase health and release candidate evidence now include the latest phase report compare summary, and release doctor warns when report compare has open issues.
- `brigade work phases init/plan/list/schema/status/next/show/start/complete/defer/doctor/import-issues` plus `brigade work phases report build/list/show` for a gitignored phase execution ledger that makes unattended multi-phase work auditable, detects silent compression, writes local reports, routes ledger issues into the work inbox, and surfaces phase-ledger health in daily, work, center, and release views.
- Daily hardening audits now perform phase-aware checks across daily receipts, center contracts, inbox evidence quality, repo fleet daily-use state, and release self-dogfood evidence, with release readiness and release candidates carrying compact summaries for those checks.
- `brigade daily hardening plan/audit/import-issues/closeout` plus `docs/phase-115-164-plan.md` for the production-hardening queue across daily reliability, operator-center contracts, inbox evidence quality, repo-fleet daily use, and self-dogfood release evidence.
- `brigade daily resume/repair/unblock/protocol`, `brigade daily telemetry doctor`, daily approval compare/archive commands, normalized daily adapter receipts, explainable plans, verification-aware closeout fields, local telemetry, and release evidence for the daily driver.
- `brigade daily approvals list/show/approve/reject/hold` plus `brigade daily run --approval <approval-id>` for local approval requests that preserve daily-driver context across approval-required boundaries.
- `brigade daily init/schema/history/show/doctor` plus local `.brigade/daily.toml` settings for daily-driver config, JSON contracts, receipt inspection, and stale or blocked run health.
- `brigade daily status/plan/review/run/closeout` for an agent-facing daily driver that ranks local operator evidence, selects one safe action, runs or stages one bounded item with receipts, and closes out the day without arbitrary execution or remote mutation.
- `brigade work import provenance` for a read-only cross-producer import provenance audit with text and JSON output.
- `brigade roadmap commands` for a parser-derived public command documentation contract with text, JSON, generated `docs/command-inventory.md`, and stale-inventory checks.
- `docs/phase-61-100-plan.md` as the public, testable phase queue for roadmap completion hardening.
- Public `templates/` index that points fresh-start users at the packaged starter templates without exposing local dogfood workspace files.
- Built-in `brigade doctor` bootstrap budget checks that fail hard when installed bootstrap files exceed conservative byte limits.
- Built-in `brigade doctor` memory-card budget checks that fail when `memory/cards/*.md` cards become too large.
- Built-in `brigade doctor` memory-index checks that fail when `MEMORY.md` links to missing `memory/cards/*.md` files.
- `brigade doctor` memory-care freshness checks for stale decay scans, plus hard failures for corrupt scan or refresh-queue JSON.
- `brigade run "<task>"`, a bounded aboyeur flow that asks one rostered orchestrator to plan assignments, dispatches worker CLIs in parallel, then asks the orchestrator to synthesize the final answer.
- `.brigade/roster.toml` loading for cross-model agent rosters using the user's installed CLIs (`codex`, `claude`, or `ollama:<model>`). Claude is optional, not required.
- `brigade roster init` and `brigade roster doctor` to scaffold a Codex/Ollama starter roster and validate roster syntax plus installed CLI availability.
- `brigade dogfood` for a built-in Codex-only, prompt-level read-only, inspected run with artifacts and optional handoff.
- `brigade dogfood init` to persist machine-local dogfood defaults in gitignored `.brigade/dogfood.toml`, enabling a one-command daily `brigade dogfood` path.
- `brigade dogfood status` to report local dogfood readiness, effective paths, CLI availability, ignore coverage, sandbox mode, and latest run.
- `brigade dogfood latest`, `brigade dogfood next`, and per-run dogfood `summary.md` artifacts for turning the latest run into the next work item without copying artifact paths.
- `brigade run --show-plan` and `--verbose` visibility modes, plus defensive runtime enforcement of roster `allow_models`.
- `brigade run --inspect` to print a readable artifact summary immediately after a run completes.
- `brigade run --cwd`, `--output-dir`, and default `.brigade/runs/<id>` artifacts for dogfooding auditable runs.
- Start, finish, and duration metadata in `run.json` artifacts.
- `roster.json` run artifacts that capture the effective orchestrator, agents, limits, allow-list, and timeouts for later review.
- `plan-attempts.json` run artifacts that capture raw planner outputs and parse errors for debugging failed planning runs.
- `synthesis.json` run artifacts that capture orchestrator synthesis status, detail, and raw text for non-dry runs.
- Successful `--handoff` runs now record the written handoff path in `run.json`.
- `brigade run --handoff` to write a Memory Handoff for successful runs, with `--handoff-inbox` override.
- `brigade runs list` to print recent run artifact directories from `.brigade/runs`.
- `brigade runs latest` to show the newest run summary without copying a run path from `brigade runs list`.
- `brigade runs show <run-dir>` to print a readable summary of one run artifact directory.
- `brigade work status` to report the current repo branch, dirty files, dogfood readiness, latest run, and extracted next step for daily work sessions.
- `brigade work start` and `brigade work end` to create local `.brigade/work/` session artifacts for normal daily work loops.
- `brigade work end --handoff` to write a Memory Handoff from closed work session artifacts.
- `brigade work list`, `brigade work latest`, and `brigade work show` to inspect local work session artifacts.
- `brigade work recap` to summarize recent or date-filtered work sessions.
- `brigade work run` to start a work session, run dogfood, close the session, write a work handoff, and print a recap in one command.
- `brigade work resume` to show the active or latest work session, latest dogfood run, extracted next step, and suggested command.
- `brigade work next` to resolve the next daily task without inspecting artifacts, plus `brigade work run` now uses the latest extracted next step when no task is passed.
- `brigade work next --json` to expose the resolved daily task, active session, dogfood snapshot, and suggested command to wrappers.
- `brigade work bootstrap` to initialize and verify the dogfood-backed daily work loop in one command.
- `brigade work brief` and `brigade work brief --json` as a start-of-day entrypoint with git state, latest sessions, latest dogfood run, resolved next task, and suggested command.
- `brigade work tasks` plus `brigade work task add/show/done` to manage a gitignored local task ledger under `.brigade/work/tasks.json`.
- Typed task metadata and repeatable acceptance criteria for `brigade work task add`, plus `brigade work task plan` for the completion checklist.
- `brigade work task add --template` for `vertical-slice`, `bugfix`, `red-green-refactor`, `docs`, and `security-follow-up` defaults.
- `brigade work task add --from-issue <issue-url-or-number>` to import GitHub issue title and metadata through the existing `gh` CLI when available.
- `brigade work task add --from-issue` now imports acceptance criteria from GitHub issue-body checkboxes and acceptance/test sections into the local task acceptance field without storing the raw body.
- Repo installs now include public-safe workflow rule templates under `rules/issue-tdd-loop.md` and `rules/acceptance-driven-work.md`, and `brigade work doctor` reports missing rule templates.
- `brigade work import issue-repairs` to route incomplete, stale, unchecked, or closed-remote issue-backed local tasks into repairable local imports without GitHub mutation.
- `brigade work acceptance` now includes completion-time acceptance gaps, code-review finding outcomes, latest work closeout status, and fuller release readiness plus release candidate evidence.
- Handoff ingestor log parsing now recognizes skipped, failed, malformed, unreachable-source, and no-reply warning states in issues and normalized reconcile receipts.
- Handoff source coverage issues now carry source keys and fingerprints, so uncovered writer inbox repairs dedupe and dismissed items stay quiet until coverage changes.
- `brigade release schema` for a wrapper-friendly local manifest of release readiness, candidate, fleet train, waiver, and manual evidence JSON record contracts.
- `brigade release candidate audit` and `import-issues` for local release candidate provenance checks and work-inbox routing without publishing or remote mutation.
- `brigade center schema` for a read-only wrapper-facing manifest of operator center status, activity, reviews, templates, report, report review, and action queue JSON contracts.
- `brigade center readiness plan/closeout/list/show/import-issues` for final local operator readiness closeouts over roadmap, docs command inventory, center, release, repo fleet, security, memory, tools, context, learning, waivers, and a manual-only publish checklist.
- `brigade center report diff <base> <compare> --record` for local operator report diff receipts that track new review items, resolved items, new blockers, and stale receipt references.
- `brigade center actions doctor` and `import-issues` for local operator action aging policy warnings and explicit work-inbox routing.
- `brigade repos discover plan` for dry-run repository discovery under explicit configured roots with include/exclude rules, safe labels, path redaction, and no cloning.
- `brigade work run` now records consumed task snapshots in work-session artifacts and stores completed session, dogfood run, and acceptance metadata on completed ledger tasks.
- `brigade work run --queue-next` to queue the successful run's extracted next step, with duplicate pending task protection.
- `brigade work import add/list/show/promote` to manage a gitignored local import inbox for scanner-discovered candidate work.
- `brigade work inbox` to group pending scanner imports by source, kind, priority, age, and acceptance coverage with suggested next commands.
- `brigade work import validate` and `brigade work import ingest` for scanner-authored JSONL import files.
- Scanner-authored task imports can now carry `type`, `priority`, `template`, and `acceptance`, and promotion preserves those fields on local ledger tasks.
- `brigade work import plan <import-id>` to preview the exact task a reviewed import would create.
- `brigade work import promote --run <import-id>` to promote one task import and immediately run it through the work-session loop.
- `brigade work import memory-care` to convert `memory/cards/decay/refresh-queue.json` into local work imports.
- `brigade memory care init/scan/status/doctor/import-issues` for read-only local memory card decay scanning, refresh queue production, daily-loop health, and reviewed work inbox routing.
- Memory-care scans now flag missing reviewed dates and missing freshness dates, and status output summarizes reviewed, freshness, confidence, and evidence metadata coverage without editing cards.
- `brigade memory care plan-fixes` for planning-only reviewed/freshness metadata repair candidates with blockers, import metadata, and daily brief visibility.
- `brigade work import chat-sweep` to convert `.brigade/chat-memory-sweeps/latest.json` issues into local work imports.
- `brigade work import memory-refresh` to convert memory-refresh candidates into TDD-ready scanner task imports with card identity, refresh reason, evidence summary, and acceptance criteria.
- Chat sweep imports now convert actionable sweep issues into task imports, preserve local provider/channel/thread/confidence metadata, and omit raw private chat fields.
- `brigade chat surfaces init/list/show/doctor` plus `brigade chat sweep validate/ingest/import-issues` for local chat export fixtures that normalize safe findings into scanner inbox imports without live chat APIs.
- Chat surface providers now support aliases for common export names, including Discord, Slack, Telegram, ClickClack, generic JSON, and JSONL, normalized to canonical provider families.
- Scanner producer imports now use source item keys and fingerprints for idempotency, including dismissed-import protection until a source item materially changes.
- Inbox doctor now reuses the cross-producer provenance audit to flag producer imports missing source identity, source fingerprints, safe summaries, evidence references, or scanner run metadata.
- Context packs now summarize docs and guidance files by presence and safe metadata instead of copying file contents, learning candidates avoid raw import text fallback, and release note inputs redact secret-looking values.
- Memory-care scan issues include stable source fingerprints for stale, expired, undersourced, contradictory, missing-index-link, orphaned-card, oversized-card, and missing-frontmatter findings, while keeping memory card edits explicit.
- `brigade work scanners init/list/show/plan/doctor` for a gitignored local scanner registry and schedule planner that never executes scanners automatically.
- `brigade work scanners doctor --import-issues` to route scanner registry health warnings into the existing local work inbox.
- `brigade work scanners run <scanner-id>`, `run --all`, `run --due`, `runs`, and `run-show <run-id>` for explicit local scanner producer execution with gitignored receipts, stdout/stderr logs, output snapshots, due-run planning, pending import count reporting, and scanner-health imports for failed, stale, due, or malformed runs.
- Scanner runs can now attach provenance to matching new imports and can explicitly ingest configured JSONL output with `brigade work scanners run ... --ingest-output`.
- `brigade work sweep`, `brigade work sweeps`, and `brigade work sweep-show <sweep-id>` for explicit daily scanner sweeps that run due producers, ingest configured JSONL outputs by default, write gitignored sweep reports, and keep promotion manual.
- `brigade work sweep-review <sweep-id>` and `sweep-review latest` for read-only triage of sweep-created imports, skipped and dismissed fingerprints, provenance health, grouping, and suggested next commands.
- `brigade work inbox doctor` and `brigade work inbox archive` for scanner inbox hygiene checks and archiving old promoted, dismissed, or superseded imports.
- `brigade work import plan-handoff` and `promote-handoff` for lint-gated Memory Handoff drafts from durable non-task scanner imports, with provenance preservation and raw chat privacy checks.
- `brigade handoff list/show/archive` for local Memory Handoff draft queue visibility, stale or invalid draft health, and reviewed archive records without running the ingestor.
- `brigade handoff runs`, `run-show`, and `reconcile` for local handoff ingestion receipt visibility, draft outcome reconciliation, and archive outcome metadata without running the ingestor.
- `brigade work review init/plan/run/runs/show/import-findings/findings/finding-show/closeout` for explicit local multi-harness code review producers, receipts, normalized findings, imported finding resolution, local closeout records, and `code-review` work inbox imports without automatic fixes or remote mutation.
- `brigade work verify plan/run/runs/show` and `brigade work closeout <session-id-or-latest>` for local verification receipts and work closeout records that collect task acceptance, test command results, scanner sweep status, code review closeout state, handoff draft status, and session evidence without CI or remote mutation.
- `brigade release plan/doctor/run/runs/show` for local release-readiness receipts that collect work closeout, verification, review closeout, scanner sweep, security, handoff, content-guard, docs, changelog, roadmap, and git-state evidence without pushing, tagging, or mutating remotes.
- `brigade release ci doctor/import-issues` for local GitHub Actions platform deprecation warnings from workflow files or saved CI summaries, including redacted safe excerpts, release-readiness evidence, and work-inbox routing.
- `brigade release smoke plan/record/list/show/doctor` for local install smoke matrix receipts across supported depth and harness combinations, including stale and missing smoke warnings in release readiness and center activity.
- `brigade release candidate plan/build/list/show/archive` for local release candidate bundles with readiness evidence, release notes drafts, manual-only publish plans, changed file lists, blockers, warnings, and content-guard summaries without pushing, tagging, or creating releases.
- `brigade release candidate compare` and `closeout` for local candidate freshness checks and reviewed, superseded, archived, or draft closeout metadata.
- `brigade context plan/build/list/show/archive` for local context engineering packs with safe summaries, task acceptance, recent evidence, and explicit private-evidence exclusions.
- `brigade context sync plan/record` for read-only context sync planning receipts against configured harness destinations, with conflict and freshness checks but no context file writes.
- `brigade context doctor/import-issues` for stale context packs, missing source references, stale task acceptance, stale tool references, and `source: context-pack` work imports.
- `brigade projects audit/import-issues` plus `brigade projects readiness plan/record/list/show` for gitignored local project consolidation decisions, manual-only migration planning, and local readiness receipts covering docs, license, security, release, ownership, and migration blockers.
- `brigade projects closeout/closeouts/closeout-show` for reviewed, deferred, superseded, or archived project migration closeouts that quiet unchanged readiness issues and resurface changed fingerprints.
- `brigade learn plan/doctor/import-issues` plus `brigade learn closeout/closeouts/closeout-show` for bounded local learning candidates that become reviewed tasks, handoffs, suppressions, accepted risk, archive, deferral, or dismissal, with unchanged closeouts quieted and changed fingerprints resurfaced.
- `brigade learn replay export/list/show/compare` for safe local before/after learning replay receipts, redacted summaries, compare receipts, release evidence, and operator-center review surfacing.
- `brigade security sarif` and security scan SARIF bundle output for dependency-free SARIF 2.1.0 evidence generated from redacted local findings.
- `brigade security template-audit` for focused public template and docs privacy checks, with placeholder allowlists, doctor integration, and release-readiness evidence.
- Security guardrail coverage now labels repo guidance, skills, slash commands, subagents, and tool wrappers separately, including prompt-injection and environment-exfiltration patterns with template confidence handling.
- Security policy presets now include `ci`, and security closeouts record policy-pack blocker, warning, and accepted-risk evidence for release readiness and candidate packets.
- `brigade tools pack build/list/show/archive` and `brigade tools sync plan/apply` for portable tool evidence bundles and reviewed projection sync over the existing managed projection path.
- `brigade tools parity status/closeout` for local reviewed projection parity receipts that quiet unchanged missing, stale, unmanaged, conflicted, or parity-gap projection issues while resurfacing changed fingerprints.
- Release readiness and release candidate evidence now include tool pack freshness, projection parity closeout state, sync-plan blockers, approval queue counts, run history, and checkpoint state without applying projections.
- `brigade work backup closeout`, `brigade security closeout`, `brigade handoff closeout`, `brigade memory care closeout`, and `brigade work acceptance` for reviewable local closeout and acceptance rollup receipts.
- Backup health status now separates raw, active, quieted, changed-fingerprint, and restore rehearsal issue counts, and release evidence includes the safe backup operator summary without copying private destination values.
- `brigade center status/activity/reviews/templates`, `brigade center report plan/build/list/show/archive/review/compare/closeout`, and `brigade center actions plan/build/list/show/start/done/defer/archive` for local operator-center summaries, local report bundles, reviewed daily action queues, freshness comparison, and report closeout over work, scanner, review, handoff, tool, learning, context, project, security, and release state.
- `brigade roadmap audit` and `brigade roadmap patterns` for roadmap closure checks, stale phase warnings, documented command drift, neutral pattern-family coverage, and source-pattern decisions.
- `brigade repos init/list/show/scan/doctor/import-issues` for gitignored local repo-fleet readiness checks, safe setup metadata, fallback guidance detection, and `repo-fleet` work inbox imports.
- `brigade repos health-commands` for read-only inspection of optional fleet health command labels, timeouts, latest sweep receipt status, stale receipts, and failed command receipts.
- `brigade repos sweep plan/run/runs/show/closeout` for explicit repo-fleet evidence refresh sweeps that run configured local read/report commands, write gitignored sweep receipts, track per-repo command status, and surface stale, failed, or unclosed sweeps through repo, center, work, and release health.
- `brigade repos report plan/build/list/show/archive/closeout` and `brigade repos actions plan/build/list/show/start/done/defer/archive` for local repo-fleet operator rollups and reviewed fleet action queues using safe labels, counts, statuses, fingerprints, and receipt labels only.
- `brigade repos actions dispatch plan/apply/report`, `dispatch --all-reviewed`, `reconcile`, and `context plan/build` for routing reviewed fleet actions into target repo work imports, explaining dismissed, superseded, changed, or broken dispatch state, building action-scoped context packs, and reconciling target repo completion evidence back into the local fleet queue.
- `brigade repos release plan/build/list/show/compare/closeout/archive` for local fleet release train bundles that collect per-repo release readiness, release candidates, fleet action reconciliation, verification, review, security, and operator evidence into manual-only publish plans without remote mutation.
- `brigade repos release actions` and `brigade repos release evidence` for reviewed release train action queues and manual publish evidence records that stay local, explicit, and non-executing.
- `brigade repos release reconcile` and `summary` for resolving fleet release actions against manual evidence and including reconciliation summaries in release train closeouts.
- `brigade repos release report/matrix/checklist/hygiene/import-issues/ready` for local release train review reports, matrix tables across readiness, evidence, actions, and waivers, evidence checklists, hygiene checks, unresolved evidence imports, and manual publish readiness gates.
- `brigade repos release waivers`, `activity`, `manifest`, and `audit` for explicit release-train waivers, chronological train activity, bundle manifests, bundle audits, and waiver-aware manual publish readiness.
- Release-train waivers now support expiry, owner labels, policy templates, renewal, health checks, work-inbox import routing, and ready/audit visibility for expired, stale, missing-expiry, missing-owner, weak-reason, invalid-scope, repo-drift, or train-changed waivers.
- `brigade work sweep closeout <sweep-id|latest>` for reviewable sweep closeout records that block unresolved pending imports, support explicit deferrals, and surface unclosed sweeps through inbox hygiene.
- `brigade work backup init/status/doctor/import-issues` for read-only local backup health summaries and `backup-health` inbox imports.
- Backup health checks for stale snapshots, failed or stale checks, failed or stale prunes, missing summaries, overdue restore rehearsals, and unsafe private summary fields.
- `brigade tools init/list/show/search/describe/contracts/call plan/call queue/call list/call show/call approve/call reject/call hold/call run/runtime/policy/plan/apply/doctor/import-issues`, plus `brigade tools run list/show/latest/replay` and `brigade tools checkpoint list/show/approve/reject/resume`, for portable tool, slash command, skill, superpower, script, and MCP catalog discovery plus explicit projection writes, read-only call planning, local call approval review, explicit approved script and local MCP execution, run history inspection, replay review, checkpointed resume, runtime supervision, and host-local execution policy.
- Tool catalog health checks for missing sources, missing manifests or schemas, invalid schema JSON, invalid contract schemas, missing examples, bad argument templates, missing contracts, parity gaps, missing projections, unmanaged projections, locally edited managed projections, stale projections, MCP config issues, stale health files, unsafe auth/env fields, and high-risk command shapes.
- Schema-backed call plans validate local JSON args against a dependency-free JSON Schema subset, render configured argument templates, report blockers, and redact secret-looking fields without invoking tools.
- Portable tool call approvals are stored in gitignored `.brigade/tools/calls.jsonl`, dedupe equivalent pending or approved calls, reject blocked approvals, and surface stale pending or stale approved calls in doctor, brief, and `tool-catalog` imports.
- Approved portable script calls can now be run explicitly with `brigade tools call run <call-id>` or `--next`, with local receipts and stdout/stderr logs written under gitignored `.brigade/tools/runs/`.
- `brigade tools run list/show/latest/replay` inspects local execution receipts and queues reviewed replay candidates without direct reruns or bypassing approval, runtime, or policy gates.
- `brigade tools checkpoint list/show/approve/reject/resume` records script-requested local checkpoints, reviews allowed resume choices, and resumes only after revalidating approval, runtime, policy, contract, source, and projection gates.
- Approved local MCP calls can now run through `brigade tools call run` via a configured local stdio command, already-running managed runtime, JSON-RPC `initialize` / `tools/list` / `tools/call`, and receipts with redacted MCP request and response summaries.
- `brigade tools runtime init/list/show/status/start/stop/restart/doctor` for explicit local runtime supervision with PID files, logs, stale PID detection, port conflict checks, health checks, and tool-call runtime gating.
- `brigade tools policy init/show/doctor` for host-local execution policy, including allowed families/effects, denied effects, required approval modes, timeout caps, runtime allow-lists, and env label bindings without storing secrets.
- Managed tool projections record source and projection fingerprints so `brigade tools plan`, `apply`, and `doctor` can distinguish missing, current, stale, unmanaged, and conflicted projection states.
- `tool-catalog` inbox imports with stable source fingerprints and dismissed-import protection until a catalog issue materially changes.
- `brigade work import triage` to group pending imports by source and kind.
- `brigade work import dismiss` to close noisy imports without promoting them.
- `brigade work import promote --all` with optional `--source` and `--kind` filters for batch promotion.
- `brigade work import list/triage/promote/dismiss` metadata filters for scanner-specific fields such as `handoff_issue_category`.
- `brigade work import dismiss --all` for filtered bulk dismissal of pending imports.
- `brigade handoff doctor` to compare pending `.claude` and `.codex` memory handoffs against gitignored local source config.
- Repo installs now include `.brigade/handoff-sources.example.json` as the local handoff ingestor source-list contract.
- `brigade handoff doctor` ingestor-log checks for stale latest-run logs, skipped malformed handoffs, warning summaries, and no-reply/no-update masking signals.
- `brigade handoff issues` and `brigade handoff import-issues` to turn handoff ingest warnings into grouped repair guidance and local work imports.
- `brigade handoff issues --category` and `brigade handoff import-issues --category` for category-limited handoff issue review/import.
- `brigade handoff lint` to validate pending or explicit handoff files before ingest and catch card/document action mismatches that would be skipped later.
- `brigade handoff sync-issues` to import new handoff-ingest issues without resurrecting dismissed ones and close stale local handoff tasks/imports.
- `docs/import-schema.md` documenting the local import JSONL contract for scanners and wrappers.
- Cybersecurity plugin roadmap covering broad agent-workspace security checks plus Brigade-specific scanner, doctor, import, and multi-harness security checks.
- Built-in `security` station and `brigade security scan` for read-only agent workspace security checks.
- Deeper MCP security checks for unpinned `npx`, shell metacharacters, secret-looking env values, sensitive or broad file args, high-risk local commands, large server sets, and missing timeouts.
- Supply-chain security checks for package scripts, GitHub Actions permissions and action refs, Python URL dependencies, and legacy install hooks.
- `brigade security enrich` for explicit post-scan enrichment artifacts, with an offline local provider and opt-in MISP provider config.
- `brigade security scan --import-findings` to route security findings into the local work import inbox for review, with source `security-scan`, stable source fingerprints, safe metadata, evidence paths, and dismissed-import protection.
- `brigade security init` to write gitignored local defaults to `.brigade/security.toml`, including scan profiles, enabled checks, include/exclude paths, severity thresholds, suppressions, and output paths.
- `brigade security config`, `brigade security doctor`, `brigade security findings`, and `brigade security show <finding-id>` for local config inspection, health checks, grouped finding review, and single-finding inspection.
- `brigade security fix` to create the local security artifact directory and refresh the managed `.gitignore` block.
- `brigade security review`, `brigade security suppress`, and `brigade security unsuppress` for a local finding review lifecycle with required suppression reasons. Suppress and unsuppress accept finding ids, id prefixes, or fingerprints.
- Security policy presets (`personal`, `public-repo`, `strict`), scan profiles (`public-repo`, `internal-workspace`, `local-only-audit`), template scanning controls, stable finding ids and fingerprints, and fingerprint suppressions.
- `brigade security scan --output-dir <dir>` to write redacted `security-report.json` and `security-report.md` evidence bundles.
- `brigade work brief`, `brigade doctor`, and `brigade work doctor` now report security config health, latest security evidence bundle status, open finding health, and local security artifact ignore coverage.
- `brigade doctor` and `brigade work doctor` now warn on stale security suppressions and suppressions missing reasons.
- Security scan secret evidence is redacted before reports, docs, session artifacts, or work imports are written.
- `ROADMAP.md` covering the daily-driver path, scanner-ready inbox, chat-surface scanners, memory-card decay refresh, and portable operator setup.
- `brigade work note` to append timestamped checkpoints to the active work session without ending it.
- `brigade work doctor` to check dogfood config, Codex availability, local artifact paths, handoff inbox, ignore coverage, and latest run context for the daily work loop.
- Workspace installs now include `.brigade/memory-care.example.json` as a scanner wiring contract for memory-care decay output.
- Workspace installs now include `.brigade/chat-memory-sweep.example.json` plus an OpenClaw memory-sweep cron fragment for nightly chat/session sweep wiring.
- Roster-level and per-agent `timeout_seconds` controls for bounded CLI calls.
- `brigade run --read-only` prompt policy for planning and review runs that should inspect and recommend only, with native `codex exec --sandbox read-only` enforcement for Codex agents.

### Changed
- `brigade roadmap audit` now reads documented commands from command snippets instead of prose and normalizes parameterized examples such as `brigade tools show <id>` to their CLI command path.
- `brigade roadmap audit --json` now includes deferred roadmap ownership records with subsystem, owner, reason, source section, status, and suggested phase.
- Roadmap phase headings now distinguish foundations, active work, and the phase queue so stale Current/Next warnings are actionable.
- Public repo contents now keep live dogfood workspace files, internal planning notes, and root memory cards untracked; public templates remain under `src/brigade/templates/`.
- Dogfood handoff defaults now use `.codex/memory-handoffs/` for new Codex-driven local configs while preserving explicit configured inbox paths such as `.claude/memory-handoffs/`.
- Bootstrap truncation is now treated as a hard doctor failure to prevent by moving durable detail into memory cards before agents load context.
- Dogfood runs now default to a 600 second per-agent timeout for practical daily repo reviews.
- Dogfood next-step extraction now handles markdown `## Next` sections and can fall back to `summary.md` when `final.txt` does not contain a next-step label.
- `brigade work run` now consumes the oldest pending ledger task before falling back to the latest extracted dogfood next step, and marks consumed tasks done after successful runs.
- `brigade work task add --from-next` now reuses an equivalent pending task instead of adding duplicates.
- `brigade work brief` now reports acceptance coverage for the next ledger task, and `brigade work run` passes accepted ledger criteria into the dogfood task prompt.
- `brigade work brief` now includes pending local work imports and import counts in both text and JSON output.
- `brigade work brief` now surfaces issue-backed next-task context.
- `brigade work doctor` now warns on pending tasks without acceptance criteria, unchecked or closed issue-backed tasks, and active work sessions left open too long.
- `brigade work doctor` now warns on stale scanner imports, task imports missing acceptance criteria, and noisy scanner sources with many dismissed imports.
- `brigade work brief` now surfaces pending handoff ingest issue counts when the local handoff source config has an ingestor latest-run log.
- The managed gitignore block now treats `.brigade/dogfood.toml`, `.brigade/security.toml`, `.brigade/runs/`, and `.brigade/security/` as local state.
- The managed gitignore block now treats `.brigade/handoff-sources.json` as host-local state.
- Live smoke docs now keep Codex agent execution in a trusted repo cwd while writing temporary roster, artifacts, and handoff output under `/tmp`.
- Handoff write failures now preserve final run artifacts, print the final answer, return nonzero, and mark `run.json` as `handoff-failed`.
- Dogfood runs default to prompt-level read-only plus Codex's `danger-full-access` sandbox setting for trusted-workspace use so repo inspection works on hosts where native read-only sandboxing blocks shell inspection; `--native-read-only-sandbox` opts into stricter native enforcement.

### Fixed
- Reduced storage footprint of the Codex adapter by skipping useless protocol chatter, storing tool-call arguments only once, capping the argument size to 4 KB (with a truncated marker and its full digest), and reporting skip/truncate metrics.
- `brigade init` now collapses mixed current and legacy managed `.gitignore` blocks into one regenerated Brigade block.

## [0.6.0] - 2026-05-24

### Added
- Managed tools: external CLIs that Brigade can install and wire per station via `brigade add <station>`. Brigade shells out to each tool, never importing it in process.
- `memory-doctor` and `bootstrap-doctor` attached to the `memory` station.
- `content-guard` attached to the `guard` station.
- New `tokens` station with Token Glace for output compaction.
- `brigade doctor` folds installed managed tools into its report and surfaces each tool's own health. Tools that are not installed are reported as non-failing `[todo]` hints, so doctor stays green on a bare host.
- `memory-doctor` and `bootstrap-doctor` inspect the operator's canonical memory and bootstrap files (host-global), so their findings are labeled operator-scoped and treated as advisory `[warn]`, never failing a workspace `brigade doctor` run.

## [0.5.0] - 2026-05-24

### Changed
- Renamed the project to **Brigade**. The PyPI distribution is now `brigade-cli` and the command is `brigade`. The workspace config directory is now `.brigade`, with a `.solo-mise` read fallback so older installs keep working.

### Added
- Built-in station registry that drives the doctor checks.
- `brigade status` command, alongside `brigade init` and `brigade doctor`, reporting over the station registry.

### Deprecated
- The `solo-mise` command is kept as a deprecated alias for `brigade`.

## [0.4.0] - 2026-05-17

### Breaking
- Removed the `--profile <name>` flag from `solo-mise init`. The flag has been deprecated since v0.3.0 with a stderr migration warning. Use `--depth <minimal|standard|deep>` plus `--harnesses <list>` instead. Migration table in the v0.3.0 notes below.

### Internal cleanup
Removed `src/solo_mise/init.py`, the `templates/profiles/` directory and its six legacy profile manifests, plus `templates.load_profile` and `selection.profile_to_selection`. No user-facing impact beyond the flag removal above.

### Migration

Same as v0.3.0. If you somehow have v0.2.0-era scripts still using `--profile`, see the table in the v0.3.0 section below.

## [0.3.0] - 2026-05-16

### Added
- Two-axis selection model: `--depth {repo,workspace}` + `--harnesses {claude,codex,openclaw,hermes}` + `--include publisher`. Pick any combination of harnesses.
- Interactive prompt on bare `solo-mise init` (no flags). Defaults to claude + repo + no includes.
- `.solo-mise/config.json` is now the per-target source of truth for selection state. Read by `doctor`, `ingest`, and `reconfigure`.
- `solo-mise reconfigure --target . [--prune]` adjusts an existing install to a new selection. `--prune` removes orphaned files for deselected harnesses.
- Per-writer handoff inboxes: `.codex/memory-handoffs/` for Codex (in addition to existing `.claude/memory-handoffs/`).
- Ingester now scans all configured writer inboxes.
- Doctor reports apparent harness shape, checks per-writer inbox, warns on orphaned inbox dirs from unselected harnesses.

### Changed
- README reframed around the two-axis model. New "Picking your harnesses" section walks through four common combos.
- CONTRIBUTING.md "Adding a profile" replaced by "Adding a harness" + "Adding a depth" + "Adding an include".

### Deprecated
- `solo-mise init --profile <x>` still works but prints a stderr deprecation note pointing at the new flags. Will be removed in v0.4.0.

### Migration

If you have v0.2.0 scripts using `--profile`:

| v0.2.0 | v0.3.0+ |
|---|---|
| `--profile repo` | `--depth repo --harnesses claude` |
| `--profile workspace` | `--depth workspace --harnesses claude` |
| `--profile openclaw` | `--depth workspace --harnesses claude,openclaw` |
| `--profile hermes` | `--depth workspace --harnesses claude,hermes` |
| `--profile generic` | `--depth workspace --harnesses none` |
| `--profile publisher` | `--depth repo --harnesses claude --include publisher` |

## [0.2.0] - 2026-05-16

### Added
- Memory-care staleness scaffolding: `memory/cards/decay/` layout and a doctor
  warning when the decay folder is missing, so durable cards do not quietly rot.
- Multi-workspace handoff patterns for users administering more than one agent
  home; secondary workspaces write into their own `.claude/memory-handoffs/`,
  the owner pulls those into a staging inbox.
- TokenJuice output-compaction guidance card covering Claude Code's PreToolUse
  wrapper path, Codex hook setup, and realistic savings expectations.
- Obsidian `/note` skill template under `skills/note/` for the `workspace`
  profile.
- `scripts/backup-restic.sh` template, exposed via the `workspace` profile.
- Managed `.gitignore` block: `solo-mise init` now creates or updates a
  `# >>> solo-mise gitignore block >>>` section in the target's `.gitignore`.
  Re-runs replace only the content between the markers, so user-authored rules
  are preserved. Skip with `--no-gitignore`.
- Release pipeline: `.github/workflows/publish.yml` builds an sdist + wheel on
  every `v*` tag and pushes to PyPI.
- CI matrix: `install-from-source` smoke now runs against all six profiles
  (`repo`, `workspace`, `openclaw`, `hermes`, `generic`, `publisher`).
- Project meta: `CONTRIBUTING.md`, `SECURITY.md`, `CODE_OF_CONDUCT.md`, and
  `.github/ISSUE_TEMPLATE/` (bug, profile-init-fails, ingester-misclassified).

### Changed
- Deepened the `workspace` profile's bootstrap files (`AGENTS.md`, `CLAUDE.md`,
  `IDENTITY.md`, `SOUL.md`, `HEARTBEAT.md`, `MEMORY.md`, `SAFETY_RULES.md`,
  `TOOLS.md`, `USER.md`, `INSTALL_FOR_AGENTS.md`).
- README: centered banner, refreshed badges, added a sample `doctor` run,
  noted that solo-mise makes no network calls, called out `init` idempotency.
- CI now pins the `content-guard` checkout to `v0.1.1` instead of tracking the
  default branch.
- `solo-mise init --profile hermes` prints a louder experimental-status notice
  on stderr in addition to the post-install note.

### Removed
- Stale `DREAMS.md` from the repo root and lingering references in templates.

## [0.1.0] - 2026-05-13

Initial release.

### Added
- `solo-mise` CLI with `init`, `doctor`, `scrub`, and `handoff-template`
  subcommands.
- Six profiles: `repo` (default), `workspace`, `openclaw`, `hermes`,
  `generic`, `publisher`.
- Conservative handoff ingester at `.claude/memory-handoffs/`: safe card
  handoffs become cards, targeted updates append, ambiguous material is
  kicked out for review.
- Content-guard pre-push hook for public-leak prevention.
- Sanitized bootstrap file set, starter memory cards, routing rules.
- OpenClaw adapter fragments and harness-aware doctor checks.
- Experimental Hermes adapter fragments.

[Unreleased]: https://github.com/escoffier-labs/brigade/compare/v0.30.0...HEAD
[0.30.0]: https://github.com/escoffier-labs/brigade/compare/v0.29.0...v0.30.0
[0.29.0]: https://github.com/escoffier-labs/brigade/compare/v0.28.0...v0.29.0
[0.28.0]: https://github.com/escoffier-labs/brigade/compare/v0.27.0...v0.28.0
[0.27.0]: https://github.com/escoffier-labs/brigade/compare/v0.26.1...v0.27.0
[0.26.1]: https://github.com/escoffier-labs/brigade/compare/v0.26.0...v0.26.1
[0.26.0]: https://github.com/escoffier-labs/brigade/compare/v0.25.0...v0.26.0
[0.25.1]: https://github.com/escoffier-labs/brigade/compare/v0.25.0...v0.25.1
[0.25.0]: https://github.com/escoffier-labs/brigade/compare/v0.24.0...v0.25.0
[0.24.0]: https://github.com/escoffier-labs/brigade/compare/v0.23.3...v0.24.0
[0.23.3]: https://github.com/escoffier-labs/brigade/compare/v0.23.2...v0.23.3
[0.23.2]: https://github.com/escoffier-labs/brigade/compare/v0.23.1...v0.23.2
[0.23.1]: https://github.com/escoffier-labs/brigade/compare/v0.23.0...v0.23.1
[0.23.0]: https://github.com/escoffier-labs/brigade/compare/v0.22.0...v0.23.0
[0.22.0]: https://github.com/escoffier-labs/brigade/compare/v0.21.1...v0.22.0
[0.21.1]: https://github.com/escoffier-labs/brigade/compare/v0.21.0...v0.21.1
[0.21.0]: https://github.com/escoffier-labs/brigade/compare/v0.19.0...v0.21.0
[0.19.0]: https://github.com/escoffier-labs/brigade/compare/v0.18.0...v0.19.0
[0.18.0]: https://github.com/escoffier-labs/brigade/compare/v0.17.0...v0.18.0
[0.17.0]: https://github.com/escoffier-labs/brigade/compare/v0.16.0...v0.17.0
[0.16.0]: https://github.com/escoffier-labs/brigade/compare/v0.15.0...v0.16.0
[0.15.0]: https://github.com/escoffier-labs/brigade/compare/v0.14.1...v0.15.0
[0.14.1]: https://github.com/escoffier-labs/brigade/compare/v0.14.0...v0.14.1
[0.14.0]: https://github.com/escoffier-labs/brigade/compare/v0.13.0...v0.14.0
[0.13.0]: https://github.com/escoffier-labs/brigade/compare/v0.12.0...v0.13.0
[0.12.0]: https://github.com/escoffier-labs/brigade/compare/v0.11.0...v0.12.0
[0.11.0]: https://github.com/escoffier-labs/brigade/compare/v0.10.3...v0.11.0
[0.10.3]: https://github.com/escoffier-labs/brigade/compare/v0.10.2...v0.10.3
[0.10.2]: https://github.com/escoffier-labs/brigade/compare/v0.10.1...v0.10.2
[0.10.1]: https://github.com/escoffier-labs/brigade/compare/v0.10.0...v0.10.1
[0.10.0]: https://github.com/escoffier-labs/brigade/compare/v0.9.3...v0.10.0
[0.9.3]: https://github.com/escoffier-labs/brigade/compare/v0.9.2...v0.9.3
[0.9.2]: https://github.com/escoffier-labs/brigade/compare/v0.9.1...v0.9.2
[0.9.1]: https://github.com/escoffier-labs/brigade/compare/v0.9.0...v0.9.1
[0.9.0]: https://github.com/escoffier-labs/brigade/compare/v0.8.2...v0.9.0
[0.8.2]: https://github.com/escoffier-labs/brigade/compare/v0.8.1...v0.8.2
[0.8.1]: https://github.com/escoffier-labs/brigade/compare/v0.8.0...v0.8.1
[0.8.0]: https://github.com/escoffier-labs/brigade/compare/v0.7.0...v0.8.0
[0.7.0]: https://github.com/escoffier-labs/brigade/compare/v0.6.0...v0.7.0
[0.6.0]: https://github.com/escoffier-labs/brigade/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/escoffier-labs/brigade/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/escoffier-labs/brigade/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/escoffier-labs/brigade/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/escoffier-labs/brigade/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/escoffier-labs/brigade/releases/tag/v0.1.0
