# Research live acceptance

Maintainer-operated live checks for first-class multi-lane research. These are
not counted CI tests.

Hermetic stubs in `tests/test_research_acceptance.py` do not prove live Oracle
authentication or model identity. Only a real `oracle --engine browser` session
can prove those.

## Exact operator commands

```bash
brigade research doctor --profile grounded --json
brigade research run "Summarize the latest Brigade research changes" --source "docs/*.md" --profile grounded --json
brigade research run "Find current browser-agent research products" --profile browser-ai --browser-ai-research --json
```

## Required evidence

Record all of the following for each live pass:

- Oracle version (`oracle --version` or equivalent)
- Requested model and observed model (or `unverified` when observation is unavailable)
- Run IDs from each `research run`
- Wall-clock duration per run
- Citation audit result (`accepted` / unresolved count)
- Fallback state (none, or recorded `from_seat` / `to_seat` / `failure_kind`)
- Redacted `research doctor` JSON (no cookie values, profile paths, bearer tokens, or home paths)

## Notes

- Use a real authenticated Oracle browser session before the grounded and browser-AI runs.
- Prefer a temporary target workspace; do not point live acceptance at an operator home without intent.
- Attach the evidence to the release or phase verification receipt that claims live Oracle acceptance.
