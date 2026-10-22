# GitHub webhook relay runbook

The relay is a separate Cloudflare Worker with its own D1 database. It accepts
only signed events for the exact repositories in `ALLOWED_REPOSITORIES` and
the five event types configured in the Worker. It never calls the Fleet Hub.
Brigade nodes pull normalized events over HTTPS with a read-only bearer token.
Events are purged 14 days after receipt.

The Worker responds 202 after D1 accepts the idempotent insert. If that write
does not finish within eight seconds, it responds 503 so GitHub can retry.

## First-time setup

Wait for explicit operator approval before creating Cloudflare resources or a
GitHub App or webhook. Provision a new Worker and D1 database named for this
relay, apply `workers/github-webhook-relay/schema.sql`, and bind the database as
`DB`. Set `ALLOWED_REPOSITORIES` to a comma-separated list of exact
`owner/repository` names. Leave it empty until the approved source repositories
are known. Do not use wildcard entries.

Choose and configure public ingress before deployment. This repository's
Wrangler file sets `workers_dev = false` and has no `route` or `routes`, so it
does not create a public URL. After the operator approves a hostname and its
Cloudflare zone, add a Wrangler `route` or `routes` entry that covers both
`/webhooks/github` and `/v1/events`. If the operator instead approves a
`workers.dev` endpoint, enable `workers_dev` for this Worker. The selected URL
must be reachable by GitHub and the approved Brigade pull clients. See
[Wrangler route configuration](https://developers.cloudflare.com/workers/wrangler/configuration/).

Generate two independent high-entropy values in the approved secret manager:
one webhook signing secret and one read-only event API bearer. Store them as
Worker secrets. For a local Wrangler operator flow, enter values at the
interactive prompts and do not put secret values in command arguments or
shell history:

```sh
wrangler secret put GITHUB_WEBHOOK_SECRET --config workers/github-webhook-relay/wrangler.toml
wrangler secret put EVENTS_READ_TOKEN --config workers/github-webhook-relay/wrangler.toml
```

Configure the GitHub App or approved webhook sender with the same webhook
signing secret and subscribe only to `workflow_run`, `check_suite`,
`check_run`, `pull_request`, and `status`. The Worker must be reachable at
`/webhooks/github`. Provide `EVENTS_READ_TOKEN` only to the approved Brigade
pull client. Keep `GITHUB_WEBHOOK_SECRET`, `EVENTS_READ_TOKEN`, and any previous
rotation secret out of Wrangler vars, source control, logs, and request bodies.

## Rotation

1. Generate a new signing secret and set it as `GITHUB_WEBHOOK_SECRET_PREVIOUS`
   while leaving the current `GITHUB_WEBHOOK_SECRET` in place.
2. Update the GitHub App or webhook sender to use the new secret.
3. Verify a newly delivered event is accepted and appears in a read-only pull.
4. Promote the new value to `GITHUB_WEBHOOK_SECRET`, then remove
   `GITHUB_WEBHOOK_SECRET_PREVIOUS` after the sender change is confirmed.
5. Rotate `EVENTS_READ_TOKEN` by updating the pull client and Worker secret in
   a coordinated change. The endpoint accepts one read token at a time; old
   readers receive 401 after the change.

Never copy secret values into this runbook, an issue, or a terminal transcript.

## GitHub redelivery and replay

Use GitHub's delivery page to redeliver the original delivery when the sender
reports a timeout, non-202 response, or an incident review calls for replay.
Redelivery preserves `X-GitHub-Delivery`, so a delivery already stored in D1 is
a no-op and still receives 202. If the original insert failed, a successful
redelivery inserts it once. The API cursor is global and monotonically
increasing; clients should persist the last `next_cursor` only after processing
that page, then request `/v1/events?after=<cursor>&limit=<n>` again.

GitHub does not automatically redeliver failures. An operator can manually
redeliver deliveries from the past three days through the GitHub UI or REST
API. See [Handling failed webhook deliveries](https://docs.github.com/en/webhooks/using-webhooks/handling-failed-webhook-deliveries)
and [Redelivering webhooks](https://docs.github.com/en/webhooks/testing-and-troubleshooting-webhooks/redelivering-webhooks).

If a client needs to reprocess already stored events, replay from its prior
cursor locally. Do not delete D1 rows to force duplicate deliveries through.
The relay does not backfill events older than GitHub's available redelivery
window or events already removed by 14-day retention. For that gap, use the
approved GitHub read path to reconcile workflow, check, pull request, or commit
status state, then resume pulls from the current cursor. Never send Fleet Hub
traffic from the relay.

## Recovery

If the Worker is unavailable, GitHub records failed deliveries but does not
retry them automatically. After service returns, an operator must redeliver
available failures from the past three days. Verify that redelivered requests
receive 202 and that the pull client advances its cursor. A 503 from the relay
means storage or configuration is unavailable. Check Worker health, D1
binding, secret presence, and the repository allowlist without printing secret
values or request bodies.

If reads fail, confirm the client bearer is current and read-only, then check
the D1 binding and the scheduled purge execution. A missed purge is safe to
rerun because deleting rows older than the cutoff is idempotent. If data has
expired, follow the reconciliation procedure above. Do not point the relay at
the Fleet Hub, create a tunnel, or widen the event/repository allowlist during
recovery without operator approval.

## Local checks

The unit suite uses only the Node.js built-in test runner and runs offline:

```sh
node --test workers/github-webhook-relay/test/worker.test.js
node --check workers/github-webhook-relay/src/index.js
```
