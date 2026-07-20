# Hyper-V native acceptance

This is a maintainer-operated acceptance gate. It is not a GitHub Actions self-hosted runner.

Prepare a Windows VM with PowerShell Direct enabled, a guest copy of
`scripts/windows-native-acceptance.ps1` at `C:\BrigadeAcceptance`, Python, pipx bootstrap
prerequisites, and Git. Do not install Go or Cargo. Create a checkpoint named `clean` after
that preparation and before any release acceptance run.

Run the host orchestrator with the exact immutable release tag's version, without its `v`:

```powershell
./scripts/hyper-v-native-acceptance.ps1 -VmName brigade-clean -BrigadeVersion 1.2.3 -Credential (Get-Credential)
```

The orchestrator stops the VM, restores `clean`, starts it, verifies Go and Cargo are absent in
the guest, and invokes published Windows acceptance with `-InstallMode pypi`. Its JSON output
records the exact immutable release tag and checkpoint. Git may be present in the guest.

Do not reuse an already-booted VM, rename the checkpoint, or substitute a branch, moving asset
URL, or source install for the tag-based run.
