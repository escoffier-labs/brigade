# Security Policy

## Supported versions

GraphTrail is early-stage (0.x). Only the latest release on the `master` branch receives security fixes. Pin to a released tag or commit if you need a known-good version.

## Reporting a vulnerability

Please **do not** open a public GitHub issue for security problems. Email **me@solomonneas.dev** with: <!-- content-guard: allow pii/email -->

- A short description of the issue.
- Steps to reproduce (or a minimal proof of concept).
- The version or commit you tested against.
- Whether you would like to be credited in the release notes.

You should get an acknowledgment within 72 hours. If you do not, please follow up - the mail may have been filtered.

## In scope

- Code execution, path traversal, or symlink-attack flaws in `graphtrail init`, `sync`, or the extractors that walk and read a repository tree.
- MCP query execution that writes without `refresh: true`, or opens a query database for writing. Query connections must always use `SQLITE_OPEN_READ_ONLY`.
- A crafted source file that causes the tree-sitter extractors to crash, hang, or read outside the indexed repository.
- A `repo`/`db` argument that lets an MCP caller read a database outside the paths it was meant to reach in a way that constitutes a real escalation.

## Out of scope

- Bugs in `content-guard` itself - please report those upstream at
  <https://github.com/escoffier-labs/content-guard>.
- Bugs in tree-sitter, rusqlite, or other third-party crates - report those to their respective projects.
- The optional `codesearch` feature making a network call to the Code Search URL you configured, or the optional `miseledger` feature reading the MiseLedger database you pointed it at. Those are opt-in by design and disabled in the default build.
- Expected graph-index writes from `refresh: true` are not vulnerabilities. Supported query tools use this opt-in incremental sync before opening a read-only query. The server waits up to 10 seconds, then proceeds with a `refresh_error` note on failure or timeout. A timed-out worker may finish concurrently with the query.
- Issues that require an attacker to already have write access to the user's machine or to the indexed repository.

## Disclosure

We aim to ship a fix within 14 days of confirming a valid report. A coordinated disclosure timeline can be negotiated for issues that need longer.
