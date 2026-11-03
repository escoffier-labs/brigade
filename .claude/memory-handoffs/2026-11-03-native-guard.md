# Memory Handoff

## Type

project-context

## Title

Optional PCRE2 guard backend compatibility boundary

## Summary

Guard can delegate raw rule matching to the local brigade-guard binary using protocol 1. Python retains all policy filtering and output behavior. Unicode and custom patterns retain Python matching because regex semantics differ across engines.

## Durable facts

- Source patterns remain in Python rules and policies. Rust receives patterns, flags and ids on stdin.
- BRIGADE_GUARD_BACKEND selects auto, python or native. Native bypasses the small-input threshold. Compatibility checks and fallback remain active.
- BRIGADE_GUARD_BIN selects the binary. No setup installer or release packaging is included.
- scripts/verify-guard-native exercises parity both ways and guard tests with native enabled. Its binary integration assertion prevents a missing binary from silently satisfying native coverage.

## Evidence

- files changed: src/brigade/guard/native.py, engines/brigade-guard/src/main.rs
- commands run: brigade work verify run --target . --argv-json '["./scripts/verify-rust-guard"]' --capture brigade-work
- Rust receipt: 20261103-050553-work-verify-336cbb completed, exit 0
- MSRV receipt: 20261103-050716-work-verify-04a48c completed, exit 0

- Native guard receipt: 20261103-050604-work-verify-520e5c completed, 21,556 tests and 1,600 subtests passed.
- Full gate receipt: 20261103-050733-work-verify-fcc7c1 completed, eight shards passed, combined coverage 86%.
- Exact parity-fixture native assertion: 20261103-051629-work-verify-2869d8 completed, 23 focused tests passed.

### Code graph impact

After synchronization, `brigade code affected src/brigade/guard/engine.py --json` reported the following lower-bound impact set. Initial queries before synchronization reported missing coverage.

- Modules: `src/brigade/bug_redaction.py`, `src/brigade/guard/audit.py`, `src/brigade/guard/baseline.py`, `src/brigade/guard/cli.py`, `src/brigade/guard/git_commits.py`, `src/brigade/guard/git_scan.py`, `src/brigade/guard/n8n_advisory.py`, `src/brigade/guard/pr_draft.py`, `src/brigade/guard/pr_prepare.py`, `src/brigade/guard/publish_check.py`, `src/brigade/memory_vault.py`, `src/brigade/obsidian_vault.py`, `src/brigade/release_cmd/candidate.py`, `src/brigade/bug_cmd.py`, `src/brigade/bug_diagnose.py`, `src/brigade/bug_instruction_diagnose.py`, `src/brigade/bug_last_error.py`, `src/brigade/guard/git_scan_target.py`, `src/brigade/guard/n8n_validate.py`, `src/brigade/release_cmd/ci.py`, `src/brigade/cli/__init__.py`, `src/brigade/cli/guard.py`, `src/brigade/guard/__main__.py`, `src/brigade/release_cmd/evidence.py`

- Tests: `tests/guard/test_allow_values.py`, `tests/guard/test_baseline.py`, `tests/guard/test_code_shaped_false_positives.py`, `tests/guard/test_engine.py`, `tests/guard/test_fstring_quoted_fields.py`, `tests/guard/test_hostname_option_context.py`, `tests/guard/test_js_async_scope.py`, `tests/guard/test_js_comment_terminators.py`, `tests/guard/test_js_contextual_words.py`, `tests/guard/test_js_interpolation_comments.py`, `tests/guard/test_js_multiline_context.py`, `tests/guard/test_js_reserved_roles.py`, `tests/guard/test_js_template_context.py`, `tests/guard/test_js_template_token_roles.py`, `tests/guard/test_js_unicode_digits.py`, `tests/guard/test_native.py`, `tests/guard/test_provider_unicode_boundary.py`, `tests/guard/test_public_fixture_materialization.py`, `tests/guard/test_root_cross_category_overlap.py`, `tests/guard/test_root_nested_privacy.py`, `tests/guard/test_root_nested_secrets.py`, `tests/guard/test_rules.py`, `tests/guard/test_shell_assignment_words.py`, `tests/guard/test_shell_declaration_expansions.py`, `tests/guard/test_yaml_shell_prefix_context.py`, `tests/test_bug_redaction.py`, `tests/guard/test_audit.py`, `tests/test_bug_report.py`, `tests/test_bug_review_regressions.py`, `tests/guard/test_allow_cli.py`, `tests/test_claim_loss_review.py`, `tests/test_external_scanner_isolation.py`, `tests/test_memory_vault_propose.py`, `tests/test_release_cmd.py`, `tests/test_self_invocation_scanners.py`

## Recommended memory action

no-card

## Target document

.learnings/LEARNINGS.md

## Suggested document content

### PCRE2 guard compatibility

Python and PCRE2 have different Unicode classes and case-folding tables. Keep unproven inputs on Python. Successful PCRE2 compilation does not establish parity. The optional native guard currently accelerates built-in ASCII rules and rejects unsupported protocol versions.
