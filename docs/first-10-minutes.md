# First 10 Minutes With Brigade Stack

Start with one Git repository you control. This walkthrough uses the published 0.30.0 bridge and records a check you can inspect afterward. It needs no model credentials, services, or schedulers.

## 1. Install and check the command

Choose an install route in [Quickstart](../QUICKSTART.md#1-install). For npm:

```bash
npm install --global brigadestack@0.30.0
brigadestack --version
brigadestack --help
```

Expected version: `brigade 0.30.0`. The bridge exposes `brigadestack` and delegates to the compatible Brigade CLI. Existing users can use their `brigade` command throughout. With npx, prefix every invocation as described in [Quickstart](../QUICKSTART.md#npm).

## 2. Pick a target

Enter an existing repository:

```bash
cd ./my-repo
```

Or create a disposable repository on Linux or macOS:

```bash
target="$(mktemp -d)"
git init -q -b main "$target"
cd "$target"
```

In PowerShell:

```powershell
$target = Join-Path ([System.IO.Path]::GetTempPath()) ([System.Guid]::NewGuid().ToString())
git init -q -b main $target
Set-Location $target
```

## 3. Preview

```bash
brigadestack operator quickstart --target . --harnesses codex --dry-run
```

Replace `codex` with the tools you use, such as `codex,claude`. Dry-run lists planned steps without applying them. For a file-by-file template preview:

```bash
brigadestack init --target . --harnesses codex --dry-run
```

## 4. Apply

```bash
brigadestack operator quickstart --target . --harnesses codex
```

The isolated Linux smoke returned `status: ok` and exit 0. Other hosts may report warnings or failures. Inspect the named step and its suggested command before treating setup as ready. A warning is not proof that every check passed.

Quickstart creates local configuration under `.brigade/`, including MCP catalog and work-loop configuration. It selects handoff source folders for the writer harnesses you chose. For Codex, that inbox is `.codex/memory-handoffs/`. It does not start services or install schedulers.

Review the generated changes with `git status` and `git diff` before committing. The managed handoff `TEMPLATE.md` can travel with the repository. Session notes and local `.brigade/` state should stay private. See [agent-assisted setup](agent-assisted-setup.md) for adoption in an existing workspace.

## 5. Check health and read the brief

```bash
brigadestack operator doctor --target . --profile local-operator
brigadestack work brief --target .
```

On the isolated stable smoke, the operator doctor reported:

```text
ready: yes
blocking_issues: 0
```

Read the full output on your machine. Optional wiring may still produce advisories. The brief can list missing optional configuration for surfaces you have not enabled. You do not need to configure every surface to record a first check. If a doctor reports a blocking issue, use its suggested diagnostic and resolve it before relying on that surface.

## 6. Run a check and inspect its receipt

Start with a Git check that works in the scratch repository:

```bash
brigadestack work verify run --target . --command "git diff --check" --capture brigade-work
```

`git diff --check` checks your unstaged diff for whitespace errors. In an empty repository it passes without checking application behavior. For actual development, replace it with your project's check command, such as `pytest -q` when pytest and the tests are installed.

The command prints the run ID, status, command exit code, and receipt path. The receipt includes log paths. Open the printed receipt and logs under `.brigade/work/verify-runs/<run-id>/`. Confirm that the receipt records the command you intended, the target, and exit 0. A failed command leaves a failed receipt to inspect. Repeated checks may reuse a matching passing receipt.

`--capture brigade-work` attributes the observed result to the installed work-loop skill. This ad hoc command produces audit evidence. It does not score an artifact. Artifact scoring needs an eligible registered verifier manifest. See [verification receipts](technical-guide.md#daily-work-loop) for details.

Run the brief again:

```bash
brigadestack work brief --target .
```

It now has the verification result available for the next agent.

## 7. If a step fails

Collect the exact command, exit code, OS, selected harnesses, and version. For setup diagnosis:

```bash
brigadestack --version
brigadestack operator doctor --target . --profile local-operator --json
brigadestack operator verify-harness --target . --harness codex --json
```

Review output before sharing it. Remove tokens, private hostnames, repository names, and absolute personal paths. Use the [setup problem form](https://github.com/escoffier-labs/brigade/issues/new/choose) with the redacted evidence. Do not rerun setup with `--force` to suppress an unexplained failure.
