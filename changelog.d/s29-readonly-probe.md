- The verify-brigade hub contract drive has a `--profile prod-readonly` mode: an exact list of
  operations that a SQLite authorizer audit proves write nothing on the Python hub and on the
  Rust front in managed proxy mode, with a whole-database digest and no retention exception.
  A case outside the list stops the run before any request, and the Proxmox hub runbook names
  it as the only drive run allowed against production.
