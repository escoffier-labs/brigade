# TOOLS.md - Local Runbook

Commands, ports, services, and runbooks for this workspace. Keep it operational. No secrets.

## Services

```bash
systemctl --user status <service-name>
journalctl --user -u <service-name> --since "-15min"
```

## Ports

Use placeholders in any public copy of this file:

```text
<service-name>  <port>  <purpose>
```

## Common Checks

```bash
git status --short
rg -n '<pattern>' .
jq '.' <file.json>
```

## Memory Handoff Ingest

```bash
solo-mise ingest --target . --dry-run
solo-mise ingest --target .
```

## Publish Guard

```bash
solo-mise scrub --target . --dry-run
git push   # pre-push hook runs content-guard
```

## Notes

- Keep commands current.
- Remove stale ports and endpoints.
- Do not store tokens or passwords here. Use env files, secret stores, or platform credential managers.
