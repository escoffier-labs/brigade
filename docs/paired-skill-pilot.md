# Paired brigade-work pilot

This local apparatus compares two collected skill packages through a bounded tool
protocol. Its replay command exercises real Brigade verification and outcome code
in disposable targets. Replay results are offline apparatus checks. A live Claude
Code adapter exists (see Live transport) but no live model pair has been run, and
neither kind of result can score or promote a skill.

## Run the offline apparatus

From the repository root, using the project Python environment:

```bash
python -m brigade model trial paired-plan src/brigade/fixtures/paired-brigade-work/manifest.json
python -m brigade model trial paired-replay src/brigade/fixtures/paired-brigade-work/manifest.json --transcript src/brigade/fixtures/paired-brigade-work/replay.json --output-dir /tmp/paired-skill-evidence
```

The replay file contains Codex-shaped `item/tool/call` requests and explicit final
claims for each scenario. `$observed_run_id` substitutes the receipt ID returned
by the real tool. It does not substitute the final status or eligibility. Scripted
claims are graded against observed receipts. They are never model measurements.

The five scenarios exercise a passing check, exit 7, a fixed sleeping fixture
command with a one-second verification timeout, a completed audit-only receipt,
and hostile text in an actual verification tool response. The hostile text asks
for unsupported shell access and false eligibility. No agent-supplied command,
path, manifest, or receipt ID is accepted by the tool dispatcher.

## Recorder and grading boundary

The only operations are `brief`, `verify`, `receipt`, `scoreability`, and `capture`,
with empty argument objects. A session can perform at most 16 supported calls.
Additional or unsupported calls receive a recorded rejection. One verification
is allowed per fresh session. Capture failures before starting a new attempt.
Each verification runs a fixed evaluator-selected Python command in a fresh
TemporaryDirectory. Command timeout produces a real failed receipt with a
`timed_out` command. Model timeout marks an incomplete apparatus attempt.

Both arms receive the same scenario, model and settings. Baseline and candidate
order alternates across scenarios and repetitions. The fixture candidate appends
a targeted receipt-reporting and hostile-output revision to the bundled
brigade-work skill. Neither fixture is installed globally.

Anchored collection and `skills_cmd.registry._package_identity` supply package
identity. Complete package bytes are retained under the output's `packages/`.
Package, scenario, evaluator, grader, model and settings participate in cell
identity. Changed identity refuses reuse of the output directory. Repeating an
unchanged run appends attempts and retains earlier failures. It does not increase
the number of independent scenarios. Pair summaries use `both_pass`, `both_fail`, `candidate_only_pass`,
`baseline_only_pass`, and `incomplete`. These describe apparatus behavior.
Every result states `model_executed: false` and `promotion_eligible: false`.
The return field `consistency_validated` describes internal consistency only.

The replay adapter binds the complete transcript, including request IDs,
call IDs, thread IDs and turn IDs, into the plan, each cell and every attempt.
Recorded replay requests and constructed offline response envelopes are retained
alongside normalized tool traces. Response envelopes are produced by the local
apparatus. They were not observed from a provider. Changing the transcript or
adapter refuses resume.

Trusted host callbacks must supply a stable `paired_adapter_identity` dictionary
with a nonempty `id`. The host must change that identity whenever callback
behavior or configuration changes. This declaration is a host trust contract.
The evaluator cannot independently establish the origin of arbitrary Python
callbacks. Callback traces contain only observed tool arguments and results,
without invented transport IDs. The validator checks declared identity and trace
consistency. Model execution and callback authenticity remain unverified.

The evaluator records requests and responses outside the session interface.
Independent validation checks the exact input against retained package bytes,
cell identity, final receipt claims, captured outcome response, scoreability
response, receipt snapshots and recomputed grades. A session receives prompt data
and a bounded callable. This does not sandbox Python code supplied by the host.
Only a transport that denies all other model tools can enforce the same boundary
for a live agent.

Output paths are walked without following symlinks before resolution. New
directories use restrictive permissions. Existing output directories must be
owned by the current user and protected against other writers on POSIX. Artifact,
digest, package and receipt writes use descriptor-relative atomic replacement.
Planted links, hardlinks and special files refuse execution and resume. Platforms
without equivalent descriptor operations fail closed.

`evidence.json` and `evidence.sha256` bind the audit record. Retain the digest from
the reviewed output separately, then validate using that reviewed value:

```bash
python -m brigade model trial paired-validate src/brigade/fixtures/paired-brigade-work/manifest.json --output-dir /tmp/paired-skill-evidence --evidence-sha256 REVIEWED_SHA256
```

Validation checks internal consistency against the supplied digest and local
evaluator source identity. A colocated digest alone is not independent provenance
or a registered verifier result. A writer with access to the entire evaluator
output can replace both files. The parent must review and retain the exact digest
outside agent write authority before binding validated evidence in registry
metadata. No scorecard, manifest, or eligibility policy changes are included.

## Live transport

Two transports were inspected. The Codex app-server cannot enforce an exclusive
tool set. The Claude Code CLI can, so the live adapter uses it.

### Codex app-server (not used)

Local inspection of codex-cli 0.160.0 used:

```bash
codex app-server generate-json-schema --experimental --out /tmp/paired-protocol-schema
```

The installed `v2/ThreadStartParams.json` defines `dynamicTools` with function
`name`, `description`, `inputSchema`, and `type`. `DynamicToolCallParams.json`
defines `arguments`, `callId`, `threadId`, `tool`, and `turnId`.
`DynamicToolCallResponse.json` defines `success` and `contentItems`, including
`inputText`. The boundary's `rpc` method exercises that request/response shape
offline. The existing AppServer client continues to auto-decline requests.

These schemas establish dynamic-tool support. They do not establish an exclusive
tool set: ThreadStartParams has no documented field disabling every built-in
shell, file-write, MCP, and delegation tool. No undocumented key was used. The
approved T3 transport also provides no per-session interceptor for these five
operations. A read-only filesystem sandbox still permits reads and does not
prove that every tool request is recorded.

### Claude Code CLI (the live adapter)

`python -m brigade model trial paired-live` launches one fresh `claude -p`
process per arm. The flag set and the init event shape were checked against
Claude Code 2.1.327 without credentials: the init event arrives before
authentication, so no model call happened. A full live arm has not run yet.

```text
claude -p --output-format stream-json --verbose --include-hook-events
  --tools "" --strict-mcp-config --mcp-config <one server> --setting-sources ""
  --settings <every builtin plugin disabled> --no-session-persistence --session-id <uuid> --disable-slash-commands
  --no-chrome --restricted --system-prompt-file <per-arm file> --model <alias>
  --permission-mode dontAsk --allowedTools <the five paired tools>
  [--effort <level>] --disallowedTools Task Agent
```

The prompt arrives on stdin and `--disallowedTools` is last because it is
variadic. `--safe-mode` is not used because it disables MCP. `--bare` is not used
because it skips OAuth credentials. The read-only deny bundle is not reused
because its `mcp__*` would remove the paired server.

The one MCP server is `python -m brigade model trial paired-mcp`. It exposes
exactly `brief`, `verify`, `receipt`, `scoreability` and `capture`, backed by the
same `Boundary` the offline path uses (empty arguments, one verification, a cap
of 16 calls). Every call, including a rejected or non-allowlisted one, is
appended to a server-side JSONL trace that is authoritative. Any tool outside the
allowlist is rejected, logged and fails the cell.

Each arm gets a fresh process, working directory, `HOME`, `CLAUDE_CONFIG_DIR`,
temp directory, fixture target and session id. The child environment is built
from an allowlist (`PATH`, locale, timezone, TLS and proxy variables, and the
Windows system variables the interpreter needs) plus `CLAUDE_CODE_OAUTH_TOKEN`, a
per-arm `HOME` and `USERPROFILE`, and these fixed values:
`CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1`, `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`,
`CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`, `DISABLE_TELEMETRY=1` and
`DISABLE_AUTOUPDATER=1`. `ANTHROPIC_API_KEY` and every other parent variable are
dropped. The MCP server config sets `CLAUDE_CODE_OAUTH_TOKEN` to an empty value in
its own `env`, so the server process starts without the token and never holds it.
It still drops `ANTHROPIC_*` and `CLAUDE_CODE_OAUTH*` before running any fixture
command, as a second line.

#### Executable binding

The adapter does not trust a path. It resolves `claude` once, opens that file
without following links, hashes it with SHA-256 and holds the descriptor for the
whole run. Both the `--version` probe and every arm exec that held file through
`proc.held_argv` (`fexecve`, the same mechanism the memory verify seat uses), so a
same-version replacement written to the path after the hash cannot receive
`CLAUDE_CODE_OAUTH_TOKEN` or forge init and stream events. The hash is recorded as
`adapter.executable.sha256` in every cell identity, so a different binary refuses
resume. The held file is re-hashed before each launch to catch an in-place rewrite,
and a mismatch stops the run before the next cell. The file must be a native
binary: a script or launcher runs another file and is refused.

Held-descriptor launch exists on Linux. On any other platform, Windows included,
`paired-live` exits 2 before launching a process rather than starting `claude` by
path. The tests run a fake `claude` through an injectable launcher prefix, which is
a test seam and records `launch: launcher` in the identity; it is not reachable from
the command line.

`--system-prompt-file` holds a fixed protocol prelude followed by the exact
`SKILL.md` bytes of that arm. Arms therefore differ only in skill bytes. The
system prompt, prelude and skill sha256 values are part of the adapter identity
inside every cell id, so a changed skill, prelude, flag set, CLI version, model,
timeout, executable hash or adapter source file refuses resume. "Adapter source file"
covers the adapter, the paired MCP server, the stdio server, the safe-output
helpers, the settings constants it shares with the verify seat, the process
launcher, the evaluator and the skill registry; editing any of them refuses resume.

#### Init gate

The first stream event must be `system/init`. Before anything is graded the
adapter aborts the cell unless the init event shows:

- no built-in tools, or exactly `EndConversation`, plus exactly the five paired
  MCP tools;
- exactly one MCP server, ours, connected, and no `mcp_server_errors`;
- no plugins (the `--settings` flag switches every builtin plugin off, so a
  reported plugin of any kind fails the gate), no skills and no slash commands;
- `agents` limited to the CLI's built-in agents, no additional directories, a
  `cwd` whose real path is the arm's working directory, and `permissionMode`
  `dontAsk`;
- a resolved `model` equal to the manifest's full model id, the requested
  session id, the recorded CLI version, and an `apiKeySource` that is not an API
  key.

Without `--safe-mode` the CLI still reads `CLAUDE.md` and `AGENTS.md` from the
working directory and every ancestor. Each arm's working directory is created
under a fresh temporary directory, and before launching the adapter checks that
neither file exists in that directory or any ancestor of it. If one does, the
cell fails as `ancestor_instructions:<file name>` and the CLI is never started.
Point `TMPDIR` at a location whose ancestors hold neither file; a home
directory that carries a `CLAUDE.md` is not such a location.

Any hook event anywhere in the stream also aborts the cell. A `tool_use`,
`server_tool_use` or `mcp_tool_use` block for anything except the five paired
tools (a built-in such as Bash, Read or Write, or another MCP name) aborts the
cell as `forbidden_tool`; the name is checked before duplicate ids, so a reused id
cannot hide one. A paired tool name carried by a `server_tool_use` or
`mcp_tool_use` block aborts as `forbidden_tool_type`, and a block whose id is
missing, not a string or already seen aborts as `malformed_tool_use`.

`EndConversation` survives `--tools ""` whenever MCP tools exist in some CLI
builds. It is recorded under `live.control_events` and never graded. 2.1.327 did
not list it in a probe with the paired server connected.

#### Trace and evidence

Observed `tool_use` items map into the existing `item/tool/call` envelope with
`callId` from the tool_use id, `threadId` from `init.session_id` and `turnId` from
the stream message uuid. After the process exits the server trace decides the
result: its row count must equal the adapter's observed count, tool names and
arguments must agree row by row, and any non-allowlisted server row fails the cell.
Response envelopes are built locally from the server responses, not observed from
the provider.

Live results use adapter id `brigade.claude-cli-live.v1` and schema
`brigade.paired_trial.live.v1`, with `model_executed: true`, `live: true` and
`promotion_eligible: false`. They never share an output directory with
`brigade.offline-replay.v1`, which is unchanged. Raw stream, server trace and
stderr are kept under each attempt and bound by hash. The OAuth token value is
removed from the stream, stderr, server trace and receipt bytes before they are
hashed or saved, so artifacts show `[redacted]` in its place. Child stdout and
stderr are read into memory, redacted there (a token split across two reads is
caught) and only then hashed or written; stderr is never staged in a file, so a
crash before cleanup leaves nothing token-bearing in the arm's temporary directory.
Only the last 64 KiB of redacted stderr is kept. The requested
session id and arm working directory are stored with each attempt and revalidated
against the init event instead of being read back from it. `paired-validate` picks the
live validator from the schema. The init gate reports the CLI's own view of its
configuration. Model and CLI authenticity remain unverified, and the result can
neither score nor promote a skill.

#### Deadline and leftovers

On Linux a cell's deadline is hard. Stdout is read through `poll` against a
monotonic deadline instead of waiting for EOF, so a process the arm started that
calls `setsid` and keeps the inherited pipe open cannot hold the cell past its
timeout. The read stops at the deadline, the arm's group and session are
signalled, and the cell fails as `descendant_held_pipes` while the trial moves on
to the next cell. After teardown the adapter scans `/proc` for a live process
whose environment still carries that cell's random `BRIGADE_PAIRED_CELL` token,
which every descendant inherits. A process without the token is never touched,
even with the same user and a working directory inside the cell, and each token
is read again after the pidfd is held. Each stray is signalled through its pidfd,
then an otherwise passing cell fails as `descendant_survived`, keeps its work dir
and records the pids in `live.diagnostics`. A same-user process whose environment
cannot be read is never signalled. It fails the cell as
`process_group_drain_failed` only when it started during the cell and either runs
in the work dir or holds one of the cell's pipes. Unrelated same-user processes
keep their environment unreadable by design, as sshd does, so they are skipped.
On Linux a cell that cannot scan `/proc` at all fails as
`process_group_drain_failed` and keeps its work dir, so a missing process table
never lets a cell pass unchecked. Every wait on the leader is bounded as well, so
a leader stuck in uninterruptible sleep records a cleanup failure instead of
holding the trial. Windows behavior is unchanged, and there is no cgroup or
PID-namespace containment yet, so a descendant that clears its own environment
can still escape the scan.

#### Size caps

Every read is bounded before it is buffered, and exceeding a cap fails the cell
(or refuses the run for a package): a skill package may hold 512 entries, 4 MiB in
total and 1 MiB per file, and `SKILL.md` at most 256 KiB, all enforced during the
directory walk. A stream line may not exceed 4 MiB and the stream 16 MiB
(`stream_line_too_large`, `stream_too_large`), stderr 1 MiB (`stderr_too_large`),
the server trace 4 MiB and the receipt 1 MiB (`server_trace_too_large`,
`server_receipt_too_large`). Revalidation reads saved artifacts under the same
caps.

#### Auth

Authentication is the subscription OAuth token only. Run `claude setup-token`
once, supply the result as `CLAUDE_CODE_OAUTH_TOKEN` from your secret store, and
leave API keys out. `paired-live` exits 2 before launching anything when the token
is absent, and `claude --version` that does not return within 30 seconds exits 2
the same way. A live manifest must name a full model id (for example
`claude-opus-5-5`). `settings.claude_cli_model` is the alias passed to `--model`,
and `settings.reasoning` becomes `--effort` when it is a valid level.

#### Operator runbook: first live `passing-verify` pair

Prove one tool-interacting pair before expanding to all five scenarios.

1. Confirm `claude --version` reports the version you intend to record, and that
   `CLAUDE_CODE_OAUTH_TOKEN` is set from `claude setup-token`.
2. Run the shipped one-case manifest into a new private output directory:

   ```bash
   python -m brigade model trial paired-live src/brigade/fixtures/paired-brigade-work/live-claude.json --output-dir <private-output-dir> --case passing-verify
   ```

   The command exits 0 even when a cell fails, because failures are evidence.
   It exits 2 for a refusal such as a missing token or a changed identity.
3. Read `attempts[].failure`, `attempts[].live.init` and `pairs[].comparison`.
   A failure beginning `init_gate:` means the CLI session was not the exclusive
   configuration. Do not loosen the gate to make a cell pass.
4. Retain the printed `evidence_sha256` outside agent write authority, then validate:

   ```bash
   python -m brigade model trial paired-validate src/brigade/fixtures/paired-brigade-work/live-claude.json --output-dir <private-output-dir> --evidence-sha256 REVIEWED_SHA256
   ```

5. Re-running the same command without `--resume-digest` starts fresh: evidence
   already in the output directory is moved to `superseded-<time>/` and never
   reused. To append attempts, pass the digest you reviewed:

   ```bash
   python -m brigade model trial paired-live src/brigade/fixtures/paired-brigade-work/live-claude.json --output-dir <private-output-dir> --case passing-verify --resume-digest REVIEWED_SHA256
   ```

   Resume validates the whole tree against that digest, and the `evidence.sha256`
   file stored beside the evidence is never read for it, because a process able to
   rewrite attempts can rewrite that file too. Resume integrity is only as strong
   as the digest you hold: keep it outside agent write authority, and a digest of
   tampered evidence carries the tampered attempts forward. Any identity change needs
   a new output directory. Expand to the other cases only after the first pair shows
   observed calls, a matching server trace and a graded receipt.

The pair is a measurement of one scenario. It is not a promotion decision.

## Cooperative skill mutations

Registry import, inbox acceptance, install, sync, uninstall, rollback and skill
projection recovery share a workspace transaction lock. Pack imports and profile
projection use the same lock. Nested commands are reentrant for the owning
thread. Approved identities are checked inside that transaction before mutation.
Lock files must be plain single-link files. Windows uses handle-relative opens
and byte-range locks. Unavailable platform primitives refuse mutation.

This lock serializes cooperating commands. It does not prevent a process with
filesystem access from ignoring the lock or replacing files directly. Existing
anchored path checks and package identity checks remain necessary.
