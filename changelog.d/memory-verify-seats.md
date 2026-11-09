- `brigade memory verify doctor` resolves `propose_seat` and `confirm_seat` from the local
  roster and probes each one. Seats whose prompts would reach a cloud provider, hosted chat
  session or other connector audience are refused with `verify_seat_connector` before any
  prompt is written, and so are seats only the hub knows about. Only `claude` seats have an
  isolation profile in v1. `ollama:` seats are refused because Ollama tags are mutable and no
  model can be launched by digest. Script launchers, seats that pin no exact model, and two
  seats on one model are refused too.
- Every Claude verify call runs with tools, hooks, settings, plugins, MCP servers and slash
  commands switched off, and is refused unless the CLI's own init event confirms it. The seat
  runs in an empty private directory under a temporary root with no instruction files above
  it. Its executable is hashed and checked again before every call. A passing probe is
  recorded per fingerprint, so a changed seat or an upgraded CLI must be probed again. The
  verified executable is launched from its open descriptor, so a binary swapped after the
  check never runs.
- Verify prompts and replies live only in owner-only work areas that are removed after every
  call, including after a timeout or interrupt. A failed removal is reported and blocks new
  calls until the leftover can be removed.
