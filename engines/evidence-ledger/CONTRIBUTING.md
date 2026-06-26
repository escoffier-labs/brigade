# Contributing

MiseLedger is a local-first Go CLI that normalizes scattered AI work history
into a searchable evidence ledger. The bar is "imports faithfully, stays local,
and never loses or corrupts evidence."

## Local setup

```bash
go build ./cmd/miseledger
go test ./...
gofmt -l . && go vet ./...
```

## What lands easily

- A new source adapter with tests and a small synthetic fixture (see `testdata/`)
- Bug fixes with a test that fails before and passes after
- Documentation

## What needs a conversation first

Open an issue before a PR for:

- Changes to the `adapter.v1` record schema or the SQLite schema (these need a
  migration and back-compat story)
- Anything that writes outside the data directory or makes a network call

## Rules

- **No real personal evidence** in tests or fixtures; use small synthetic
  `adapter.v1` records.
- Conventional commits, no AI co-authorship trailers.
