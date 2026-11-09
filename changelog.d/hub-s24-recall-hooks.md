- Claude Code recall hooks read hub memory when the workspace's memory config is in `read` or
  `sync` mode. Session-start and prompt recall fuse hub hits with local cards inside the existing
  5 s recall child. With the hub stopped or hung they answer from local cards and the replica
  without a warning line. Hub hits are named `hub:<scope>/<card_id>`, and only `reviewed`,
  `policy-promoted` and `imported` hub versions are shown, so another principal's unreviewed card
  in a shared scope never appears unasked.
- Every recall line a hook injects, local cards included, now goes through the memory prompt-load
  scan, each rendered field on its own and then the whole line. A blocking or warning finding
  shows only `- <card_id> | blocked: <rule id>`. Private-class text is withheld with
  `code: memory_content_private` unless the operator opted the host in with
  `brigade work hooks install --recall-audience local` (or `update`), which writes a marker bound to
  the host name, the machine id (required except on macOS) and the Brigade home's inode. On Windows
  the Brigade home must sit inside the user's profile. Nothing
  writes it by default. `--recall-audience connector` turns it off even when the file cannot be
  removed, and exits 2 with the marker path if local recall may still be on. `CLAUDE_CODE_REMOTE` or a `BRIGADE_RECALL_AUDIENCE`
  value other than `local` always means connector. `brigade doctor` warns "local recall not
  enabled" with the command when a recall target is active. A scanner that cannot run injects no
  recall.
- Recall skips cards the SessionStart work brief already named, so a card is not shown twice. Ids
  pasted into a prompt never hide a card.
- The Stop hook sends hub memory a `recall.capture` event with a hashed session reference and the
  card keys recall showed and the session opened with the Read tool (any card search lists, at any
  depth under a card root), under the id card search gives it. It holds ids only. A batch the hub does not take stays pending and is retried with
  the same event id at the next Stop.
- At session start the work brief runs first under its own deadline, and memory recall gets only
  the hook time the brief left. On a slow host recall is skipped instead of costing the brief its
  injection.
