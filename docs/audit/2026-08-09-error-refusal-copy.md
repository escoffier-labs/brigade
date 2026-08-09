# Error and refusal copy audit (#739)

Sweep date: 2026-08-09. Classification keys:

| Class | Meaning |
|---|---|
| safe-to-automate | Inline a copy-pasteable command; automating it unattended is acceptable. |
| judgment-required | Describe the situation; point at doctor/docs/`--help`; do not inline the mutating command. |
| destructive/bypassing | Never name the override/bypass flag in the refusal. |

## Flagged messages and changes

| Location | Was | Class | Change |
|---|---|---|---|
| `work_cmd/session/task_ops.py` (`task_done` open children) | `pass --force to close anyway` | judgment-required | Text: `parent has open children`; keep `reason` + `open_children` structured. |
| `work_cmd/session/briefing.py` (workflow_rules) | `brigade init ... --force` for *missing* templates | wrong remediation | Drop `--force`; missing files install without overwrite. |
| `dogfood_cmd.py` / `roster_cmd.py` / `mcp_cmd.py` init | `pass`/`use --force` to overwrite | destructive | Refuse with "leaving it unchanged"; MCP JSON adds `reason=already_exists`. |
| `roster_cmd.py` (fallback shadow) | `Pass --force to scaffold ... anyway` | judgment-required | Describe shadowing risk; no force flag. |
| `receipts_cmd.py` keygen | `hint: pass --force to overwrite` | destructive | Hint that the existing key is left unchanged. |
| `research_cmd.py` export | `pass --force to replace` | destructive | "leaving it unchanged". |
| `learn_cmd.py` propose-skill | `use --force to refresh` | destructive | "leaving it unchanged". |
| `mcp_cmd.py` conflict detail | `--force to overwrite` | destructive | "leaving the live config unchanged". |
| `mcp_cmd.py` / `harness_profile_cmd.py` stdio gate | name `--allow-global-stdio` | gate-bypassing | Describe user-wide stdio risk; require operator acknowledgement without naming the flag. |
| `center_cmd/actions.py`, `repos_cmd/fleet_health.py`, `repos_cmd/release_train.py` | `or pass --allow-unreviewed` | gate-bypassing | Require reviewed/deferred closeout only. |
| `repos_cmd/sweeps.py` health suggestions | `--force` / `--all --force` | wrong remediation | `--repo X` already refreshes; `--all` already implies force. Drop redundant `--force`. |
| `outcome_cmd.py` ledger corrupt / doctor | inline `outcome repair --operator-confirm` | judgment-required | Point at `outcome doctor`; JSON keeps `repair_command` without the confirm flag plus `repair_requires_operator_confirm`. |
| `outcome_repair.py` (own confirm gate) | names `--operator-confirm` | judgment-required | Point at `--help` instead of teaching the flag in the refusal. |
| `install.py` kept-files note | `run with --force to overwrite` | judgment-required | Describe intent to replace templates; no force flag. |
| `runguard.py` DirtyWorktreeError | `pass --allow-dirty to run anyway` | gate-bypassing | Ask to commit/stash/clean only. |
| `doctor.py` publish hook | `run chmod +x hooks/pre-push` | platform-specific | Describe required end state for any Git hook host. |
| `scrub.py` (`hook_status` missing hook) | `brigade init ... --force` in `suggested_commands` | destructive/bypassing | Safe init: `brigade init --target . --depth repo`. |
| `operator_cmd/lifecycle.py` (quickstart install failure) | `brigade init ... --force` in `next_commands` | destructive/bypassing | Point at `brigade doctor --target TARGET`. |
| `runbook_cmd.py` (text approval refusal) | names `--approved` | gate-bypassing | Point at `brigade runbook run --help`; JSON keeps `status`/`runbook_id`. |

## Left intentionally

| Location | Why |
|---|---|
| CLI `add_argument(--force / --allow-* / --operator-confirm)` help text | Documents the flag surface; not a refusal remediation. |
| `work_cmd/verification.py` POSIX high-risk remedy naming `chmod +x` | Already gated behind `_verification_is_windows() == False`. |
| `templates/workspace/SAFETY_RULES.md` / `TOOLS.md` `--no-verify` guidance | Operator safety policy, not CLI refusal copy. |
| Safe remediations (`brigade setup`, `brigade security init`, …) | Safe-to-automate; kept. |

## Contract pin

`tests/test_error_refusal_copy.py` pins the highest-traffic refusal strings and
rejects bypass-flag tokens in those payloads.
