- Claude Code recall hooks read hub memory when the workspace's memory config is in `read` or
  `sync` mode. Session-start and prompt recall fuse hub hits with local cards inside the existing
  5 s recall child. With the hub stopped or hung they answer from local cards and the replica
  without a warning line. Hub hits are named `hub:<scope>/<card_id>`, and only `reviewed`,
  `policy-promoted` and `imported` hub versions are shown, so another principal's unreviewed card
  in a shared scope never appears unasked.
- Every recall line a hook injects, local cards included, now goes through the memory prompt-load
  scan. A blocking or warning finding shows only `- <card_id> | blocked: <rule id>`. Private-class
  text loads only into sessions on a host that carries the local-audience marker, which
  `brigade work hooks install` and `update` write under the Brigade user home. Elsewhere, and in
  any session with `CLAUDE_CODE_REMOTE` or a `BRIGADE_RECALL_AUDIENCE` value other than `local`,
  it is withheld with `code: memory_content_private`. `brigade doctor` warns when the marker is
  missing. A scanner that cannot run injects no recall.
- Recall skips cards the SessionStart work brief already named, so a card is not shown twice. Ids
  pasted into a prompt never hide a card.
- The Stop hook sends hub memory a `recall.capture` event with a hashed session reference and the
  card keys recall showed and the session opened with the Read tool, under each card's front
  matter id. It holds ids only. A batch the hub does not take stays pending and is retried with
  the same event id at the next Stop.
