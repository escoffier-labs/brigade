# Grok Bot job queue and feed

The private local job queue that Grok Bot workers lease from. Work only enters
it through an explicitly approved manifest - the feed never invents jobs - and
the queue is read back through sanitized projections.

## Sub-features

- `run cloud grokbot feed --manifest <path>` validates an approved manifest;
  `--apply` enqueues, `--limit 1..10` bounds how many new jobs a single call
  may create. Validation is the default.
- `run cloud grokbot scout-feed` - the Repository Scout variant, under an
  active-scout and daily limit.
- `run cloud grokbot enqueue --file` - one envelope from JSON.
- `run cloud grokbot status --json` - safe projections of queued, leased, and
  completed jobs.
- Lease/lifecycle verbs (`lease`, `heartbeat`, `complete`, `cancel`,
  `reconcile`, `findings`, `relay`) and `serve` for the listener.
- Manifest shape (`brigade.grokbot.feed.v1`): exactly
  `{schema, approved, label, entries}`, each entry exactly
  `{idempotency_key, spec}`, each spec exactly the nine keys `label`, `role`,
  `repository`, `base_ref`, `ownership_paths`, `instructions`,
  `verification_commands`, `artifact`, `timeout_seconds`.

## How to get to it (user POV)

An operator writes a private manifest of approved work, validates it, then
enqueues:

```bash
brigade run cloud grokbot feed --target . --manifest ~/private/feed.json --limit 1 --json
brigade run cloud grokbot feed --target . --manifest ~/private/feed.json --limit 1 --apply --json
brigade run cloud grokbot status --target . --json
```

A worker seat then leases from the queue; the operator watches `status`.

## Driving it with control-brigade

```bash
C=registry/skills/verify-brigade/control-brigade.py

# writes a valid sample manifest (mode 0600) into the state root and validates it
$C grokbot-feed --target "$TARGET" --sample

# or point at a manifest you already own
$C grokbot-feed --target "$TARGET" --manifest /path/to/feed.json --limit 1

$C grokbot-status --target "$TARGET"
```

Observed end state on a fresh target:

- `grokbot-feed --sample` -> exit 0, `{"valid": 1, "known": 0, "limit": 1}`.
  `valid` counts entries that parsed and passed spec validation; `known` counts
  entries already present under the same idempotency key. Because the helper
  never passes `--apply`, `$TARGET/.brigade/` gains no queue state - diff the
  directory before and after to confirm.
- `grokbot-status` -> helper exit 3, `{"ok": false, "reason": "auth-failed"}`,
  because a fresh target has no hub authority. That is the honest reading: the
  command parsed, resolved the target, and refused to project a queue it cannot
  authenticate to. A green `status` here would mean the helper picked up
  someone else's credentials.

To prove a validation failure is caught, break one field. `"ownership_paths":
["docs/"]` (trailing slash) returns exit 2 and `error: invalid-ownership-paths`.

## Gotchas

- The manifest is read through an ownership and permission check before it is
  parsed. It must be a regular file, owned by the calling uid, not group- or
  world-writable, and not a symlink; otherwise every call returns
  `unsafe-manifest` regardless of content. The helper's `--sample` writes mode
  0600 for this reason.
- Key sets are compared for exact equality, not superset. One extra key in the
  manifest or in a spec is `malformed-manifest` / `unknown-key`, not a warning.
- `ownership_paths` entries must not start with `/`, contain `\`, or have any
  empty / `.` / `..` segment - so a trailing slash is invalid.
- `artifact.kind` is role-bound: `implementation-worker` takes `draft-pr` or
  `branch`; `repository-scout` takes `report`. Nothing else validates.
- Any key whose name looks credential-shaped is rejected outright before
  validation. Do not try to smuggle a token through a spec field.
- `--apply` mutates the private queue. Never pass it during verification; use
  a manifest you own and the default validate-only path.
- `feed` is idempotent by `idempotency_key`, but a reused key with a *different*
  spec is `idempotency-conflict`, not an overwrite. Change the key when you
  change the spec.
