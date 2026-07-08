# Security Policy

## Supported versions

Brigade is in alpha. Only the latest minor release on the `main` branch receives security fixes. Pin to a released tag if you need a known-good version.

## Reporting a vulnerability

Please **do not** open a public GitHub issue for security problems. Email **me@solomonneas.dev** with: <!-- content-guard: allow pii/email -->


- A short description of the issue.
- Steps to reproduce (or a minimal proof of concept).
- The version or commit you tested against.
- Whether you would like to be credited in the release notes.

You should get an acknowledgment within 72 hours. If you do not, please follow up - the mail may have been filtered.

## In scope

- Code execution, path traversal, or symlink-attack flaws in `brigade init`, `doctor`, `scrub`, or the ingester.
- Template content that leaks credentials, tokens, or personal data into a target workspace.
- Public-leak guard bypasses (cases where the content-guard pre-push hook fails to flag content it is configured to catch).
- Profile manifests that write outside `--target` (the manifest validator should reject these).

## Runbooks execute arbitrary shell

`brigade runbook run` executes the `run` string of every step as a shell command on the operator's machine. Treat a runbook file as **arbitrary shell that is only as trustworthy as whoever wrote it** - including any agent that can write a `.json` file into the workspace.

- Execution requires the **operator** to pass `--approved` on the command line. An `"approved": true` baked into the runbook file is **ignored** and never authorizes execution. This keeps a human in the loop: run `brigade runbook plan <file>` (or `runbook run <file> --dry-run`) to read every command before approving.
- The `allowed_commands` allowlist validates the whole command, not just the first token, and refuses an inline-script shell wrapper (for example `bash -c "..."`) because such a wrapper can run anything regardless of the allowlist.
- The built-in destructive-pattern deny-list is **advisory only**. It catches a few obvious shapes but is trivially bypassable (`find / -delete`, `dd`, `curl ... | sh`, and so on). Do not rely on it as a security boundary; the boundary is the operator reviewing the steps before approving.
- Optional runbook pins are also advisory. `brigade runbook pin <file>` records `command`, resolved absolute `path`, and `sha256` for the current binary behind each step's first token, then `runbook plan` and `runbook run` can report whether those binaries still match. Pins help detect local executable drift, but they do not sandbox execution, prove that a command is safe, or inspect files loaded by the command.
- Pins cover `argv[0]` only. For `bash script.sh`, `python script.py`, or a similar interpreter step, the pin covers the interpreter binary, not the script, package imports, shell profile, environment, or network content the interpreter may load.
- If a pin has `version_cmd`, Brigade runs the resolved pinned binary with those arguments (for example `--version`) during `runbook pin` to refresh the stored `version`, and during `runbook run` pin verification to record runtime `version_output` in the receipt, so the version always describes the same file the hash covers. `runbook plan` and `runbook run --dry-run` never execute `version_cmd`. Review `version_cmd` arguments the same way you review step commands.

## Receipt digests and optional local signatures

Work verification receipts, runbook receipts, and outcome ledger records use SHA-256 digests to make ordinary local drift visible. `brigade receipts verify` recomputes receipt payload digests, stdout and stderr log digests, and the outcome ledger `prev_digest` chain.

The digest layer is tamper-evident bookkeeping. It can detect hand-edited receipt fields, changed or missing logs, edited outcome records, and deleted middle ledger records. By itself, it does not defend against an attacker who can rewrite both a receipt and its stored digests, or an attacker who can rewrite and re-chain the ledger tail after changing a record.

Brigade also supports an optional single-machine HMAC tier for work verification and runbook receipts. `brigade receipts keygen --target .` creates a local `.brigade/receipt-signing-key`, or `BRIGADE_RECEIPT_SIGNING_KEY_FILE` can point at another key file. When a key is available, receipt writers store `digests.signature` and `digests.key_id`; `brigade receipts verify` validates a matching local key as `SIGNED-OK`.

The HMAC tier defends against receipt-plus-digest rewrites by someone who does not have the local signing key. It does not protect against a trusted key holder, a stolen key, or malware running as the operator. It is single-machine authorship evidence, not PKI and not cross-machine identity. If a receipt carries a signature but the local key is absent, unreadable, rotated away, or has a different `key_id`, verification reports `UNVERIFIABLE-SIGNATURE` without changing the command exit status. If the local `key_id` matches but the HMAC does not, verification reports `SIGNATURE-MISMATCH` and exits nonzero like a digest mismatch.

Key rotation is explicit: run `brigade receipts keygen --force --target .`. Rotation orphans older signatures on that machine into `UNVERIFIABLE-SIGNATURE` unless the old key is supplied through `BRIGADE_RECEIPT_SIGNING_KEY_FILE`. A future cross-machine trust tier can use `minisign` or `ssh -Y sign` for public-key signatures, but that is separate from the local HMAC design.

## Out of scope

- Bugs in `content-guard` itself - please report those upstream at
  <https://github.com/escoffier-labs/content-guard>.
- Bugs in OpenClaw, Hermes, Claude Code, or Codex - report those to their respective projects.
- Issues that require an attacker to already have write access to the user's machine, harness config, or PyPI account.
- Memory cards or handoffs that a user wrote and committed themselves. Brigade provides scaffolding and guardrails, not perfect content review.

## Disclosure

We aim to ship a fix within 14 days of confirming a valid report. A coordinated disclosure timeline can be negotiated for issues that need longer.
