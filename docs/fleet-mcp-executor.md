# Fleet MCP through Executor

Slice 7 of [#1887](https://github.com/escoffier-labs/brigade/issues/1887).
This opt-in recipe mounts the existing [Fleet MCP station](runbooks/fleet-hub-proxmox.md#fleet-mcp-station)
in an Executor app. Direct MCP over stdio or loopback HTTP remains available.
Executor is not the default route. No Executor installation or operator
configuration is changed by Brigade.

## Versions and verification status

Recipe target: **Executor 2.0.0-beta.26**, the version named in its
[CLI documentation](https://executor.sh/docs/run/cli) retrieved on 2026-10-22.
The source does not state a publication date. The issue's earlier research
named beta.24/beta.25. This recipe has **not** been run against Executor.
The app source below passed strict TypeScript checking against the published
`apps@0.0.1-beta.90` package and `@modelcontextprotocol/sdk@1.32.1`.
Use the exact framework version reported by the target host's
`executor apps framework`, and record it with the Executor version during manual
verification. Beta host/framework compatibility remains unverified.

Brigade's station accepts MCP `2024-11-05`, `2025-03-26`, `2025-06-18`, and
`2026-07-28`. Unknown initialization versions receive `2025-06-18`.
The last version requires routing headers, including `Mcp-Method` and, for tool
calls, `Mcp-Name`. Prefer `2025-06-18` for the initial compatibility check and
record the version actually negotiated by Executor. That negotiation through
Executor is unverified. Events and desktop panels require separate capability
checks, as described in [Fleet MCP Events](fleet-mcp-events.md).

Automated tests in `tests/test_fleet_mcp_executor.py` exercise the real HTTP
station and a temporary local hub database. They pin annotations, online and
offline replay, grant-filtered discovery, denied reads and writes, credential
identity, explicit ack, peer framing, message IDs, cursors and structured errors.
These are station transport tests. Executor import, approval and output
preservation remain manual checks below.

## Principal and transport setup

Use a dedicated agent principal, such as `agent:executor`, with exact recipient
grants for the intended peer nodes or agents. Avoid `*`, `node:*`, `agent:*`,
admin credentials and broadly privileged node credentials. Enrollment and grant
management use the existing [principal and grants workflow](runbooks/fleet-hub-proxmox.md#external-agent-principals).
For example, `agent:example-caller` can have just one exact recipient grant:

```json
{ "may_send_to": ["agent:example-peer"] }
```

The test recipe must use a temporary hub and temporary credentials. Never mint
credentials on a real hub merely to validate this document.

Start a station with the dedicated credential in a private, owned token file:

```sh
brigade fleet mcp --http --port 8933 --principal-token-file ./executor-principal.token
```

Before starting it, supply `BRIGADE_MCP_HTTP_TOKEN` from a protected store as a
separate random transport bearer of at least 32 characters. The principal file
contains the **hub credential**. The Executor account below contains only the
**station transport bearer**. Neither goes in source, tool inputs or MCP URLs.
The station refuses admin credentials, and tool inputs cannot choose a sender,
principal, node credential or token file.

Executor and the station must share a loopback network namespace. Hosted Executor
cannot reach another machine's loopback. Container loopback may also refer to a
different namespace. Keep the station's loopback bind and Host/Origin/bearer
guards. This recipe does not add a tunnel or public exposure. Whether a specific
Executor runtime permits this loopback connection is unverified.

## App source and explicit policy

Create an app source directory with `index.ts` below and a `package.json`.
Follow [Author an app](https://executor.sh/docs/author-an-app) to create and deploy
it only in the intended test instance. Pin `apps` to the version the host reports,
and declare the optional `@modelcontextprotocol/sdk` peer required by `apps/mcp`.
The published beta.90 SDK documents `@modelcontextprotocol/sdk` version `1.32.1`.
Brigade retains zero runtime dependencies.

```ts
import {
  bearer, defineApp, defineProvider, object, router, secrets, string, withApprovals,
} from "apps";
import { mcpRouter } from "apps/mcp";
import { always, never } from "apps/operations/approval";

const stationUrl = "http://127.0.0.1:8933/mcp";
const fleetStation = defineProvider({
  name: "Fleet station transport",
  hosts: ["127.0.0.1:8933"],
  auth: {
    bearer: secrets({
      label: "Station transport bearer",
      fields: object({ token: string({ minLength: 32 }) }),
      request: ({ token }) => bearer(token),
    }),
  },
});

const reads = new Set(["fleet_who", "fleet_inbox", "fleet_sent", "fleet_claims"]);
const writes = new Set(["fleet_send", "fleet_reply", "fleet_ack"]);

export default defineApp(
  { user: { accounts: { fleetStation } } },
  async ({ user, signal }) => {
    if (!user) return { tools: router({}) };
    const account = user.accounts.fleetStation;
    const upstream = await mcpRouter({
      url: stationUrl,
      account,
      headers: { Authorization: "Bearer " + account.fields.token },
      signal,
      timeoutMs: 45_000,
    });
    return {
      tools: withApprovals(upstream, (_tool, name) => {
        if (name === "fleet_inbox") return ({ toolInput }) => {
          if (typeof toolInput !== "object" || toolInput === null) return "denied";
          const waitSecs = "wait_secs" in toolInput ? toolInput.wait_secs : 0;
          return typeof waitSecs === "number" && Number.isInteger(waitSecs)
            && waitSecs >= 0 && waitSecs <= 10 ? "approved" : "denied";
        };
        if (reads.has(name)) return never();
        if (writes.has(name)) return always();
        return () => "denied";
      }),
    };
  },
);
```

Link a caller-owned account for this provider containing only the station
transport bearer. Its `hosts` restriction includes the exact port, and its
placement restricts substitution to the Authorization header. Give each
different principal a separate station, port and transport bearer. Changing an
account does not change the hub principal bound to an existing station.

| Fleet tools | `readOnlyHint` | `destructiveHint` | App policy |
|---|---|---|---|
| `fleet_who`, `fleet_inbox`, `fleet_sent`, `fleet_claims` | true | false | Run, inbox waits at most 10 seconds |
| `fleet_send`, `fleet_reply` | false | false | Require approval |
| `fleet_ack` | false | true | Require approval |

Send and reply append messages and may append to a durable offline spool.
Ack updates an existing message's unread state and removes it from default inbox
results, so it is marked destructive even though the message is retained.
No `idempotentHint` is advertised for sends or replies because the replay key
is optional. Annotations describe behavior and never replace hub authorization.

Executor's [generated MCP policy](https://executor.sh/docs/concepts/tools-and-approvals)
asks for approval based on `destructiveHint`. It would not gate additive sends.
The explicit policy above requires approval for all three writes and denies new,
unknown tool names. Keep that policy when updating the import.

The installed `apps@0.0.1-beta.90` declaration of `ApprovalContext<Input>` in
`apps/types/apps/src/contracts/approval.d.ts` exposes decoded call input as
`toolInput`. The [Approvals section](https://executor.sh/docs/concepts/tools-and-approvals#approvals)
also documents input-aware policies (retrieved 2026-10-22, publication date not
stated). The inbox policy uses that input to deny waits above 10 seconds, rather
than relying on callers to follow the suggested wait. The 45-second router
timeout leaves slack above that bound. Recheck the declaration when upgrading.

## Browser approval boundary

Connect the agent only through a scoped OAuth connection approved with
`elicitation_mode=browser`, for example
`https://executor.example.com/mcp?connection=CONNECTION_ID&elicitation_mode=browser`.
Select only this app and its intended account and tools. Configure browser
sign-in and consent using Executor's [MCP connection documentation](https://executor.sh/docs/mcp).
Executor's [Elicitation modes section](https://executor.sh/docs/mcp#elicitation-modes)
states that the issued grant binds the mode and query edits cannot change it
(retrieved 2026-10-22, publication date not stated). Manual step 3 must confirm
that behavior in the deployed version.

**Executor's default `model` mode lets the calling agent approve its own call
through `resume`. This recipe must not use it for gated tools.** In browser mode,
the agent may collect the signed-in person's answer with `resume({ requestId })`,
as documented in [Elicitation modes](https://executor.sh/docs/mcp#elicitation-modes)
(retrieved 2026-10-22, publication date not stated).
It must not be able to supply the approval decision itself. Native-mode client
confirmation also does not meet this recipe's browser requirement.

The approval boundary is Executor's deployed app policy plus its browser-bound
OAuth grant and signed-in approval page. Brigade's HTTP bearer authenticates
transport access, and the hub enforces grants and message ownership. Neither
Brigade boundary enforces Executor approval. Do not give the caller a station
bearer, hub credential, Executor personal access token, local server key, app edit
access or permission to authorize a fresh connection in another mode. Executor's
[Scoped connections section](https://executor.sh/docs/mcp#scoped-connections)
states that personal tokens and local server keys retain full access outside
scoped connections (retrieved 2026-10-22, publication date not stated).
A user consenting to a new model-mode grant would create a
different approval boundary. This recipe cannot prevent such consent in app code.

Browser approval, denial, forged `resume` answers, reconnect mode changes and
credential bypass checks are **unverified** until the manual tests below pass.
Do not enable approval-dependent writes where those checks cannot be completed.

## Waits, cancellation, retries and unavailable hubs

Use `fleet_inbox` with `wait_secs: 10` and a small `limit` for this recipe.
Its station maximum is 120 seconds, but the app policy above denies values over
10 seconds. Executor's [Codemode section](https://executor.sh/docs/mcp#codemode)
documents a five-minute active-time cap, 100-call cap and 100,000-byte output cap,
excluding approval and input waits from the time cap (retrieved 2026-10-22,
publication date not stated). A client may have a shorter timeout. Budget every
call in a multi-step program, keep each wait well under five minutes, and use one inbox
wait per execution. The imported router above allows 45 seconds per request.

The station reads once, then uses hub wait chunks of at most 20 seconds with
network timeouts bounded by five seconds. Older or capacity-limited hubs fall
back to delays of 1, 2, 4, then at most 5 seconds. A network read may finish after
the requested wait deadline. An initial failed read returns a sanitized error.
A later poll failure can return the last good page, so an empty page is not
evidence of current connectivity. Directory reads and ack require the hub.

Cancellation has a known limit: the shared stateless station ignores MCP
cancellation notifications and does not propagate an HTTP disconnect to
`poll_inbox`. The loop can open successive hub waits of up to 20 seconds until
the full `wait_secs` deadline, up to 120 seconds, even after the caller abandons
it. Ending one chunk does not end the abandoned call. Two such calls can keep
the per-principal cap of 2 hub waiters occupied for their full waits unless mail
arrives or a hub failure ends polling. The app's 10-second policy bounds this
recipe's waits, but direct station callers can still request 120 seconds.
There is no cancellation rollback for an accepted send or ack. End-to-end
cancellation and immediate capacity release are deferred to
[#2012](https://github.com/escoffier-labs/brigade/issues/2012). Check capacity
release before treating the cancellation acceptance item as complete.

Generate a fresh `client_id` for each intended send or reply, persist it before
calling, and reuse it with identical arguments after a timeout or reconnect.
Keys match `[A-Za-z0-9_-]{1,64}`. The hub keys
deduplication by authenticated sender and `client_id`, returns the original
message ID, and rejects changed payloads with HTTP 409. Keys for send and reply
share that sender's namespace. Deduplication lasts while the message is retained
(currently 90 days). A new key means a new message.

An unavailable hub can produce `delivery: "spooled"` with a `client_id` and no
delivered message ID. Retrying the same queued key returns `spooled`
without appending another entry or draining it. Recover with the credential's
existing flush workflow, for example
`brigade fleet msg flush --token-file ./executor-principal.token`, then replay
the same key to recover the delivered ID. Revoked credentials and permanent
permission errors fail without delivery. Queued data never
changes the credential's grants.

For transient failures, use at most three attempts with delays such as 1 and
2 seconds, within the outer deadline. Executor's
[Codemode section](https://executor.sh/docs/mcp#codemode) states that the
interpreter has no timers (retrieved 2026-10-22, publication date not stated), so
schedule retries in the caller between executions. Never spin in `execute`.
Respect `permanent`/`transient` errors and each entry of ack's `results` array.
`paging_cursor_expired` from `fleet_who` or `fleet_claims` is permanent: the
cursor expired, was evicted, or is not valid for this caller, so retrying it
cannot succeed. Restart the walk without a cursor and do not merge rows across
the two walks. The error body carries a `hint` saying so. A cursor is bound to
the query that created it, so every continuation must repeat the same `filter`
and `all` (only `all` for `fleet_claims`). Dropping or changing either one also
returns `paging_cursor_expired`.
Inspect MCP `isError` and the JSON text content, even if Executor's outer call
succeeds. Preserve fences, scan findings, IDs and decimal-string cursors.
Reading or returning a message never acknowledges it.

Executor's [What is coming later section](https://executor.sh/docs/mcp#what-is-coming-later)
states that a server restart loses paused programs and warns against automatic
reruns because earlier calls may have taken effect (retrieved 2026-10-22,
publication date not stated). Reconcile individual calls by their persisted keys
and IDs.

## Manual Executor acceptance and comparison

Run these only against a temporary hub and dedicated temporary principal.
Record Executor version, framework version, client name/version, negotiated MCP
version, app revision, scoped connection's tool/account selection and browser
mode. Record no credentials. All checks in this section are currently unverified.

1. Discover the eight upstream tools with Executor `search` and `describe`.
   Verify the read annotations survive import and inspect the explicit policy.
   List presence and claims, confirm only granted nodes appear, and confirm
   denied recipients and another principal's messages remain inaccessible.
2. Send with a persisted key. Before browser approval, assert no hub message and
   no spool entry exists. Try supplying an approval through `resume`, including
   false success claims. Only the signed-in page may decide. Deny once, confirm
   no side effect, then approve a fresh operation and receive it as the peer.
   Repeat the approval and denial checks for reply and ack.
3. Reconnect the issued browser grant using `model`, `native` and no mode query.
   It must still need browser approval. Test both `call` and `execute`. Confirm
   the client has no broader gateway credential and cannot obtain a different
   mode without fresh human consent. Stop if any path self-approves.
4. Replay the same send and reply after dropping their response or reconnecting.
   Confirm original IDs, one stored message per key, and a conflict on changed
   content. Test an unavailable temporary hub, duplicate-free spool/flush, a
   revoked principal and bounded caller retries.
5. Round-trip peer instructions inside fences, message IDs, `next_cursor`,
   `isError` and structured error fields. Confirm reads leave mail unacked,
   and ack changes only the intended principal's messages. Check partial ack
   failures individually.
6. Time an empty 10-second inbox wait, then cancel another wait and observe the
   hub's waiter count return to baseline. Record the delay. The current station
   cannot promise immediate cancellation propagation. Test Executor/client
   deadlines and server restart recovery without replaying a whole program.
   Confirm the app denies `wait_secs: 11` and `wait_secs: 120` before the station
   is called, while omitted, zero and 10-second waits remain allowed.
7. Compare direct HTTP MCP and Executor on the same presence, send, receive,
   reply, receive, ack and sent-status workflow, using separate fresh message
   keys and equivalent approvals. Record correctness, outer MCP calls, upstream
   fleet calls, wall-clock latency including and excluding human approval, and
   measured input/output tokens from the same client/model. Do not estimate
   token savings from payload size. Keep direct MCP whichever route wins.

No benchmark numbers or Executor end-to-end success are claimed. Event push,
desktop panels, browser approval and immediate cancellation release need their
own evidence before recommending a broader deployment.
