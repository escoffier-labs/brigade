---
status: paused
proof: drive
surfaces: run cloud grokbot pack setup, run cloud grokbot pack doctor, run cloud grokbot pack canary, run cloud grokbot pack remove
drive: grokbot-pack-setup, grokbot-pack-doctor, grokbot-pack-canary, grokbot-pack-remove
last-drive: 2026-11-03 31d5c549d
reason: pack doctor reads the job queue and the host-wide feed authority through the Fleet Hub when the host has a hub URL, so its verdict there is a hub answer; setup, canary, and remove are local
unpause: pending: a decision on driving Grok Bot verbs against a hub-free home or a pinned Fleet Hub contract
---

# Grok Bot connector pack lifecycle

> **Paused.** `pack doctor` reads the job queue and the host-wide feed
> authority through the Fleet Hub whenever the host has a hub URL configured,
> so on such a host its verdict is the hub's answer. `setup`, `canary`, and
> `remove` stay local and can still be driven by hand. The header says what unpauses it. The observations
> below were last checked on main `31d5c549d`; do not cite them as current
> proof until the status is `active` again.

First-party connector packs are the role-scoped MCP listeners Grok Bot talks
to. `implementation-worker` is the pack an Implementation Worker runs behind.
The lifecycle is: see the catalog, write non-secret instance config, diagnose
it, canary it, remove it. No step here needs a real credential.

## Sub-features

- `pack list` / `pack show --id` - the packaged catalog: id, kind, version,
  default bind, tool names.
- `pack setup --id --bearer-env NAME | --bearer-file PATH` - previews, then
  with `--apply` writes `.brigade/grokbot/packs/<id>.json` and
  `.brigade/grokbot/<id>.json`. Optional `--bind`, `--allow-host`,
  `--allow-origin`, and pack-specific paths (`--runtime-path`, `--ledger-path`,
  `--action-state-path`, `--approval-dir`, ...). The bearer is stored as a
  *reference*, never a value. The `dot` pack takes neither bearer flag because
  it verifies Cloudflare Access tokens at the origin. It stores `runtime_path`.
- `pack doctor --id` - sanitized checks: `dependency`, `config`, `permissions`,
  `queue`, `host-wide-feed-authority`, `endpoint`. Details never carry credentials or job
  content. The host-wide feed check reports `unavailable` for hub outages and
  `fail` for authorization refusals. Both produce a nonzero doctor exit.
- `pack canary --id` - bounded non-mutating authentication and inventory check
  against the listener.
- `pack remove --id` - previews the owned config and unit files, deletes them
  with `--apply`.
- `pack update`, `pack install-service`, and the `relay-*` family, not yet
  driven by the helper.

## How to get to it (user POV)

An operator standing up an Implementation Worker seat runs:

```bash
brigade run cloud grokbot pack list --json
brigade run cloud grokbot pack setup --target . --id implementation-worker \
  --bearer-env GROKBOT_IMPL_BEARER            # preview
brigade run cloud grokbot pack setup --target . --id implementation-worker \
  --bearer-env GROKBOT_IMPL_BEARER --apply     # write
brigade run cloud grokbot pack doctor --target . --id implementation-worker --json
brigade run cloud grokbot pack canary --target . --id implementation-worker --json
```

They then start the listener and re-run doctor and canary until both are clean.

## Driving it with control-brigade

```bash
C=registry/skills/verify-brigade/control-brigade.py
P="--target $TARGET --id implementation-worker"

$C grokbot-pack-setup  $P                # preview: writes nothing
$C grokbot-pack-setup  $P --apply        # writes config
$C grokbot-pack-doctor $P
$C grokbot-pack-canary $P
$C grokbot-pack-remove $P                # preview: deletes nothing
$C grokbot-pack-remove $P --apply --dry-run   # --dry-run wins over --apply
```

Observed end state on a fresh target, with no listener running:

- `setup` preview -> exit 0, `"applied": false`,
  `"writes": [".brigade/grokbot/packs/implementation-worker.json", ".brigade/grokbot/implementation-worker.json"]`,
  and neither file on disk afterwards.
- `setup --apply` -> exit 0, `"applied": true`, and both files present under
  `$TARGET/.brigade/grokbot/`. Check the files; the preview list is a promise,
  the files are the proof.
- `doctor` on main `31d5c549d`, on a host with a hub URL configured and
  without the `grokbot` extra -> helper exit 3 (the CLI exits 1), with
  `"config_resolved": true`, `"listener_running": false`,
  `"failed_checks": ["dependency", "queue"]`, and checks
  `dependency=fail, config=ok, permissions=ok, queue=fail,
  host-wide-feed-authority=skipped, endpoint=fail, bind-drift=ok`.
  This page used to record helper exit 0 for the same checks. The helper
  sinks the drive on any failing check other than `endpoint`, so exit 3 is the
  current answer. `bind-drift` is a check added since the last record.
- The same `doctor` call run straight against the CLI with a throwaway `HOME`
  (no fleet config, so no hub URL) reported `queue=ok` and no
  `host-wide-feed-authority` row at all. That run bypassed the helper, so it is
  a hand check, not a recorded drive. It shows which rows depend on the host.
- `canary` -> exit 0 from the helper (the CLI exits 1), `"reason": "health"`,
  `"listener_running": false`, `"expected_without_listener": true`.
- `remove` preview -> exit 0, `"applied": false`, `"paths"` listing the two
  files, both still on disk.

**`endpoint: fail` and `reason: health` are the proof, not the failure.** The
commands resolved the pack config, dialed the loopback bind the pack declares
(read it from the `bind` field in the setup output), and reported honestly that
nothing answered. A pack command that returned "ok" with no listener up would
be the bug.

## Gotchas

- `pack setup` requires exactly one of `--bearer-file` / `--bearer-env`, except the `dot` pack, which takes neither because it verifies Cloudflare Access tokens at the origin.
  The CLI rejects missing bearer flags for other packs before setup runs.
- For packs using a bearer, `pack doctor`'s `config` check *resolves* the bearer reference, so it fails
  when the named env var is unset - which looks like a config bug and is not.
  The helper sets a non-secret placeholder in `VERIFY_BRIGADE_PACK_BEARER` for
  exactly this reason. Never put a real bearer in a verification run.
- `dependency: fail` means the `mcp` server package is not importable in the
  active interpreter. That is an environment fact about the checkout, not a
  regression in the pack.
- `queue: fail` on a fresh target is expected: no queue state exists yet.
- Doctor output is deliberately sanitized to `{check, status}` pairs. Do not
  wait for a detail string that explains the failure; there is none by design.
- Several packs need more than a bearer (`--runtime-path`, `--ledger-path`,
  `--action-state-path`, `--approval-dir`, and for `cerebro-memory`,
  `--cli-executable` plus `--workdir`). `dot` requires only `--runtime-path`
  and takes neither bearer flag because it verifies Cloudflare Access tokens at the origin. `implementation-worker` needs only the
  bearer reference, which is why it is the default pack to drive.
- Ports are fixed per pack in `grokbot_packs.py`. If a real listener is already
  bound on the host, a canary can reach *that* process. Drive with a `--bind`
  you own, or read the bind in the setup output before trusting a green canary.
