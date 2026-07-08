# Pull Request Draft

Summary:

- Adds a staged-file scan before public pushes.
- Dogfooded against a local checkout at `/home/example-user/work/private-repo`.

Notes to remove before publishing:

- Follow-up task lives in REDACTED-TRACKER-123.
- Private branch name was `example-user/private-dogfood-run`.
<!-- content-guard: allow localhost-port -->
- Local reproduction used `http://localhost:5204/debug/report`.

Expected handling: draft-only local context should be blocked or redacted before
the text is used as a hosted pull request body.
