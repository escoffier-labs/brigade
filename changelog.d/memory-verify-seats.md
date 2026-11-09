- `brigade memory verify doctor` resolves `propose_seat` and `confirm_seat` from the local
  roster and probes each one. Seats whose prompts would reach a cloud provider, hosted chat
  session, remote Ollama model or other connector audience are refused with
  `verify_seat_connector` before any prompt is written, and so are seats only the hub knows
  about. Seats without an isolation profile (`claude` and local `ollama:<model>` have one),
  script launchers, seats that pin no exact model, and two seats on one model are refused too.
- Every Claude verify call runs with tools, hooks, settings, plugins, MCP servers and slash
  commands switched off, and is refused unless the CLI's own init event confirms it. The seat
  runs in an empty private directory under a temporary root with no instruction files above
  it. Its executable is hashed and checked again before every call. A passing probe is
  recorded per fingerprint, so a changed seat, an upgraded CLI or a new Ollama model must be
  probed again.
- Verify prompts and replies live only in owner-only work areas that are removed after every
  call, including after a timeout or interrupt. A failed removal is reported and blocks new
  calls until the leftover can be removed.
