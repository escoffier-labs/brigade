---
status: active
proof: drive
surfaces: hub connector list, hub connector setup
drive: hub-connector
last-drive: 2026-11-13 920319587
---

# Hub connector discovery and setup preview

## Sub-features

List the three connector profiles and preview cloud-agent setup with a private
runtime containing Access-mode placeholders. The drive asserts the profile ids,
setup action, loopback bind, and `apply: false`. It checks that no instance or
credential was written.

## How to get to it (user POV)

Run `brigade hub connector list --target <temp-target>`, then
`brigade hub connector setup --id cloud-agent --target <temp-target>
--runtime-path <runtime.json>` without `--apply`.

## Driving it with control-brigade

```bash
$C hub-connector --target "$TARGET"
```

Expect `ok: true`, with both `list.returncode` and `preview.returncode` equal
to zero. Each command has its own capture. Use the shared evidence and cleanup
steps in README.md after the drive.

## Gotchas

The setup preview loads a private runtime and requires Linux filesystem guards.
It runs offline, uses no real credentials, and starts no service. Discovery is
portable. The drive covers only list and setup preview.
