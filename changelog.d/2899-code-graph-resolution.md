- Code Graph now uses the canonical database and unsafe-root environment
  overrides. If you have an old repository index, run `brigade code sync` to
  rebuild it in the current location. The rebuild leaves the old index intact.
