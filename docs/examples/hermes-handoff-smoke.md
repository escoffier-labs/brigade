# Hermes Handoff Smoke Example

This is a local-only smoke path for a repo where Hermes writes Brigade Memory Handoffs.

```bash
brigade init --target . --depth workspace --harnesses hermes
brigade handoff sources init --target .
brigade handoff draft --target . --inbox hermes \
  --title "Hermes smoke handoff" \
  --summary "Hermes can write a local Brigade Memory Handoff." \
  --content "### Hermes smoke handoff

Hermes uses the shared Brigade handoff format."
brigade operator verify-harness --harness hermes --target .
brigade handoff list --target .
```

When the memory owner reviews and ingests the draft, record the local outcome:

```bash
brigade handoff receipt record --target . --owner hermes --run-id hermes-manual-001 <draft-id>
brigade handoff show --target . <draft-id>
brigade handoff archive --target . <draft-id> --reason "reviewed"
```

Expected local paths:

- `.hermes/memory-handoffs/TEMPLATE.md`
- `.hermes/memory-handoffs/processed/`
- `.brigade/hermes/workspace.harness.json`
- `.brigade/hermes/memory-handoff.harness.json`
- `.brigade/handoff-sources.json`

The verifier checks that the Hermes adapter fragments point at `.hermes/memory-handoffs/`, not another harness inbox.

Brigade does not start Hermes, call a Hermes API, or ingest memory automatically. The example verifies the repo-local handoff contract Hermes users depend on.
