# Codex instruction diagnosis implementation plan

> For agentic workers: use the subagent-driven-development or executing-plans
> skill. Read the design before changing source. Keep ownership bounded.

**Goal:** explain a modeled Codex project instruction chain through assisted
diagnosis and carry the reviewed change through protected merge.

**Architecture:** translate the existing body-free `context_preview.preview`
result into redacted findings. The existing diagnosis wrapper owns export and
local report preparation. The preview producer's contract is unchanged.

**Tech stack:** existing Python standard library, argparse and pytest.

**Spec:** `docs/phase-instruction-assisted-diagnosis.md`.

## Constraints and ownership

- Work from refreshed main `994468e0f`, branch `feat/codex-instruction-diagnosis`.
- Preserve the existing MCP default and the CLI no-write exemption.
- Project-only POSIX Codex 0.160.0 modeling, with explicit caller settings.
- No additional dependencies, private transcripts, live configuration mutation,
  runtime probes, model launches, automatic fixes or issue submission.
- Production worker owns `src/brigade/bug_instruction_diagnose.py`,
  `src/brigade/bug_diagnose.py`, `src/brigade/cli/bug.py` and
  `tests/test_bug_instruction_diagnose.py`.
- Parent owns design, plan, guide, changelog, command inventory, screenshots,
  integration review, PR and merge. Reviewers remain read-only unless assigned
  a distinct test file. Do not revert another worker's changes.
- Pre-edit graph sync and affected query recorded in
  `/tmp/instruction-diagnosis-impact.json`: 275 affected test files, no missing
  indexed input paths and no truncation. Static attribution is a lower bound.
  New files need explicit tests and a graph refresh after implementation.

## Review focus

1. Unsupported settings must refuse before instruction content reads.
2. A nonselected expected file must not become a false missing-file assertion.
3. File bodies, private path labels and forged verification must not escape.
4. Invalid or refused diagnosis must not record global errors or check updates.
5. Report preparation and export must preserve the existing side-effect boundary.

## Task 1: instruction diagnosis vertical slice

**Consumes:** `context_preview.preview(*, target, cwd, codex_version, trust,
read_access, assume_codex_defaults=False, project_doc_max_bytes=None,
fallback_filenames=None, root_markers=None, codex_home=None,
global_max_bytes=None) -> dict`.

**Produces:** an instruction adapter `_compare(args) -> dict` with a body-free,
redacted finding and the diagnosis wrapper's existing `text`, `runtime` and
`verification` fields. The worker can choose a clearer local helper name.

- [ ] Add a public CLI regression that fails because instruction mode is absent.
  Pin `actual_session_observed is False`, `runtime == 'unknown'` and
  `verification == 'not-run'`.
- [ ] Run that regression through `brigade work verify run` and inspect the red
  failure before product edits.
- [ ] Register `--component instructions` and explicit project settings. Reject
  mixed-component arguments and unsupported harnesses without reads.
- [ ] Translate allowlisted producer evidence. Test missing, selected override,
  changed cwd, `not_selected`, truncated, cap-exhausted, disabled and unevaluated
  states. Assert that an expected path not in the result causes no extra reads.
- [ ] Reuse output controls and choose an instruction-specific local report
  title. Test exclusive export and import validation, pending ingest, and local
  report preparation with no ambient collection or network.
- [ ] Add adversarial body, terminal-control and fake-secret cases. Check text,
  decoded JSON, export and saved report. Test stable public digests when only
  undisclosed instruction text changes without changing public findings.
- [ ] Run the focused gate through Brigade for the new test file,
  `tests/test_bug_diagnose.py`, `tests/test_bug_diagnose_security.py`,
  `tests/test_context_preview.py`, `tests/test_context_cmd.py`,
  `tests/test_bug_report.py` and `tests/test_update_notify_entry.py`.
- [ ] Report exact commands, results, receipts and source impact. Do not commit
  or push until parent integration review. Do not weaken existing expectations.

## Task 2: integration, evidence and protected merge

- [ ] Review the final diff independently and through the approved security
  route. Fix verified findings, rerun only justified checks and obtain re-review.
- [ ] Add an operator guide and one changelog entry. Regenerate command inventory
  using its existing command. Run Vale and content checks on new prose.
- [ ] Capture and inspect base/head and unchanged-behavior terminal images from
  real command execution at one fixed geometry. Publish scrubbed PNGs only to
  the existing `pr-assets` branch through a normal guarded push or Git data API.
- [ ] Run the full gate once through Brigade for the final PR head. Preserve
  receipts as audit-only and do not bypass occupied full-gate slots.
- [ ] Commit, push and link the PR immediately. Include the Before/After table,
  exact validation, uncertainty and supported contract in its body.
- [ ] Request configured free reviews once stable. Read actual reviewed commits,
  all actionable comments and required CI. Do not mistake quota for approval.
- [ ] Own fixes and re-review. Enqueue only after current gates clear, then
  verify merged state and commit. Finalize the durable handoff and unwatch.
