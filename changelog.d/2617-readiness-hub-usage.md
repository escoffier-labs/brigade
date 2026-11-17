- `brigade roster ready` reads live Hub seat occupancy from the new read-only `GET /models/usage`
  route, so positive seat limits and enrolled fleet policy seats report admitted capacity instead of
  staying unverified, and a full seat blocks.
- `brigade roster ready` bounds its spool-lock waits with the existing 2.5-second client timeout and
  reports a lock timeout as unverified rather than reading it as no prior enrollment.
- Check every selected Hub seat with a positive limit and report the smallest free capacity, so one full seat cannot hide behind an idle one (#2617).
