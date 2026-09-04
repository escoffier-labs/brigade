# Governance inventory

`brigade governance inventory` exports a point-in-time record of the AI-related
agents, model configuration, local tools, MCP servers, and bounded run metadata
that Brigade is configured to read in one workspace. It does not discover an
organization, contact providers, or make retention or control-compliance claims.

```bash
brigade governance inventory --target .
brigade governance inventory --target . --since 2026-09-01T00:00:00Z --until 2026-09-04T00:00:00Z
brigade governance inventory --target . --output-dir ./governance-export --cyclonedx
```

Without `--output-dir`, the command writes one JSON object to standard output.
With `--output-dir`, the destination must be empty. Brigade writes artifacts to a
private sibling directory, fsyncs them, calculates their digests, and atomically
publishes the destination.

## Output contract

The combined export uses schema `brigade.governance_inventory.v1`. It is the one
canonical inventory document: the agent, model-provider, tool, and MCP registries
are keys inside `registries`, rather than separately generated registry artifacts.
That avoids a clock or detached-manifest digest disagreement between files. It contains:

- `scope`: a fixed notice that the scope is limited to Brigade's configured
  workspace surface.
- `time_window`: the normalized RFC 3339 `since` and `until` bounds.
- `registries.agents`: configured roster seats using
  `brigade.governance_inventory.agents.v1`.
- `registries.model_providers`: configured model/provider admission facts using
  `brigade.governance_inventory.model_providers.v1`.
- `registries.tools`: configured local tool catalog entries using
  `brigade.governance_inventory.tools.v1`.
- `registries.mcp_servers`: configured canonical MCP entries using
  `brigade.governance_inventory.mcp_servers.v1`.
- `observed_runs`: aggregate seat, model, provider, count, first-seen, and
  last-seen facts from bounded local run metadata, using
  `brigade.governance_inventory.observed_runs.v1`.

All collections are sorted. With identical configuration, bounds, and clock,
the export is byte-identical. Configuration is a point-in-time observation. The
time bounds apply only to observed-run facts, using the half-open interval
`[since, until)`, not configuration. The roster is read only from
`--target/.brigade/roster.toml`; Brigade never substitutes a user-home roster.

Unavailable owner, environment, purpose, lifecycle, privilege, provider-retention, and
provenance fields are represented as `{"value":"unknown","reason":"..."}`.
Brigade does not infer them from a model name or an external provider.

Observed use comes from each bounded `worker-results.json` `results` record;
the companion `run.json` contributes only its timestamp. Seat names are joined
to the configured workspace roster for provider attribution. An unconfigured
seat remains explicitly unknown. When a valid node-local Fleet model-policy
LKG is present, the model-provider registry labels its cached time, revision,
admissions, and denials. It never contacts Fleet to create this projection.

## Privacy and input handling

The export uses allowlisted projections. It does not include environment values,
MCP headers, command arguments, command paths, source paths, URL query strings,
URL fragments, URL userinfo, task text, logs, worker output, approval reasons,
credentials, or account identifiers.

Stdio MCP servers become `local-mcp-server` components. HTTP and SSE MCP servers
become `remote-mcp-service` records with a sanitized scheme and authority only.
Malformed, oversized, symlinked, unsupported, or unsafe inputs produce bounded
registry errors and are omitted from public output.

## CycloneDX and detached artifacts

`--cyclonedx` is opt-in. It adds `cyclonedx.json`, using the official CycloneDX
1.7 JSON schema URI, deterministic serial number and `bom-ref` values, components
for local tools, stdio MCP servers, and configured model/provider entries (as
`machine-learning-model` components), plus services for HTTP/SSE MCP endpoints.
The BOM represents the time window as metadata properties and does not add
`modelCard` data outside machine-learning-model components.

An output directory always includes `inventory.json` and `manifest.json`. The
manifest uses `brigade.governance_inventory.manifest.v1` and lists only relative
artifact names, final byte sizes, and SHA-256 digests. It deliberately excludes
its own digest, so verification has no self-reference. Its `time_window` repeats
the normalized bounds without altering the digest list.
