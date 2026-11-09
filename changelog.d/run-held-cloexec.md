- Memory verify seats no longer inherit a descriptor to their own executable. The held
  descriptor is closed on exec, so the seat process and anything it starts never see it.
