# Grok Bot MCP listener

Install the listener with Brigade. Do not clone or install `grokbot-dispatch-mcp` for a new deployment.

```bash
pipx install "brigade-cli[grokbot]"
```

The package contains the adapter source, but it does not turn the Brigade CLI into a listener. Each role runs a separate listener process with a fixed role. The listener delegates queue operations to the queue below its selected Brigade target. It has no independent queue authority. Keep every target local to the operator that owns that queue.

## Role setup and checks

Run these commands from the local Brigade target. Each example uses a distinct loopback port and a separate protected token file. Create each token file through your secret-management process with permissions limited to the service account. The `setup` command stores only the file reference, never the bearer value.

The `install-service` commands below render a systemd unit to standard output. They do not write a unit because they omit `--out`. Review the rendered unit before installing it through the host's normal service-management process. `canary` makes bounded authenticated requests to the running listener and checks anonymous rejection plus the exact role tool inventory. It does not mutate the queue.

### Operator

```bash
brigade run cloud grokbot setup --target . --instance operator --bind 127.0.0.1:8766 --bearer-file /run/brigade/grokbot-operator.token
brigade run cloud grokbot doctor --target . --instance operator
brigade run cloud grokbot install-service --target . --instance operator
brigade run cloud grokbot serve --target . --instance operator --bind 127.0.0.1:8766 --bearer-file /run/brigade/grokbot-operator.token
brigade run cloud grokbot canary --target . --instance operator
```

The operator listener exposes queue listing, status, cancellation, and expiry. It cannot claim or complete worker jobs.

### Repository scout

```bash
brigade run cloud grokbot setup --target . --instance repository-scout --bind 127.0.0.1:8767 --bearer-file /run/brigade/grokbot-repository-scout.token
brigade run cloud grokbot doctor --target . --instance repository-scout
brigade run cloud grokbot install-service --target . --instance repository-scout
brigade run cloud grokbot serve --target . --instance repository-scout --bind 127.0.0.1:8767 --bearer-file /run/brigade/grokbot-repository-scout.token
brigade run cloud grokbot canary --target . --instance repository-scout
```

The repository-scout listener lists and reads only its role's jobs, then claims, renews, starts, completes, fails, or acknowledges cancellation for those jobs.

### Implementation worker

```bash
brigade run cloud grokbot setup --target . --instance implementation-worker --bind 127.0.0.1:8768 --bearer-file /run/brigade/grokbot-implementation-worker.token
brigade run cloud grokbot doctor --target . --instance implementation-worker
brigade run cloud grokbot install-service --target . --instance implementation-worker
brigade run cloud grokbot serve --target . --instance implementation-worker --bind 127.0.0.1:8768 --bearer-file /run/brigade/grokbot-implementation-worker.token
brigade run cloud grokbot canary --target . --instance implementation-worker
```

The implementation-worker listener has the same worker operations, constrained to implementation-worker jobs.

A successful worker claim returns the bounded validated envelope (label, role, repository, base ref, owned paths, instructions, verification commands, artifact kind, and timeout) as the worker's execution context. List, status, operator, and CLI surfaces stay redacted: they never include the envelope's `instructions` or `verification_commands`.

The routine sequence for a worker is: list, claim, validate role and repository, start, renew before expiry, then complete or fail.

`doctor` reports sanitized dependency, configuration, permissions, queue, and endpoint checks. It returns nonzero after setup until the listener starts because the endpoint check cannot connect. Run it again after the listener starts. A canary needs the listener already running.

## Cloudflare boundary

If remote Grok Bot needs the listener, a Cloudflare Tunnel may publish only that listener endpoint. Put Cloudflare Access in front of the published endpoint. Do not publish the fleet hub or any local queue path, and do not replace Access with an unauthenticated tunnel policy. This guide intentionally does not include account-specific Tunnel or Access configuration.

## Migration from `grokbot-dispatch-mcp`

Migration is an operator-run comparison, not an automatic conversion.

1. Run the old sidecar and the Brigade listener separately, each with its own endpoint and rollback path.
2. Direct one test bot to the Brigade listener while the remaining bots use the old sidecar.
3. Confirm that the test bot receives the matching role-specific tool inventory, then run its passing non-mutating canary.
4. Keep the old sidecar available until the comparison has passed. Remove it only after the operator decides to cut over.

## Current limits

`setup` writes non-secret configuration and bearer references only. The operator still provisions Grok Bot routines and secrets. This work does not commission a Grok Bot and does not prove weekly quota attribution.
