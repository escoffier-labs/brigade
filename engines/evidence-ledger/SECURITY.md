# Security Policy

## Supported versions

MiseLedger is pre-1.0; fixes land on the latest version. Please upgrade before reporting.

## Reporting a vulnerability

Report privately, not in a public issue:

- GitHub: **Security → Report a vulnerability** (private advisory) on this repo, or
- contact the maintainer privately via [@solomonneas](https://github.com/solomonneas)

MiseLedger builds a local SQLite ledger of your AI work history, so the issues
that matter most are anything that could **exfiltrate or corrupt that ledger**:
an import or export path that writes outside the data directory, a bug that
sends data off the machine, or SQL inspection that escapes its read-only intent.
Include the adapter input or command, with any sensitive values redacted.

## Scope

In scope: the CLI, the importers/exporters, the SQLite schema and migrations,
the read-only `sql` inspection mode, and the `serve` / `mcp` surfaces.

Out of scope: the upstream source systems (StationTrail, SourceHarvest, the
crawlers) - report to their own projects.

## Notes

MiseLedger is local-first and makes no outbound network calls in its core path.
Treat the ledger database itself as sensitive: it holds the content you imported.
