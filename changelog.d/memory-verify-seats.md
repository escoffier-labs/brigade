- `brigade memory verify doctor` resolves `propose_seat` and `confirm_seat` from the local
  roster and probes each one for a tool-free, recall-free call in an empty private working
  directory. Seats that would reach a cloud provider, hosted chat session or other connector
  audience are refused with `verify_seat_connector` before any prompt is written, and so are
  seats only the hub knows about. Seats without an isolation profile (`claude` and local
  `ollama:<model>` have one), seats that pin no exact model, and two seats on one model are
  refused too. A passing probe is recorded per seat fingerprint, so a changed seat or an
  upgraded CLI must be probed again. Prompts and replies live only in an owner-only work area
  under `.brigade/memory/verify/work/` that is removed after every call, including after a
  timeout or interrupt.
