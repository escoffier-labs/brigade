- Hub memory has a `read_stats` action that returns per-card read aggregates for one scope.
  It needs one grant on that exact scope holding both `read` and a new `read_stats` right
  (wildcards, node defaults and `reflect` do not open it), pages at most 1,000 rows through
  a 15-minute cursor bound to the caller and scope, and allows one request per second and
  600 per day per principal. Each admitted request gets an audit row in the hub audit log,
  refusals are merged into one summary row per principal per hour, and the hub writes at
  most 20,000 such rows a day. `memory_hub_client.read_stats` reads a page and refuses the
  admin credential.
