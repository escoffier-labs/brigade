# Codex instruction-cap diagnosis and repo trim

Date: 2026-10-27
Status: Slice 1 approved for implementation. Follow-on slices remain on hold.

Tracking: [epic #2088](https://github.com/escoffier-labs/brigade/issues/2088).

## Goal

Help users identify project instructions that Codex would truncate, understand
the byte limit used for that diagnosis, and choose between reducing startup
instructions and increasing the configured cap.

Ship configuration-aware Brigade doctor reporting first. Follow with an
explicit repo target in Bootstrap Doctor. Brigade diagnoses and suggests.
Bootstrap Doctor owns proposed edits and application. Neither tool changes
instruction files during diagnosis or a trim preview.

## Existing behavior

- `src/brigade/doctor_instruction_chain.py` calls the existing context preview
  with a default-assumed cap of 32,768 bytes and labels the result
  `estimate, not observed`.
- `src/brigade/context_preview.py` accounts for the selected root-to-cwd chain,
  override and fallback selection, consumed raw bytes, and rendered UTF-8 bytes.
  Its contract is pinned to Codex 0.160.0. It does not discover configuration or
  observe a running session.
- `docs/codex-context-preview.md` documents configuration provenance, trust,
  resource ceilings, platform limitations, and session uncertainty.
- `src/brigade/managed.py` runs Bootstrap Doctor status and lifecycle lint as
  operator-scoped advisory checks. The integration contract is in
  `docs/phase-bootstrap-doctor-lint-integration.md`.
- Bootstrap Doctor currently targets OpenClaw bootstrap files and character
  budgets. Its trim pipeline shortlists sections with heuristics, requires a
  configured LLM judge, and moves approved sections into memory cards while
  leaving links. Making the judge optional would be new behavior.

## Contract

### Slice 1: configuration-aware doctor reporting

This slice is independently useful and must not depend on repo trim shipping.

- Keep the check conditional on the Codex harness and preserve its advisory
  role. No configuration or instruction-file writes, model calls, automatic
  installs, or Codex process invocation.
- Discover the user base config at `CODEX_HOME/config.toml` when `CODEX_HOME`
  is nonempty. Empty or unset means `~/.codex/config.toml`. A nonempty custom
  home must resolve to an existing directory. Missing or non-directory custom
  homes report `MANUAL` without falling back to the default home.
- Follow symlinks for config discovery, matching the pinned consumer. Resolve
  a relative custom home against the doctor process's working directory,
  then canonicalize it. Do not reject it merely for being relative. Report
  that discovery uses the doctor's environment and launch directory, which
  may differ from a Codex session's environment.
- Open configuration non-blocking and require a regular file after opening.
  Bound the read to 1 MiB, checking for overflow with at most one extra byte.
  Nonregular, unreadable, changed-during-read, or over-bound inputs are
  unevaluated. This config-read bound is independent of the instruction cap.
- Expose only allowlisted setting values and sanitized provenance. Use logical
  source labels such as `$CODEX_HOME/config.toml` or `~/.codex/config.toml`,
  without exposing resolved personal paths. Never echo unrelated settings,
  credentials, config bodies, or raw parser errors.
- Parse present configuration with stdlib `tomllib`. Do not use the permissive
  compatibility parser to establish Codex config validity. On Python 3.10,
  where stdlib `tomllib` is unavailable, report `MANUAL` for a present config
  with an opaque parser-unavailable reason. Missing config still permits a
  labeled default assumption. Document this limitation and retain the explicit
  supplied-cap preview route. Do not add a dependency or a new TOML parser.
- Initially read only the top-level user-base `project_doc_max_bytes`. This is
  a configured value, not a claim to resolve Codex's effective configuration.
  Explicitly disclose that profile, project, system, managed, and launch-time
  overrides are unresolved. Do not infer the active profile from the shell or
  claim to discover an existing session's overrides.
- When the file or setting is absent, retain the pinned default assumption of
  32,768 bytes and label it as an assumption. A present but malformed,
  unreadable, unsafe, or oversized config is not absence: report `MANUAL` with
  an opaque reason. Do not silently use the default.
- Accept nonnegative integer caps, including zero. Reject booleans, negative
  values, strings, and other invalid types. Preserve the preview's 1 MiB
  resource ceiling. Larger configured values produce an unevaluated result.
  Never clamp them or describe that ceiling as a Codex limit.
- A zero cap reports advisory `WARN`: project instruction loading is disabled
  under the configured or assumed cap, and instruction files were not
  inspected. Never describe this outcome as an absence of instruction files.
- Feed the discovered cap into the existing preview. Keep its public
  caller-supplied API and explicit supplied-value route intact. Do not add
  implicit config discovery to the preview itself.
- Doctor owns the cap provenance shown in its result: `user-base-config` or
  `default-assumption`. Do not substitute preview's `caller-supplied` label
  for a value doctor discovered. Preview's own provenance contract is unchanged.
- Preserve the Codex 0.160.0 accounting contract and existing assumptions for
  cwd, trust, access, fallbacks, and root markers. Report those assumptions.
  Do not label the whole result effective merely because the cap was read.
- Keep `actual_session_observed: false`. Configuration provenance and session
  observation are separate facts. A suitable human summary is:
  "Configured user-base cap: 65,536 bytes. Fresh-disk chain estimate. Active
  session not observed. Other configuration layers unresolved."
- Preserve `OK` for a complete chain within a positive modeled cap, `WARN` for
  modeled truncation or disabled loading, and `MANUAL` for unevaluated accounting.
  An `OK` describes the
  chain under the stated assumptions. Live-session loading remains unobserved.
- Retain the existing explicit preview remedy for supplying known settings,
  including its quoting, extras enablement, and unsupported-platform behavior.
  When a configured cap is known, include `--project-doc-max-bytes <N>` so the
  rerun uses the same cap. Above the instruction inspection ceiling, explain
  the Brigade resource limitation and omit the unusable rerun command.
- When truncation is modeled, explain both remedies: reduce startup
  instructions or increase `project_doc_max_bytes` if the full chain is
  necessary. Configuration changes require a new run or restarted session.
  Do not edit config or prescribe an arbitrary larger cap.
- Do not emit a repo-trim command before Bootstrap Doctor supports it.
- Update `docs/codex-context-preview.md` to describe the new doctor behavior.
  Keep doctor's existing result tuple and JSON envelope. Slice 1 adds detail
  text, not new structured fields. Adopt the exact-byte and secondary KiB
  display in slice 1, updating the corresponding output assertions.

### Units and scope across the epic

- Codex limits use bytes. Display exact counts, with KiB as a secondary label:
  `32,768 bytes (32 KiB)`. OpenClaw retains its existing UTF-16 code-unit
  accounting and trimming rules, labeled as OpenClaw characters.
- Keep byte, character, and Brigade advisory budgets visibly distinct in
  flags, structured output, and human summaries. Do not introduce an
  ambiguous shared `limit` setting or estimate bytes from character counts.
- Repo projections must count the encoded file bytes, including line endings.
  OpenClaw's normalized character counts cannot supply these totals. Preserve
  the preview's distinction between raw consumption and rendered UTF-8 bytes.
- The Codex budget applies to the selected chain for a particular cwd, not
  separately to each file. Moving content to a nested instruction file only
  helps where that file is excluded from the selected chain.

### Follow-on slices: repo preview, application, and remedy

These establish the epic boundary. Implementation requires separate authorization.

- Add an explicit repo target to Bootstrap Doctor without changing OpenClaw
  defaults or classification. Repo operation must not select the operator's
  OpenClaw workspace implicitly.
- Before repo preview, define a data-only, versioned handoff based on Brigade's
  preview accounting. Include schema and consumer versions, approved repo
  scope and relative cwd identity, ordered selected files, source fingerprints,
  cap and provenance, completeness, and limitations. The current preview JSON
  is not assumed to contain every required field. Slice 1 does not change it.
- Bootstrap Doctor consumes that handoff and revalidates file identities before
  planning. It performs no Codex config discovery or chain selection and does
  not import Brigade as a Python dependency. Its projected-byte arithmetic must
  pass conformance vectors maintained with Brigade's accounting contract.
  A cap alone is insufficient. Missing or stale handoffs require a new explicit
  inspection. Do not add a runtime dependency without a separate conversation.
- Repo-mode destinations stay inside the approved repository, are supplied
  explicitly or by a repo-specific default, and must not be loaded as part of
  an evaluated instruction chain. Links are relative to the source file and
  portable across clones. Never inherit the operator's OpenClaw cards directory.
- Preserve the original UTF-8 encoding and line endings in repo mode. Compute
  before/after totals and diffs from the exact bytes to be written, including
  breadcrumbs and newly created files. Reject unsupported encoding or newline
  cases explicitly instead of silently normalizing the entire source file.
- Start with a dry-run plan showing selected files, cwd, cap provenance,
  before/after byte totals, proposed diffs, relocated destinations, and any
  remaining overflow. A plan need not reach the cap if safe moves are exhausted.
  State exactly which cwds and chains were evaluated. Editing a shared ancestor
  does not justify a claim that every descendant chain has been checked.
- Decide explicitly whether repo planning retains the required judge or adds
  a deterministic mode. A missing model must never be hidden behind a claim
  that the existing judge is optional. A judge requires a configured model and
  an OpenAI-compatible endpoint, which need not be OpenClaw. Document cache writes separately from
  the guarantee that previews do not edit instruction files.
- Keep required startup rules in startup context. Relocation makes content
  load on demand. A breadcrumb alone does not preserve automatic loading.
  Exclude managed/generated blocks unless their owner supports relocation.
  Define the protected-section and selection rules before the preview ships.
  Preview must never propose relocating protected content.
- Application remains explicit through `trim --apply`, with source-change
  validation, destination validation, clean-workspace protection, atomic
  writes, cards written before source rewrites, and post-apply accounting.
  Bind application to the reviewed plan using its digest, source fingerprints,
  scope/cwd, and accounting inputs. Do not silently rerun a judge to substitute
  a different plan. Repo mode must not use `--force` to bypass clean-workspace,
  stale-plan, destination, or protected-content checks. Reinspect the evaluated
  chains through Brigade after application and report any remaining overflow.
- Only after the repo command ships, add an exact, safely quoted dry-run remedy
  to Brigade doctor. Check installed capability, or provide an honest upgrade
  hint. Never automatically run trim or suggest `--apply` as the default.
- Keep the existing operator status/lint probes and their advisory mapping.

## Tests first

For slice 1, extend `tests/test_doctor.py` and use focused resolver tests if a
separate module is warranted. Use synthetic homes and configs only.

- A configured cap changes the modeled truncation result in both directions.
- Custom `CODEX_HOME` selects the intended config without reading the default
  home. Missing file and missing key preserve labeled default assumptions.
- Empty home uses the default. Relative and symlinked homes and symlinked config
  files are supported. Missing or non-directory custom homes never fall back.
  Relative resolution uses the process cwd even when doctor targets another repo.
- Invalid TOML, denied reads, unsafe paths, and oversized files report
  uncertainty without leaking config contents or raw errors.
- Cover nonregular inputs, changed-during-read input, the 1 MiB config-read
  boundary, and overflow detection. Strict TOML rejects duplicate keys and
  accepts valid multiline strings on supported runtimes. Present config on
  Python 3.10 is `MANUAL` without calling the permissive fallback parser.
- Zero, negative, boolean, string, boundary, and over-resource-ceiling values
  follow the contract without silent coercion or clamping.
- Zero is `WARN` with disabled-loading and not-inspected wording. A rerun
  command carries the discovered cap. An over-ceiling cap has no unusable rerun.
- Provenance distinguishes discovered user-base values from defaults and
  caller-supplied preview values. Other layers remain explicitly unresolved.
- A configured cap never changes `actual_session_observed` to true or removes
  the other assumptions from an apparently clean diagnosis.
- Existing override, nested-chain, multibyte, whitespace, empty-file, and
  truncation accounting remains covered by the preview tests.
- Doctor does not write, spawn Codex or a model, install a tool, or run trim.
  Remedy quoting, extras handling, and platform limitations remain covered.
- No unsupported repo-trim command is printed.

Observe the relevant new tests fail before implementation, then make the
smallest change that satisfies the contract. Before non-trivial code changes,
record the actual code-graph impact set, including tests.

Follow-on plans must add tests for chain-wide projected bytes, CRLF and
multibyte content, protected sections, managed blocks, dry-run file preservation,
stale plans, destination collisions, partial failures, repeated application,
remaining overflow, and installed-command capability. Finalize those contracts
before writing their implementation tests. Include handoff conformance and
identity checks, portable in-repo destinations, a protected-content preview
rejection, exact plan binding, and refusal of repo-mode `--force` bypasses.

## Verification

For slice 1 implementation, run the focused gate through Brigade, adding any
new resolver test selector to the same invocation:

```bash
brigade work verify run --target . --argv-json '["./scripts/verify-focused","tests/test_doctor.py","tests/test_context_preview.py"]' --capture brigade-work
```

Ensure subprocesses import this worktree's source when a shared environment is
used. Report the actual result and any failure verbatim.

Reserve the full gate for PR, merge, release, or verification-infrastructure
work, according to `AGENTS.md`. Run it through Brigade without `--no-reuse`.
Status 75 is slot contention and must not trigger a larger-timeout retry.
Bootstrap Doctor implementation uses that repository's own verification gate.

For this phase document, use the docs-only content scan from `CONTRIBUTING.md`,
the house prose checks, and a whitespace check. No code tests are needed for
the document alone.

## Out of scope

- Building follow-on slices before separate implementation authorization.
- Full Codex config resolution or observing a live session's consumed prompt.
- Upgrading the pinned consumer contract as part of cap discovery.
- Automatically editing instructions, changing caps, installing tools, or
  launching a judge from Brigade doctor.
- A guarantee that shortening instructions preserves behavior or that being
  under a cap guarantees instruction adherence.
- New truncation checks for Claude Code, Cursor, or OpenCode. Their loading
  contracts and any hard limits need separate evidence and phase plans.
- Changing OpenClaw character budgets or conflating them with Codex bytes.
- New runtime dependencies, releases, PRs, or commits during this design-only
  task. Updating the existing epic is authorized tracking work.

## Review terminal state

Implement and verify slice 1 under the repository contribution and review
policy. Track its progress in epic #2088. Do not continue into the follow-on
slices or modify Bootstrap Doctor without separate authorization.
