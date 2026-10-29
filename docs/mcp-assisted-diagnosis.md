# MCP assisted diagnosis

`brigade bug diagnose` compares a selected project's MCP catalog and ownership
state with one harness configuration. It helps explain configuration differences
for a reported symptom. It does not check whether a server is running or whether
the harness loaded the files.

## Run a diagnosis

```text
brigade bug diagnose --target <repo> --harness codex --summary <symptom> [--name <server>] [--json]
```

For example, use a disposable repository path and symptom text:

```text
brigade bug diagnose --target ./example-repo --harness codex --summary "The project MCP server is missing"
```

The command reads only the selected project's MCP catalog, ownership sidecar,
and selected harness configuration. It does not read another harness or user
configuration. Each of the three files is limited to 64 KiB, and diagnosis is
limited to 100 servers. The default command writes nothing. `--json` requests
structured diagnosis output.

The comparison describes expected configuration and observed configuration.
A catalog entry missing from the selected Codex project configuration means
there is a configuration difference. A matching entry means only that the
configuration matches. Runtime availability remains unknown in both cases.
Healthy files do not prove that Codex loaded them.
The evidence digest identifies the redacted findings. Formatting and secret-only
changes preserve it when those findings are unchanged. Raw credential-bearing
file contents are never hashed for export.

Examples of distinct states:

- **Missing:** the catalog lists a server, but the selected harness config has
  no corresponding entry. The result can suggest a static `brigade mcp plan`
  preview for the selected harness.
- **Stale:** the catalog changed after the harness projection was written. The
  recorded projection no longer reflects the current catalog.
- **Conflict:** a managed projection has edits that differ from its recorded
  ownership state. Review the configuration before considering a sync.
- **Excluded:** the server is disabled or excluded, so the comparison identifies
  that state instead of treating it as a missing active server.
- **Malformed:** invalid catalog, ownership, or harness data is reported as an
  evidence error. It is not treated as an empty configuration.

Suggested preview commands are static guidance. Diagnosis does not run them,
connect to servers, invoke a model or launch T3, look up network state, inspect
transcripts, rewrite configuration, restart services, repair a difference, or
post an issue. Verification is not run. A configuration difference alone does
not establish a Brigade code defect.

## Export a finding or prepare a report

These optional actions apply to the current comparison and are mutually
exclusive:

```text
brigade bug diagnose --target ./example-repo --harness codex --summary "The project MCP server is missing" --save-finding ./finding.jsonl
brigade bug diagnose --target ./example-repo --harness codex --summary "The project MCP server is missing" --prepare-report
```

`--save-finding NEW_PATH` writes the redacted finding as existing work import
JSONL to a new file. Export does not ingest or promote it. Review it through the
existing import validation and dry-run flow before ingestion, for example:

```text
brigade work import validate ./finding.jsonl
brigade work import ingest ./finding.jsonl --target ./example-repo --dry-run
```

`--prepare-report` creates and previews a local report using the existing bug
report v1 format and its redaction and security checks. It does not submit the
report. Review it locally. Report preview approval and any later public
submission are separate explicit consent steps in `brigade bug report`.
The report uses the selected diagnosis directly. It does not run general
diagnostics or collect recent receipts, global errors or installation state.
Report metadata outside the selected evidence is marked unknown.

Either action performs a fresh comparison. Configuration may have changed since
an earlier preview, and approval for a prior preview does not carry forward to
new evidence or public submission.
