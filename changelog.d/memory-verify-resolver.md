- Memory verification can now check an abbreviated commit id against git. The new resolver fetches each allowed
  remote-tracking ref over https in the same run, and expands an abbreviation only when exactly one object
  matches, that object is a commit, and an allowed ref can reach it. It refuses repositories whose local config
  holds includes, hooks, helpers, aliases, URL rewrites, proxies or any `http.*` setting, and it refuses shallow
  clones, partial clones and grafts. The minimum abbreviation drops from 12 to 7 hex characters.
- `[[verify.workspace]]` accepts an optional `commit_refs` list of fully qualified `refs/remotes/<remote>/<branch>`
  refs. Without it, the resolver uses the single ref that `refs/remotes/origin/HEAD` names.
