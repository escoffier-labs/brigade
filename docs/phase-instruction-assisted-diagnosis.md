# Codex instruction visibility diagnosis

Status: implemented locally, PR gates pending. Solomon authorized the proposed
Codex project-instruction slice through protected merge on 2026-10-29. The CLI
details below are implementation choices within that scope.

## Intent

Explain why the instruction chain modeled for a selected project and working
directory differs from what an operator expected. Reuse the existing bounded
Codex instruction preview and the assisted diagnosis output controls.

This is configuration-based instruction visibility. Actual session loading
remains unknown. Memory-card retrieval and model behavior are outside this slice.

## Approach

Compose `context_preview.preview` with `bug diagnose`. This preserves the existing
version contract, file selection, resource bounds and descriptor protections.
Reimplementing instruction discovery would duplicate the sensitive read boundary.
Wrapping the ordinary context CLI would add parser, ledger and printing behavior
that the diagnosis does not need. The pure preview result is the evidence seam.

Use `brigade bug diagnose --component instructions`. The existing default remains
`mcp`. Instructions require `--harness codex`, an explicit absolute `--target`
and `--cwd`, `--codex-version`, `--trust`, `--read-access` and `--summary`.
Expose the existing project settings `--project-doc-max-bytes`,
`--fallback-filenames`, `--root-markers` and `--assume-codex-defaults`.
Defaults must retain the producer's `default-assumption` provenance.
The current producer supports caller-supplied Codex 0.160.0 on POSIX only.

An optional `--instruction-path` identifies an expected project-relative file.
Reject absolute paths, traversal, empty paths and terminal controls. Comparing
that label with selected rows must not open or stat an additional file.
Reject MCP-only `--name` in instruction mode and instruction settings in MCP mode.
There is no global-home option, discovery, version probe or session attachment.

## Operator workflow

1. Supply the symptom, approved project, working directory and effective settings.
2. Preview redacted expected and observed instruction selection, contribution
   and uncertainty. File bodies and absolute input paths are never printed.
3. Read the next check, which confirms the working directory and settings before
   using the existing context preview. It never runs a harness or a repair.
4. Explicitly export one new finding or prepare a local bug-report preview.
   Existing review, redaction, security blocking and consent controls still apply.

## Evidence contract

- Use only `context_preview.preview` with project scope. Do not call memory-store
  discovery, general diagnostics, last-error readers, search logs or transcripts.
- Project settings and caller-supplied version are assumptions, not observations
  of the active operator. Always emit `actual_session_observed: false`, runtime
  `unknown` and verification `not-run`.
- Explain selected files, filename precedence and ordered contributions without
  claiming that a nearer instruction file replaces all ancestor instructions.
- Distinguish a modeled complete chain with no selected files from an expected
  label absent from the selection. The latter is `not_selected`, not proof that
  the file is missing. Do not invent a reason the producer cannot establish.
- Explain zero-cap or untrusted loading exclusions, exhausted byte budgets,
  partial truncation and empty files using producer evidence and supplied settings.
- Unsupported versions, unknown settings, unsafe paths and incomplete inspection
  remain unevaluated. Do not label their instruction chains healthy or missing.
- Keep the producer's bounded refusal before content reads. Bound raw JSON setting
  strings before parsing them. Project scope never expands to the operator home.
- Export only a redacted allowlist of diagnostic fields. Hash public redacted
  findings and public modeled settings, never instruction bodies or raw files.
- Default preview has no intentional writes and skips global error/update hooks.
  Access-time changes remain possible as documented by the producer.
- Export stays exclusive. Import stays separate and pending. Local report
  preparation uses only the collected finding and never submits an issue.

## Proof and merge gates

Use isolated temp repositories and fake settings. Prove a missing chain,
same-directory override selection, nested working-directory differences,
expected-path nonselection, truncation and exhaustion, disabled loading,
unsupported assumptions, unsafe inputs, redaction and body-free output.
Protect default no-write behavior, exclusive export, trusted importer identity
and local report preparation with ambient-process and transport sentinels.
Keep existing MCP diagnosis and context preview regressions passing.

Before the PR is ready, capture real base and head terminal screenshots at the
same geometry using the same fake fixture and command. Include unchanged MCP
behavior. Use a temporary base snapshot or native T3 isolation, never a shell
worktree. Inspect every image and host it on `pr-assets`, preserving other assets.

Run focused and full gates through Brigade, independent code and security
review, current-head CI and applicable human requirements. Use the protected
merge queue and verify the merged commit. Publication and live activation stay held.

## Smallest implementation

Add one instruction-result adapter, a component branch in diagnosis and parser
arguments. Reuse export, report and redaction helpers. No new runtime dependency,
command family, scanner, configuration store, provider launcher or database.
Expand only when another harness has an explicit supported evidence contract or
Solomon authorizes another memory visibility boundary.
