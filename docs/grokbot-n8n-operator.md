# n8n-operator connector pack

The n8n-operator pack is a first-party Grok Bot connector. It exposes a closed six-tool MCP inventory for bounded n8n reads and four approved mutations. It does not import or shell out to n8n-ops-mcp or n8nctrl.

Default listener bind: `127.0.0.1:8775`.

## Tools

Public tools, and only these:

- `n8n_overview`
- `n8n_workflow_status`
- `n8n_execution_bundle`
- `n8n_propose_action`
- `n8n_action_status`
- `n8n_execute_action`

Read tools return small projections only. They do not return workflow definitions, credentials, environment values, API keys, arbitrary execution data, or raw upstream errors.

## Actions

Initial catalog:

- `deactivate-workflow` posts `/api/v1/workflows/{id}/deactivate`
- `archive-workflow` posts `/api/v1/workflows/{id}/archive`
- `unarchive-workflow` posts `/api/v1/workflows/{id}/unarchive`
- `cancel-execution` posts `/api/v1/executions/{id}/stop`

Activate, trigger, retry, edit, delete, credential writes, execution deletion, and tag writes are not in the catalog.

Proposals expire after 15 minutes and bind the action, target, and a SHA-256 revision of the current bounded target projection. Execute consumes the proposal atomically before the upstream write. Consumption is at-most-once and stays consumed if the HTTP call fails. There are no automatic mutation retries.

## Runtime and approvals

Create a mode-0600 current-UID runtime JSON with this exact schema:

```json
{
  "version": 1,
  "base_url": "https://n8n.example.invalid",
  "api_key_file": "/var/lib/grokbot-n8n/api-key"
}
```

The API key file is a separate mode-0600 current-UID file. Environment variables are not supported for the API key. The client sends it only as `X-N8N-API-KEY`.

Approval files are operator-created under the configured approval directory. The connector never writes them. Exact schema:

```json
{
  "version": 1,
  "proposal_id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "action_id": "deactivate-workflow",
  "target_id": "wf1",
  "target_type": "workflow",
  "revision": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "approved_at": "2026-08-30T00:01:00Z"
}
```

The file must be a regular file, mode `0600`, current-UID owned, and not a symlink.

## Setup

```bash
brigade run cloud grokbot pack setup --id n8n-operator --bearer-file /run/brigade/grokbot-n8n.token --runtime-path /var/lib/grokbot-n8n/runtime.json --action-state-path /var/lib/grokbot-n8n/actions --approval-dir /var/lib/grokbot-n8n/approvals
brigade run cloud grokbot pack setup --id n8n-operator --bearer-file /run/brigade/grokbot-n8n.token --runtime-path /var/lib/grokbot-n8n/runtime.json --action-state-path /var/lib/grokbot-n8n/actions --approval-dir /var/lib/grokbot-n8n/approvals --apply
brigade run cloud grokbot pack doctor --id n8n-operator
brigade run cloud grokbot pack install-service --id n8n-operator
brigade run cloud grokbot pack canary --id n8n-operator
```

Setup stores path references only. It does not accept a secret CLI value. The generated unit may write only the action-state directory. Runtime, approval, and API-key files stay read-only.

HTTPS base URLs must have no userinfo, query, fragment, or non-root path. Host syntax must be unencoded, and ports must parse in the 1-65535 range. The connector reconstructs the URL from the validated scheme, host, and port. Plain HTTP is allowed only for loopback hosts.

The API key file must be a distinct inode from the runtime file and must not be a hard link. Path containment is checked before the key is read.

Windows is fail-closed. Brigade's Windows handle adapter can refuse reparse points, but it cannot prove owner-only write or privacy equivalent to POSIX `0700`/`0600`. The connector therefore rejects Windows with `n8n environment is invalid` instead of pretending permission parity.
