- `brigade run cloud register`, `adopt`, `status` and `sync` now track Claude cloud sessions that a
  conductor launched through a one-time routine. New `--routine-id`, `--issue`, `--session-url` and
  `--observed-status` flags record the routine, the issue and the run state the conductor read.
  Routine rows classify from GitHub branch and PR state plus that report, and registering one needs
  no hub cloud lane. The `claude-cloud` source reports `conductor-routed` instead of
  `disabled-by-policy`, `doctor --provider claude-cloud` passes offline, and
  `launch --provider claude-cloud` refuses with a pointer to the conductor runbook in
  `docs/claude-cloud-capabilities.md`.
