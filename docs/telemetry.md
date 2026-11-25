# Python CLI telemetry

The Python CLI checks for new releases in the background. It collects no
command counters, creates no install ID, and uploads no usage reports.

## Automatic version check

The detached process sends `GET https://check.brigade.tools/v1/version` with no
query parameters or body.

| Mode | User-Agent | Install channel |
| --- | --- | --- |
| Default | `brigadestack/<version> (<OS>)` | `X-Brigade-Channel` is one of `pipx`, `pip`, `uvx`, `npm`, `source`, or `plugin` |
| Privacy opt-out | `brigadestack-update-check` | Omitted |

The channel is a fixed label. `BRIGADE_INSTALL_CHANNEL` accepts only the listed
labels, and the npm launcher sets its own channel. The version request contains
no command arguments, filesystem paths, or repository names.

## Opt-outs

| Setting | Effect on the automatic check |
| --- | --- |
| `DO_NOT_TRACK=1` | Still checks for updates with the fixed User-Agent and no channel header. |
| `BRIGADE_TELEMETRY=0` | Still checks for updates with the fixed User-Agent and no channel header. |
| `BRIGADE_NO_UPDATE_CHECK=1` | Sends no automatic request and suppresses CLI update notices and cached work-brief hints. |
| Nonempty `CI` | Sends no automatic request and suppresses the stderr update notice. A work brief can still show a cached update. |

`DO_NOT_TRACK` also accepts any nonempty value except `0`, `off`, `false`, and
`no`. `BRIGADE_TELEMETRY` also accepts `off` and `false`. Both settings ignore
case and surrounding whitespace. Any nonempty `BRIGADE_NO_UPDATE_CHECK` value
disables the automatic check. An explicit `brigade update` still contacts
release services.

The request has a five-second timeout. The CLI waits 24 hours after a valid
response. A failed check keeps the previous result, and the one-hour refresh
claim limits how soon another attempt can start. npm installations refresh
the cache without displaying an update notice or work-brief hint.

## Storage and retention

Since November 24, 2026, the version-check Worker stores nothing from incoming
requests. It uses the URL path to route the request and ignores the headers.
Cloudflare receives the connecting address as part of serving HTTPS. A weekly
purge deletes historical rows older than 90 days. Those rows could contain a
weekly salted address hash, UTC dates and timestamps, the version, a request
count, and an operator-egress flag. See the [service privacy notice](https://check.brigade.tools/privacy).

The local cache at `<cache_root>/brigade/update-notify.json` holds the latest
version and check and notice timestamps. The adjacent `update-notify.claim`
file uses its modification time to space out refresh launches. Both files can
be deleted and have no automatic expiry.

## Other CLI data flows

In GitHub repositories, `brigade work brief` can make GitHub reads through
`gh` using your login. Fleet Hub and memory hub features send data to their
respective hubs only when you configure them. Fleet Hub checkout presence can
include the node ID, repository identity, branch, checkout path, and dirty
paths.

`brigade daily telemetry` inspects local daily receipts.
`brigade receipts export otel-genai` and
`brigade receipts export openinference` write local JSONL files.
[Receipt telemetry mapping](telemetry-mapping.md) lists the exported fields.
