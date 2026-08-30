# Grok Bot Listener Recovery

## Status

Approved for implementation on 2026-08-30 as part of the Grok Bot closure wave.

## Problem

Brigade-owned Grok Bot listener services can remain failed after a transient
systemd resource failure that occurs before `ExecStart`. Calendar timers do not
recover a failed listener, and existing diagnostics only inspect application
configuration and endpoints. Operators therefore cannot distinguish a broken
timer from the listener service's recorded result without a separate manual
check.

## Design

Every first-party Grok Bot listener unit rendered by Brigade will use the same
bounded recovery policy:

- `Restart=on-failure`
- `RestartSec=60s`
- `RestartPreventExitStatus=2`
- `StartLimitIntervalSec=15min`
- `StartLimitBurst=3`

The start-limit directives belong to the unit section, while restart timing and
the non-restarting exit status belong to the service section. A shared renderer
fragment will keep queue-role and connector-pack units aligned. Exit status 2
remains the intentional terminal signal for configuration or usage failures, so
those failures do not enter a restart loop.

`brigade run cloud grokbot pack doctor --id <pack> --service-result` will add one
sanitized `service-result` check. The check will invoke `systemctl --user show`
for the exact Brigade-owned service unit and read only its `Result` property. It
will never inspect a timer unit, expose the raw property value, or run unless the
operator supplies the flag. A successful `Result=success` yields `ok`; missing,
failed, timed-out, or malformed responses yield `fail` through the existing
doctor status contract.

## Alternatives considered

1. Duplicate the recovery directives in every renderer. This is mechanically
   small but lets pack units drift again.
2. Install a systemd drop-in. This avoids renderer edits but creates a second
   lifecycle artifact and makes preview/remove behavior harder to reason about.
3. Use the shared renderer fragment described above. This keeps one install
   artifact per listener and one recovery policy across all first-party packs.

Option 3 is selected.

## Boundaries

- No legacy sidecar units are changed.
- No services are started, restarted, enabled, or reloaded by this feature.
- Doctor remains non-mutating and opt-in for systemd inspection.
- No service output, credentials, paths, timer state, or private configuration
  are emitted by the new check.

## Verification

Tests must fail before implementation for both the rendered recovery directives
and the opt-in doctor behavior. Coverage includes exact fixed `systemctl` argv,
success, failure, timeout, no-call without the flag, exit status 2 preservation,
and all first-party listener renderers. Focused and full Brigade verification
must pass before the pull request is opened.
