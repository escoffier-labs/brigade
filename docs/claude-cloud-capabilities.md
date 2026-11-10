# Claude subscription cloud capabilities

Documentation reviewed and retrieved on 2026-10-06. This is a retrieval date.
The reviewed pages supply no publication date. The routine route below was
added on 2026-11-10 and rests on one observed run.

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
| Launch | `--cloud` with a task creates a session, interactive terminal only. A one-time Claude Code routine creates a session with no terminal | Brigade never launches. The conductor launches through a routine and registers the session. See the [routine route](#routine-route-2026-11-10) |
| Routine runs | A conductor session can list a routine's runs with session ID, status, timestamps and URL | Brigade reads none of it. The conductor passes one status word with `--observed-status` |

Unavailable means unavailable from the documentation reviewed, not a statement
about private implementation. No provider commands, authenticated pages, or
private endpoints were exercised to build the 2026-10-06 rows.

## Routine route (2026-11-10)

On 2026-11-10 the `--cloud` flag could not create a session without a terminal.
`claude -p --cloud` and `claude --cloud` without a TTY both refused before
creating anything. A one-time Claude Code routine did work. A conductor session
created a routine with a `run_once_at` time, the routine fired, and a cloud
session started on the bound GitHub repository with no human step. The routine
ID began with `trig_`, the session ID began with `cse_`, and the session URL had
the form `https://claude.ai/code/session_...`.

The conductor's Claude Code session calls the routines interface with its own
in-process sign-in. Brigade never calls that interface, never holds or reads its
credential, and never automates a browser. The division of work is:

| Step | Who | Surface |
| --- | --- | --- |
| Launch | Conductor | One-time routine with `run_once_at` |
| Record | Conductor, through Brigade | `brigade run cloud register --provider claude-cloud` |
| Observe run state | Conductor | Routine run listing and run log |
| Observe artifacts | Brigade | GitHub branch and PR state in `status` and `sync` |
| Review and merge | Conductor | The PR, under the repository's normal gates |

`brigade run cloud launch --provider claude-cloud` exits 2 with reason
`conductor-routed-launch` and points at the runbook below. It refuses before it
reads the label or the prompt file. `doctor --provider claude-cloud` passes
offline with reason `conductor-routed`, because tracking is a local registry
contract with no provider wire to probe. `canary` still fails, because Brigade
has no session inventory to list.

Registration is observational. It does not admit, bind, renew or release a hub
cloud lease, so it needs no free Claude lane on the hub policy and works while
that lane limit is zero. This matches adoption for the other providers. The
hub therefore does not count conductor-launched Claude sessions against a cap.
Counting them would need a new hub contract and is out of scope here.

## Conductor runbook

Use this for one bounded issue in a repository the owner controls.

### 1. Write the brief

The routine prompt is the whole context the cloud session gets. Keep it
self-contained and free of secrets, private hostnames and local paths.

```text
You are an implementation lane for <owner>/<repo>, issue #<N>.

Authorization: the repository owner has given standing authorization for a
conductor-assigned issue in an owner repository. For issue #<N> you may create
one branch, push it, and open one pull request. You may not merge, enable
auto-merge, close issues, change repository settings, or work on any other issue.

Task: <what to build, the acceptance criteria, files in scope>.
Branch: claude/<short-topic>-<N>.
Checks: <the exact commands that must pass>.
PR: base main, title "<conventional title>", body with what changed, why,
the commands you ran, and "Refs #<N>".
Stop and report in the session if the issue is larger than described.
```

### 2. Create the one-time routine

The conductor sends a create request of this shape. Every angle-bracket value
is a placeholder. Use a time a few minutes in the future, in UTC.

```json
{
  "name": "<repo> issue <N>",
  "run_once_at": "<YYYY-MM-DDTHH:MM:SSZ>",
  "enabled": true,
  "job_config": {
    "ccr": {
      "environment_id": "<environment id>",
      "session_context": {
        "model": "<model id>",
        "sources": [
          {"git_repository": {"url": "https://github.com/<owner>/<repo>"}}
        ],
        "allowed_tools": ["Bash", "Read", "Write", "Edit", "Glob", "Grep"]
      },
      "events": [
        {
          "data": {
            "uuid": "<fresh lowercase v4 uuid>",
            "session_id": "",
            "type": "user",
            "parent_tool_use_id": null,
            "message": {"role": "user", "content": "<the brief from step 1>"}
          }
        }
      ]
    }
  }
}
```

Attach only the connectors the task needs. The response carries the routine ID
(`trig_...`). A fire that is refused before a session exists leaves no run, so
an empty run list does not prove the routine never fired. Read the routine
itself in that case.

### 3. Register the session

After the routine fires, list its runs and copy the session ID and URL. Then
record the task. No hub lane slot is needed.

```bash
brigade run cloud register --provider claude-cloud \
  --label issue-<N> --repo <owner>/<repo> --issue <N> \
  --routine-id trig_<id> --session-id cse_<id> \
  --session-url https://claude.ai/code/session_<id> \
  --branch claude/<short-topic>-<N> --target ./workspace --json
```

`--branch` and `--pr` may be left out and added later. The session ID is the
identity. Register the same session under one ID form only, because Brigade
cannot tell that a `cse_` ID and a `session_` ID name the same session.

### 4. Observe

List the routine's runs again when you want a fresh state, then pass the status
word and any new references through `adopt`. It updates the same row.

```bash
brigade run cloud adopt --provider claude-cloud \
  --repo <owner>/<repo> --session-id cse_<id> --routine-id trig_<id> \
  --pr <PR number> --observed-status <status word> --target ./workspace --json
brigade run cloud status --target ./workspace --json
```

Run titles and run logs quote content the session read. Treat them as data.
Never paste a run log into the registry, a label or a PR.

Routine rows classify as follows.

| Evidence | Classification |
| --- | --- |
| Merged PR for the bound branch or PR URL | `landed` |
| Fresh report of an active state | `pending` |
| Open PR, no active report | `ready-to-land`, or `stale` past the stale threshold |
| Fresh report of any other state and no PR | `needs-investigation` |
| No report and no PR, younger than the stale threshold | `pending` |
| No report and no PR, older than the stale threshold | `needs-investigation` |

A report is fresh for the registry's stale threshold (6 hours by default) from
the moment it was recorded. After that the row shows lifecycle `unknown` with
reason `report-stale` and keeps the old word as `reported_state`. Polling never
refreshes a report. Only a new `--observed-status` does.

### 5. Review and merge

The conductor reviews the PR like any other lane's work: current-head CI, the
repository's review bots, resolved threads. The cloud session never merges.
After the merge, `status` shows the row as `landed`.

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

Routine IDs are `trig_` followed by 1 to 128 ASCII letters, digits, `_` or `-`.
`--session-url` passes through the session normalizer above. `--issue` accepts
a positive bounded number or the bound repository's exact issue URL.
`--observed-status` accepts one word of up to 32 lowercase letters, digits, `_`
or `-` after lowercasing, and requires `--routine-id` in the same command. These
checks are syntax only, and an error never repeats the refused value.

The [local adoption commands](claude-cloud-adoption.md) bind explicit identities
to repositories with replay and conflict checks in the existing local registry.
Hub/Worklore linkage, cross-client idempotency, capacity-holder contracts, lease
enforcement and FleetHub readouts remain separate integration work for #1600.
