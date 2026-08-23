# Research live acceptance

Maintainer-operated live checks for first-class multi-lane research. These are
not counted CI tests.

Hermetic stubs in `tests/test_research_acceptance.py` do not prove live Oracle
authentication or model identity. Only a real `oracle --engine browser` session
can prove those.

## Oracle 0.18+ cookie sync is opt-in

Oracle 0.18.0 changed live Chrome cookie copying to opt-in
([upstream change](https://github.com/steipete/oracle/commit/3a185f55918a8f0dd36f9c2f0144550616b88803);
[follow-up repair](https://github.com/steipete/oracle/pull/383)). Brigade's
adapter was written and tested against Oracle 0.16.1, where cookie sync was on
by default on non-Windows systems. A clean Oracle 0.18+ install can pass the
executable check while lacking the cookies needed for a live browser run, so:

- Choose an explicit browser-cookie strategy before the live run — enable
  `--browser-cookie-sync` (or the fork equivalent), configure a browser
  profile, or use another supported Oracle cookie source. Do not rely on
  Oracle's default.
- Record which strategy was used with the acceptance evidence.
- If a run fails auth, expect Brigade's diagnostic to name this requirement;
  fix cookie sync before re-running rather than assuming a stale jar.
- The adapter's command surface and read-only classification are tracked at
  [`src/brigade/agents.py`](https://github.com/escoffier-labs/brigade/blob/main/src/brigade/agents.py).

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
