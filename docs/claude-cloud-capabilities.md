# Claude subscription cloud capabilities

Documentation reviewed and retrieved on 2026-10-06. This is a retrieval date.
The reviewed pages supply no publication date.

## Supported-capability matrix

The [cloud documentation](https://code.claude.com/docs/en/claude-code-on-the-web)
and [CLI reference](https://code.claude.com/docs/en/cli-reference) support this
bounded matrix for subscription Claude Code sessions:

| Capability | Documented surface | Brigade boundary |
| --- | --- | --- |
| Identity | `session_...`, `cse_...`, session URL | Syntax validation only |
| Listing | Browser sidebar, interactive teleport picker | Machine-readable account-wide listing unavailable in reviewed docs |
| Status | Browser progress | Read-only machine lifecycle/status interface unavailable in reviewed docs |
| Follow-up | `-p` with `--cloud <id-or-url>` sends a message. JSON returns `{ok, session_id, url}` | Mutates the session. Never use as a status probe or guaranteed creation result |
| Artifacts | Browser diffs and PR creation | Machine conversation/diff export unavailable in reviewed docs. Git references need separate binding |
| Lifecycle | Browser archive/delete, environment expiry | Independent events. No documented machine lifecycle feed found |
| Local continuation | Teleport fetches branch and copies conversation locally | Local continuation does not establish cloud completion |
| Launch | `--cloud` with a task creates a session | Automatic launch remains disabled |

Unavailable means unavailable from the documentation reviewed, not a statement
about private implementation. No provider commands, authenticated pages, or
private endpoints were exercised to build this matrix.

## Separate execution and authority

The [CLI reference](https://code.claude.com/docs/en/cli-reference) describes
`claude agents --json --all` as background-session inventory. The
[cloud documentation](https://code.claude.com/docs/en/claude-code-on-the-web)
distinguishes cloud execution from Remote Control of a session on your machine.
Neither surface establishes subscription cloud inventory.

The [Managed Agents overview](https://platform.claude.com/docs/en/managed-agents/overview)
describes API-key access and its own session/event model. The
[Sessions API](https://platform.claude.com/docs/en/api/beta/sessions) belongs to
that product. Applicability to subscription Claude Code sessions is unestablished.
Do not substitute that API or infer product identity from an ID prefix.

Brigade policy keeps provider lifecycle, GitHub artifact state, local continuation,
and Brigade leases separate. A merged PR proves artifact landing only. A missing
branch, archive, expired environment, or expired lease must not fabricate provider
completion. Later integration must retain unavailable lifecycle as unknown or
unobserved, with source, reason, observation time, and last confirmed time. Polling
must not refresh an old provider fact. Adoption must not grant capacity or holder
authority without admission.

## Identity validator contract

`normalize_claude_cloud_identity` returns a frozen `ClaudeCloudIdentity` with a
case-sensitive `session_id` and derived `https://claude.ai/code/<id>` URL.
Bare IDs, matching HTTPS URLs and exact `claude.ai/code/<id>` inputs normalize
equally. Query and fragment are removed.

Brigade accepts `session_` or `cse_` followed by 1 to 128 ASCII letters, digits,
`_`, or `-`. Entire inputs are limited to 4096 printable ASCII characters
without whitespace. These are Brigade validator restrictions, not a provider-wide
schema. URLs require literal authority `claude.ai` and exactly `/code/<id>`.
Credentials, ports, deceptive hosts, nested or encoded paths, control characters,
whitespace, empty suffixes, and malformed values raise a fixed safe `ValueError`
without echoing input. The normalizer performs no I/O and does not verify existence,
account ownership, repository binding, lifecycle, or lease authority.

The [local adoption commands](claude-cloud-adoption.md) bind explicit identities
to repositories with replay and conflict checks in the existing local registry.
Hub/Worklore linkage, cross-client idempotency, capacity-holder contracts, lease
enforcement and FleetHub readouts remain separate integration work for #1600.
