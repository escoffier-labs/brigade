- Memory verification can now check an abbreviated commit id against git. The new resolver fetches each allowed
  remote-tracking ref over https in the same run, and expands an abbreviation only when exactly one object
  matches, that object is a commit, and an allowed ref can reach it. It refuses repositories whose local config
  holds includes, hooks, helpers, aliases, URL rewrites, proxies or any `http.*` setting, and it refuses shallow
  clones, partial clones and grafts. The minimum abbreviation drops from 12 to 7 hex characters.
- The resolver runs only a root-owned git from `/usr/bin`, `/bin` or `/usr/local/bin`, executed from a held
  descriptor, with an environment built from scratch. Commit expansion is POSIX-only for now: on Windows every
  abbreviation abstains with `verify_resolver_unsupported_platform`.
- `[[verify.workspace]]` accepts an optional `commit_refs` list of fully qualified `refs/remotes/<remote>/<branch>`
  refs. Without it, the resolver uses the single ref that `refs/remotes/origin/HEAD` names.
