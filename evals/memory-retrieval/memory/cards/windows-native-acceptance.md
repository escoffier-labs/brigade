---
title: Windows Native Acceptance
tags: [ci, windows, acceptance]
description: CI-only job covers Hyper-V native acceptance; local verify is the fast gate.
---

# Windows Native Acceptance

Do not treat `./scripts/verify` as a substitute for the Windows native acceptance workflow. Platform-oriented release checks stay CI-only.

### schtasks missing-task Query

`schtasks /Query /TN` on a deleted `BrigadeCare-*` task prints `ERROR: The system cannot find the file specified` and exits 1. That names the task, not `brigade` or the printed `/TR`. GitHub Actions `shell: powershell` exits `$LASTEXITCODE` after the script, so the expected-not-found Query must be redirected and the success path must `exit 0`. The printed `.cmd` is `/Create` only — do not `/Run` the `/TR` body during validation.
