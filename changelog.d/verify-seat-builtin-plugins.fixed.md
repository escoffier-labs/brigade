- Claude verify seats now switch off all 13 built-in plugins that Claude CLI 2.1.325 ships.
  Before, a rollout that loaded the easel plugin on some calls made a seat fail its
  isolation check partway through a run, so `memory verify gate` aborted with
  `verify_seat_not_isolated` at random.
