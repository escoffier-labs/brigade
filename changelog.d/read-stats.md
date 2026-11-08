- Hub memory has a `read_stats` action that returns per-card read aggregates for one scope.
  It needs `read` and a new `read_stats` right granted on that exact scope (wildcards and
  `reflect` do not open it), pages at most 1,000 rows through a 15-minute cursor bound to the
  caller and scope, allows one request per second and 600 per day per principal, and records
  every request in the hub audit log. `memory_hub_client.read_stats` reads a page and refuses
  the admin credential.
