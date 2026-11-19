- Hub memory keeps rollback quota checks flat as a scope accumulates automatic
  applies: the memory schema adds covering indexes for the principal and scope
  usage probes, so a rollback no longer reads every proposal body to decide a
  quota. A hub picks the indexes up when it opens the memory file on the new
  version, so restart the hub after upgrading.
