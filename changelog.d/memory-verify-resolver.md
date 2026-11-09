- Memory verification can now check an abbreviated commit id against git. The new resolver fetches each allowed
  remote-tracking ref over https in the same run, and expands an abbreviation only when exactly one object
  matches, that object is a commit, and an allowed ref can reach it. It refuses repositories whose local config
  holds includes, hooks, helpers, aliases, URL rewrites, proxies or any `http.*` setting, and it refuses shallow
  clones, partial clones and grafts. The minimum abbreviation drops from 12 to 7 hex characters.
- The resolver runs a trusted git from `[verify] git_executable` or the fixed system directories, never from
  `PATH`, with an environment built from scratch. No loader, OpenSSL, proxy, curl or XDG variable and no
  inherited `HOME` reaches git. A timeout kills git's whole process tree, through a job object on Windows.
- `[[verify.workspace]]` accepts an optional `commit_refs` list of fully qualified `refs/remotes/<remote>/<branch>`
  refs. Without it, the resolver uses the single ref that `refs/remotes/origin/HEAD` names.
