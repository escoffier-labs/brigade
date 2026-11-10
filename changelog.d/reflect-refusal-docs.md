- The scheduled hub reflection docs now say that a scan metadata refresh against a hub that
  predates scan metadata fails the run and pauses the job after three failures, and that stored
  non-retryable refusals such as `memory_content_blocked` do not expire when hub policy or the
  scan ruleset is relaxed, with the ways to clear them.
