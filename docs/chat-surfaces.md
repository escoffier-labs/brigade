# Brigade Chat Surface Exports

`brigade chat` describes local chat export surfaces, validates safe finding summaries, and routes actionable items into the existing scanner inbox. It is local-only and does not call live chat APIs.

## Commands

```bash
brigade chat surfaces init
brigade chat surfaces list
brigade chat surfaces show discord-export
brigade chat surfaces doctor
brigade chat sweep validate .brigade/chat-surfaces/discord-export.json
brigade chat sweep ingest discord-export
brigade chat sweep import-issues discord-export
```

## Config

The local config is gitignored:

```text
.brigade/chat-surfaces.toml
```

Each surface entry includes:

- `id`
- `provider`
- `workspace_label`
- `channel_label`
- `export_path`
- `sweep_output_path`
- `enabled`
- `privacy_mode`
- `evidence_policy`
- `confidence_threshold`

Supported provider families are `discord-export`, `slack-export`, `telegram-export`, `clickclack-export`, and `generic-jsonl`. Config and finding input may also use provider aliases:

- `discord`, `discord-json`
- `slack`, `slack-json`
- `telegram`, `telegram-json`
- `clickclack`
- `generic`, `generic-json`, `jsonl`

Aliases are normalized to the canonical provider family in sweep output.

## Finding Contract

Export findings should contain:

- `sweep_id`
- `provider`
- `surface_id`
- `channel_label`, `thread_label`, and `message_range_label`
- `issue_id`
- `issue_type`
- `priority`
- `confidence`
- `safe_summary`
- `evidence_summary`
- `suggested_task_text`
- `acceptance_criteria`
- `source_fingerprint`

`brigade chat sweep ingest <surface-id>` normalizes those findings into `.brigade/chat-memory-sweeps/<surface-id>-latest.json`. `brigade chat sweep import-issues <surface-id>` converts the normalized findings into `chat-memory-sweep` work imports with stable fingerprints and dismissed-until-changed behavior. Task findings can be promoted into the normal work task loop; durable non-task findings such as decisions or preferences can be promoted into reviewed Memory Handoff drafts.

## Privacy Boundary

Raw exports and sweep outputs stay under `.brigade/` and should remain gitignored. Brigade rejects raw private chat fields by default, including `raw_text`, `raw_messages`, `messages`, `message_text`, `quotes`, and `transcript`.

Use safe summaries, labels, message ranges, local evidence paths, and confidence values. Brigade does not run Discord, Slack, Telegram, or ClickClack APIs, perform OAuth, send webhooks, edit memory, promote imports, or run a daemon.

## Live Run Controls

`brigade runs watch`, `brigade runs steer`, and `brigade runs interrupt` operate on local run artifacts and, for active Codex app-server runs, the `control_socket` recorded in `run.json`.
They are not chat export APIs. `runs watch` tails `.brigade/runs/<run>/events/*.jsonl` and other artifacts; `runs steer` and `runs interrupt` send newline-delimited JSON to the local Unix socket for an active worker turn.

The socket exists only while the run process is alive and is removed during cleanup. Completed runs remain inspectable through artifacts, but cannot be steered or interrupted.
