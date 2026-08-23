# Research live acceptance

Maintainer-operated live checks for first-class multi-lane research. These are
not counted CI tests.

Hermetic stubs in `tests/test_research_acceptance.py` and
`tests/test_agents_oracle.py` cover command construction and failure
handling. They do not prove live Oracle authentication or model identity.
Only a real `oracle --engine browser` session can prove those.

Do not treat Oracle synthesis as ready by default after `which oracle`
succeeds. Brigade invokes `oracle --engine browser -p <prompt>` and does
not pass `--browser-cookie-sync` or another cookie-source flag. That argv
matches Oracle **0.16.1** defaults (cookie copy on, except Windows). Oracle
**0.18.0** made live Chrome cookie copy opt-in. A clean 0.18.0+ install can
pass the doctor executable check while lacking cookies for a live browser
run.

Before the commands below, choose an explicit Oracle cookie source on the
browser host and confirm a one-shot `oracle --engine browser -p "Reply with
exactly: ORACLE-OK"` returns that text. If Oracle reports a login or
expired-session error, stop. Record `browser-auth`, run
`brigade pantry expiry-alert`, restore the cookie source, and retry the
smoke. Do not retry in a loop.

Remote Gemini through `oracle bridge` or `--remote-host` is unsupported.

## Exact operator commands

```bash
oracle --version
brigade research doctor --profile grounded --json
brigade research run "Summarize the latest Brigade research changes" --source "docs/*.md" --profile grounded --json
brigade research run "Find current browser-agent research products" --profile browser-ai --browser-ai-research --json
```

## Required evidence

Record all of the following for each live pass:

- Oracle version (`oracle --version` or equivalent)
- Cookie-source strategy actually used (`--browser-cookie-sync`, a configured
  browser profile, or another supported Oracle cookie source). Do not record
  cookie values or profile paths.
- Requested model and observed model (or `unverified` when observation is unavailable)
- Run IDs from each `research run`
- Wall-clock duration per run
- Citation audit result (`accepted` / unresolved count)
- Fallback state (none, or recorded `from_seat` / `to_seat` / `failure_kind`)
- Redacted `research doctor` JSON (no cookie values, profile paths, bearer tokens, or home paths)

Research run artifacts record requested model, observed model or
`unverified`, and any fallback. They do not currently persist the Oracle
CLI version; copy that value from `oracle --version` and the redacted
doctor `version` field into the live-acceptance receipt.

## Notes

- Use a real authenticated Oracle browser session before the grounded and browser-AI runs.
- Prefer a temporary target workspace; do not point live acceptance at an operator home without intent.
- Oracle may still write sessions and artifacts outside that workspace even
  when Brigade's adapter is hard read-only for workspace writes.
- Attach the evidence to the release or phase verification receipt that claims live Oracle acceptance.
