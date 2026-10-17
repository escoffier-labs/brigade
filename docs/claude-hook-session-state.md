# Claude hook session-state containment

Session state lives under `.brigade/work/claude-hooks/sessions/` inside the
resolved target root. The hook runtime opens each component without following
links and retains the directory chain for the operation. A locked transaction
retains the same chain across its reads, writes, and timeout-sidecar operations.
Each access checks that the directory entries still identify the held objects.

The session-directory operations are:

| Operation | Containment |
| --- | --- |
| JSON read | Open the known name relative to the held sessions directory, reject links and nonregular or multiply linked files, then revalidate after reading. |
| JSON write | Create an exclusive sibling temporary file with mode `0600`, write and fsync it, revalidate, replace the known name relative to the same directory, and sync the directory. Temporary cleanup also uses the held directory. |
| Timeout journal and pending increments | Read or append the known sidecar name relative to the held directory with the existing byte limits. |
| Session lock | Open a known sibling name relative to the held directory and use the existing deadline-bounded flock or Windows byte lock. Check the held lock identity before protected access. |
| Recent session listing | Enumerate the held directory, stat candidate names without following links, and read each JSON file through the same anchor. Preserve timestamp filtering, ordering, and limits. |

There is no session-file cleanup or directory-pruning operation. Heartbeats,
compaction markers, hook logs, and cache task epochs use separate storage paths
and are outside this change.

POSIX uses `O_DIRECTORY`, `O_NOFOLLOW`, and descriptor-relative operations.
Windows uses the existing `nt_dirfd` operations through `brigade.dirfd`. Those
operations reject reparse points when opening components and files. Validation
also checks reparse tags and file attributes. Known filenames do not require
enumeration. Current main provides held-handle enumeration for recent-state
listing. If that operation is unavailable, listing produces a diagnostic and
returns no state. It never falls back to reopening a directory pathname.

Reads refuse unsafe or missing state with the existing empty result. Sidecar
appends return false on refusal. JSON writes and lock acquisition raise
`OSError`, allowing the outer hook to use its existing degraded response.
Advisory locks cannot constrain a process that ignores them or directly edits
the trusted target tree. The descriptor checks prevent a substituted pathname
from redirecting an operation into another tree.

## Native Windows qualification

`tests/test_claude_hooks_state_containment.py` uses directory junctions on
Windows, including swaps of every component below the target during a held
transaction. Its Windows-only backend-unavailable test is marked with a native
platform skip. These tests are discovered by the Windows pytest driver.

Before merging, run this selector on native Windows with no native test skips
and obtain the independent security review required by issue #1238. The current
`windows-native-acceptance` workflow explicitly selects process-containment and
skills-state tests. The conductor must include the new hook-containment
selector in that gate. Workflow changes are outside this implementation lane's
allowed write set.
