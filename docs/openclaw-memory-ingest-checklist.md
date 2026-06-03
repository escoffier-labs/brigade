# OpenClaw Memory Ingest Checklist

Use this checklist when Brigade handoffs are ready to become durable memory through OpenClaw or another memory owner.

## Review Queue

```bash
brigade handoff list --target .
brigade handoff lint --content-guard --guard-policy personal --target .
```

Requirements before ingest:

- Brigade lint passes.
- Content Guard passes with the `personal` policy.
- The operator has reviewed the handoff.
- The target memory card or document is explicit.
- Raw transcripts, tokens, host-private paths, and private identifiers are absent or redacted.

## Inspect A Draft

```bash
brigade handoff show <handoff-id-or-path> --target .
```

Review:

- target card or target document
- suggested action
- source import id and source fingerprint when present
- scanner provenance when present
- stale age and latest ingestion status

## Ingest Boundary

Brigade does not write canonical memory. OpenClaw, Hermes, or another memory owner performs the actual ingest after review.

Treat every file under `.claude/memory-handoffs/`, `.codex/memory-handoffs/`, `.opencode/memory-handoffs/`, and `.hermes/memory-handoffs/` as pending review until it has an ingestion receipt or manual archive record.

## Reconcile Receipts

After the memory owner runs, record what happened:

```bash
brigade handoff reconcile --target .
brigade handoff runs --target .
brigade handoff run-show <run-id> --target .
```

Receipts should show processed, promoted, routed, skipped, failed, malformed, unreachable-source, warning, and no-reply events when the ingestor reports them.

## Archive Closeout

```bash
brigade handoff archive <handoff-id-or-path> --target .
brigade handoff archive --all-reviewed --target .
```

Archive only after the handoff is either ingested, intentionally skipped, or replaced by a better draft. Invalid drafts stay in the inbox for repair.

## Fast Path

```bash
brigade handoff list --target .
brigade handoff lint --content-guard --guard-policy personal --target .
brigade handoff show <handoff-id-or-path> --target .
# memory owner ingests outside Brigade
brigade handoff reconcile --target .
brigade handoff archive <handoff-id-or-path> --target .
```
