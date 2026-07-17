# Environment override log scrubbing

Issue #310 closes two follow-ups from the per-seat environment override work. A direct CLI can echo an injected override value in stdout or stderr, and those streams are later stored under the run directory. Resume also rebuilds agents from `roster.json` without applying the environment-table validation used by the original roster loader.

After a seat environment table resolves successfully, `agents.run_agent` will replace every exact nonempty resolved override value in output-derived strings with the resolved target name in brackets, such as `[ANTHROPIC_AUTH_TOKEN]`. Scrubbing happens after the process returns and after structured Grok JSON is parsed, so replacement cannot corrupt parsing. The returned text, detail, stdout, and stderr are scrubbed before worker, attempt, synthesis, or research code can persist them. Longer values are replaced first, and equal values use a deterministic target name. Brigade does not scan the parent environment, apply heuristic secret detection to output, or change the child environment passed to the CLI.

Snapshot resume will call the roster loader's `_as_env` validator for each stored agent environment table. Invalid variable names, inline secret names or values, malformed references, and target collisions are rejected before a resumed synthesis can dispatch. Existing valid snapshots keep the same environment mapping.

This change adds no dependency, does not rewrite historical logs, and does not turn log scrubbing into a general content-guard pass. Exact-value replacement is bounded to values Brigade resolved for the current seat dispatch.
