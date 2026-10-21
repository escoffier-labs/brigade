# Python CLI telemetry

The Python CLI sends a passive version request. It does not collect command
counters, create an install id, or upload usage reports. Tier 1 opt-in usage
reporting from issue #1861 is held pending operator sign-off on the consent
wording. This slice adds no Tier 1 prompt and no telemetry subcommand.

This page covers the Python CLI only. `brigade daily telemetry` inspects local
daily receipts and is unrelated to usage reporting. Other implementations and
notification services have their own data flows.

## Version request fields

A detached child may send `GET https://check.brigade.tools/v1/version` with no
query parameters or body. Its request headers are:

| Header | Value |
| --- | --- |
| `User-Agent` | `brigade-cli/<version> (<OS>)`, unchanged from earlier clients |
| `X-Brigade-Channel` | `pipx`, `pip`, `uvx`, `npm`, `source`, or `plugin` |

The channel header contains a label only. The npm launcher supplies its identity
to the child. `BRIGADE_INSTALL_CHANNEL` can explicitly select a channel, including
`plugin` or `uvx`. Other values are ignored. Without an override, the CLI detects
pipx from `pipx_metadata.json` in its Python environment, including custom pipx
homes. The `uv` marker in `pyvenv.cfg` identifies uv-created environments as
`uvx`, including `uvx --from brigade-cli brigade ...`. Other installed packages
are `pip`, and source trees are `source`. No environment path or arbitrary
environment value is transmitted. The currently deployed Worker ignores the
channel header. The channel will be recorded only after the separate Worker
update ships. Sending `X-Brigade-Channel: npm` does not yet produce a server-side
`channel=npm` observation.

For requests with the exact Brigade User-Agent, the Worker records ISO week,
UTC day, version, first and last timestamps, request count, an `internal`
classification for configured operator egress, and a salted SHA-256 hash of the
connecting address and ISO week. The hash changes weekly. It is not an install
id and cannot join sources across weeks. The Worker does not store the OS
carried in the header. The HTTPS service receives the connecting address, but
the observation table does not store the raw address.

No command paths, arguments, working directory, repository names, usernames,
hostnames, account identifiers, or error text are included. Weekly observations
are not a count of people or installations.

## Retention and responsibility

A weekly server purge deletes version observations older than 90 days. The
local version cache at `<cache_root>/brigade/update-notify.json` stores the latest
version and check/notice timestamps. It has no automatic expiry and can be
deleted at any time. There is no local payload history or usage receipt because
Tier 1 is not enabled.

The controller is Solomon Neas (Escoffier Labs), reachable at the contact in
[SECURITY.md](../SECURITY.md#reporting-a-vulnerability). Use that contact for
privacy questions and erasure requests. Cloudflare processes the HTTPS request
and hosts the Worker and its D1 observation table. There is no persistent
install id to supply with a Tier 0 erasure request.

## Kill switches and scheduling

- `DO_NOT_TRACK=1` or `BRIGADE_TELEMETRY=0` keeps the version fetch but sends
  `User-Agent: Python-urllib` and no channel header. The Worker records no
  observation for that User-Agent. `DO_NOT_TRACK` also accepts other nonempty
  values except `0`, `off`, `false`, and `no`. `BRIGADE_TELEMETRY` also accepts
  `off` and `false`. Matching is case-insensitive with surrounding spaces removed.
- `BRIGADE_NO_UPDATE_CHECK=1` skips the automatic request and notice entirely.
- A nonempty `CI` skips the automatic request and notice. Tests set both
  `BRIGADE_NO_UPDATE_CHECK=1` and `BRIGADE_TELEMETRY=0`.

The cache refresh is detached, makes one request with a 5 second timeout, and
ignores network failures. The local schedule normally waits 24 hours between
checks. A request can run with piped stderr or after a failed command. npm
installs refresh the cache but suppress the notice and cached work-brief hint.
`update` and `completions` do not trigger automatic refreshes. These switches do
not disable an explicitly requested `brigade update` operation.
