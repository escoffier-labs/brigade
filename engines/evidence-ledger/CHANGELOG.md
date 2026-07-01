# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Releases before this changelog was started are on the [releases page](https://github.com/escoffier-labs/miseledger/releases).

## [Unreleased]

### Added

- `Dockerfile` and `.dockerignore` that build the static, CGO-free binary and run
  `miseledger mcp` over stdio, so the MCP server can be containerized for registries.
- Project governance: `CONTRIBUTING.md`, `SECURITY.md`, `CODE_OF_CONDUCT.md`, and
  issue / pull-request templates.

### Changed

- README now leads with a recorded terminal demo (`docs/assets/miseledger-ledger.svg`,
  reproducible from `miseledger-ledger.cast`): `init`, `import adapter`, `search`,
  and `stats` against a synthetic session.
- README opening now states what / why / how-it-differs in the first three sentences,
  adds a top-of-page Website link, a keyword-rich `What it does` section, a real-output
  proof block, and `Why not something else?` and `What MiseLedger is not` sections.

### Fixed

- MCP stdio server now accepts newline-delimited JSON-RPC (the ratified MCP stdio
  transport used by Claude Desktop, the MCP Inspector, and Glama) in addition to the
  LSP-style `Content-Length` framing. A spec-compliant client previously got a server
  that silently produced no output; the framing is detected from the first message and
  responses match it.
- Commit the synthetic `testdata/exports/*.json` fixtures that an over-broad
  `exports/` `.gitignore` rule had excluded, so `go test ./...` passes on a clean
  checkout. CI and fresh clones were failing `TestCrawlProviderExports` and
  `TestSessionsListAndSearch` on the missing files.
