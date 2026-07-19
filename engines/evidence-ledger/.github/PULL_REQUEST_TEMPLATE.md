<!-- Keep this short; delete sections that do not apply. See CONTRIBUTING.md. -->

## What and why

<!-- One or two sentences on the change and the problem it solves. -->

Closes #

## Type of change

- [ ] New source adapter (with tests + fixture)
- [ ] Bug fix
- [ ] Schema change (`adapter.v1` or SQLite) - includes a migration, opened an issue first
- [ ] Docs

## Checklist

- [ ] `go test ./...` passes; `gofmt -l .` and `go vet ./...` are clean
- [ ] Added or updated tests covering the change
- [ ] No real personal evidence in tests or fixtures (synthetic `adapter.v1` records only)
- [ ] No new outbound network calls in the core path; nothing writes outside the data dir
- [ ] Updated the `Unreleased` section of `CHANGELOG.md` for any user-visible effect
- [ ] Conventional commit messages, no AI co-authorship trailers
