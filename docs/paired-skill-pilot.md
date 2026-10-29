# Paired brigade-work pilot

This local apparatus compares two collected skill packages through a bounded tool
protocol. Its replay command exercises real Brigade verification and outcome code
in disposable targets. Replay results are offline apparatus checks. No live model
pair has been run, and these results cannot score or promote a skill.

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

## Live transport blocker

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
shell, file-write, MCP, and delegation tool. Its arbitrary `config` object does
not prove that a proposed config key is supported. No undocumented key was used.
The approved T3 transport exposed here also provides no per-session interceptor
for these five operations. A read-only filesystem sandbox still permits reads
and does not prove that every tool request is recorded.

The live entry point refuses before launching a model:

```bash
python -m brigade model trial paired-live src/brigade/fixtures/paired-brigade-work/manifest.json --output-dir /tmp/paired-live-evidence
```

It exits 2 because this implementation has no live tool adapter. A live adapter
must expose an exclusive intercepted tool set and a fresh session per arm.
Prove one tool-interacting pair before expanding to all five scenarios. This
limitation describes the supplied implementation and inspected transport. It
does not establish that every provider or deployment lacks such a capability.

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
