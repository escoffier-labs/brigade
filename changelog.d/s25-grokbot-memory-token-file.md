- Grok Bot feeds can attach `memory_refs` to new jobs. Name the Grok Bot memory principal's token
  file in the user-level `~/.brigade/fleet.toml` under `[grokbot_memory] token_file`. The file must
  be an absolute path to a private regular file owned by the feed user, not a symlink, at most
  4 KiB, and the fleet hub must confirm the token as `agent:grokbot-memory`. A workspace file never
  sets the path, and a relative `BRIGADE_HOME` turns references off. Windows is not supported.
  Without the key, or when anything is refused, references stay off, and
  `brigade run cloud grokbot doctor` prints a `memory-refs` line with the reason and never the token.
