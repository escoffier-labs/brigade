# Hermes Handoffs

Brigade treats Hermes as a local Memory Handoff writer through `.hermes/memory-handoffs/`.
The inbox uses the same Markdown handoff schema as Claude Code, Codex, and OpenCode.

![Hermes is a local handoff writer inbox](assets/hermes-handoff-flow.svg)

## Local Setup

```bash
brigade init --target . --depth workspace --harnesses hermes
brigade handoff sources init --target . --force
```

`init` writes the shared workspace files, the Hermes adapter fragments under `.brigade/hermes/`, the local `.hermes/memory-handoffs/` inbox, and the matching gitignore entries. `handoff sources init` records the local inboxes that the canonical ingestor is expected to scan, including `.hermes/memory-handoffs/`.

Hermes can be the memory owner:

```bash
brigade init --target . --depth workspace --harnesses claude,codex,openclaw,hermes --owner hermes
```

## Draft A Hermes Handoff

```bash
brigade handoff draft \
  --target . \
  --inbox hermes \
  --title "Hermes smoke handoff" \
  --summary "Hermes can write a local Brigade Memory Handoff." \
  --content "### Hermes smoke handoff

Hermes uses the shared Brigade handoff format."
```

This creates a local ignored file under `.hermes/memory-handoffs/` and lints it before returning success.

## Smoke Test The Wiring

```bash
brigade handoff sources init --target . --force
brigade handoff draft --target . --inbox hermes \
  --title "Hermes smoke handoff" \
  --summary "Hermes can write a local Brigade Memory Handoff." \
  --content "### Hermes smoke handoff

Hermes uses the shared Brigade handoff format."
brigade operator verify-harness --harness hermes --target .
brigade handoff list --target .
brigade handoff doctor --target .
```

Expected ready state:

- `.hermes/memory-handoffs/` exists
- `.hermes/memory-handoffs/` is gitignored
- `.brigade/handoff-sources.json` watches `.hermes/memory-handoffs/`
- pending Hermes drafts pass `brigade handoff lint`

After a memory owner reviews and ingests a Hermes draft, record the outcome locally:

```bash
brigade handoff receipt record --target . --owner hermes --run-id hermes-manual-001 <draft-id>
brigade handoff show --target . <draft-id>
brigade handoff archive --target . <draft-id> --reason "reviewed"
```

The receipt is local evidence only. It lets `handoff list`, `handoff show`, and archive records report whether the Hermes draft was ingested, skipped, or failed.

## Boundaries

This is repo-local writer wiring. Brigade does not start Hermes, install a Hermes container, call a live Hermes API, or ingest handoffs into canonical memory automatically. The live Hermes runtime still needs a separate smoke test once a Hermes environment is available.

For a shorter copyable recipe, see [Hermes handoff smoke example](examples/hermes-handoff-smoke.md).
