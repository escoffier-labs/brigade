- Claude Code recall hooks read hub memory when the workspace's memory config is in `read` or
  `sync` mode. Session-start and prompt recall fuse hub hits with local cards inside the existing
  5 s recall child. With the hub stopped or hung they answer from local cards and the replica
  without a warning line. Hub hits are named `hub:<scope>/<card_id>`, and flagged hub versions
  are never shown.
- Every recall line a hook injects, local cards included, now goes through the memory prompt-load
  scan. A blocking or warning finding shows only `- <card_id> | blocked: <rule id>`. Private-class
  text still loads into local sessions and is withheld with `code: memory_content_private` when
  the session runs in a cloud sandbox (`CLAUDE_CODE_REMOTE`) or `BRIGADE_RECALL_AUDIENCE=connector`
  is set. A scanner that cannot run injects no recall.
- Recall skips cards the work brief already named at session start and cards named in a prompt's
  `memory_context` block, so a card is not shown twice.
- The Stop hook sends hub memory a `recall.capture` event with a hashed session reference and the
  card keys recall showed and the session opened with the Read tool. It holds ids only. A batch
  the hub does not take stays pending and is retried with the same event id at the next Stop.
