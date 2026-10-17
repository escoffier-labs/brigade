# Claude hook session-state containment

Session state lives under `.brigade/work/claude-hooks/sessions/` inside the
resolved target root. The hook runtime opens each component without following
links and retains the directory chain for the operation. A locked transaction
retains the same chain across its reads, writes, and timeout-sidecar operations.
Each access checks that the directory entries still identify the held objects.

The session-directory operations are:

| Operation | Containment |
| --- | --- |
| JSON read | Open the known name relative to the held sessions directory and validate the held file as regular with one link. Trust its contents across concurrent atomic replacement, then revalidate the directory chain. An inode already unlinked before its initial check triggers a bounded open retry. |
| JSON write | Create an exclusive sibling temporary file with mode `0600`, write and fsync it, revalidate, replace the known name relative to the same directory, compare the published name with the held temporary fd, and sync the directory. Temporary cleanup also uses the held directory. |
| Timeout journal and pending increments | Read or append the known sidecar name relative to the held directory with the existing byte limits. |
| Session lock | Open a known sibling name relative to the held directory and use the existing deadline-bounded flock or Windows byte lock. Check the held lock identity before protected access. |
| Recent session listing | Enumerate the held directory, stat candidate names without following links, and read each JSON file through the same anchor. Preserve timestamp filtering, ordering, and limits. |

There is no session-file cleanup or directory-pruning operation. Heartbeats,
compaction markers, hook logs, and cache task epochs use separate storage paths
and are outside this change.

The target root must already exist. Hook state I/O never creates it. New shared
ancestors `.brigade`, `work`, and `claude-hooks` use mode `0777` subject to umask,
matching their previous defaults. The sessions directory uses mode `0700`.

POSIX uses `O_DIRECTORY`, `O_NOFOLLOW`, and descriptor-relative operations.
Windows uses the existing `nt_dirfd` operations through `brigade.dirfd`. Those
operations reject reparse points when opening components and files. Validation
also checks reparse tags and file attributes. Known filenames do not require
enumeration. Current main provides held-handle enumeration for recent-state
listing. If that operation is unavailable, listing produces a diagnostic and
returns no state. It never falls back to reopening a directory pathname.

Missing state returns the existing empty result. Malformed JSON retains its
existing behavior. Directory changes, reparse points, invalid file types and
link counts raise `SessionIntegrityError`. State reads, timeout journals,
pending increments, latch reads and announce claims propagate that refusal.
They never reinterpret it as missing state or persist a rebuilt state after it.
The timed worker returns a blocking Stop response, a denied PreToolUse response,
or [`continue: false`](https://code.claude.com/docs/en/hooks#json-output) for
other events. The integrity result survives Windows
worker serialization and the bounded timeout follow-up path.

Writes remain bound to the validated directory object. A same-uid attacker can
rename that directory and relocate the object, but cannot redirect its held
descriptor into a different existing directory. A rename after the last chain
check can therefore relocate a write without being detected by that check.
The post-publication identity check detects a substituted temporary entry, but
runs after publication and does not roll back bytes already published. These
same-uid mutations and processes that ignore advisory locks remain residuals
under [the threat model in #1093](https://github.com/escoffier-labs/brigade/issues/1093).
Windows directory sharing modes are unchanged.

## Native Windows qualification

`tests/test_claude_hooks_state_containment.py` uses directory junctions on
Windows, including swaps of every component below the target during a held
transaction. Its Windows-only backend-unavailable test is marked with a native
platform skip. The same file also runs the read-during-replace regression.
If Windows refuses a directory rename while the sibling lock is open, the
parent-swap test asserts that refusal and verifies the original state and
outside tree are unchanged. If the rename succeeds, it exercises the operation
and requires an integrity refusal. Neither outcome skips the native test.

The PR's `windows-native-acceptance` job explicitly selects this file together
with process-containment and skills-state tests, and fails on native test skips.
The broader Windows pytest job runs on main. Native passing evidence and the
independent security review required by issue #1238 remain merge gates.
