# Export lifecycle journals

`brigade receipts export journal` reads one existing lifecycle journal at
`<target>/.brigade/runs/<run-id>/events/lifecycle.jsonl`, validates the entire
original chain, and exports canonical UTF-8 JSONL with LF framing.

```bash
brigade receipts export journal --target ./workspace --run-id example-run --out ./events.jsonl
brigade receipts export journal --target ./workspace --run-id example-run --since 2026-07-27 --out -
```

`--run-id` is required and must match an exact bare run directory name.
`latest`, paths, and prefix lookup are refused. `--format` defaults to `jsonl`
and accepts only `jsonl`. `--out` defaults to `-` for stdout. A file output
must be new: the command refuses an existing file and has no overwrite flag.
The parent output directory must already exist.
An unresolvable `~user` in `--target` or `--out` is refused with a nonzero exit
and an `error: journal export:` diagnostic, without a traceback or output.

## Time filtering and validation

`--since YYYY-MM-DD` selects events recorded at or after UTC midnight on
that calendar date. A timezone-aware ISO timestamp such as
`2026-07-27T00:30:00Z` or `2026-07-26T20:30:00-04:00` selects the same inclusive
cutoff instant. Invalid dates and timestamps without a timezone are refused.

The command validates the complete original journal before applying the
cutoff or creating output. A missing source, corrupt chain, partial tail,
journal bound violation, or event from another run fails with a nonzero exit
and a diagnostic on stderr. These failures emit no stdout JSONL and create
no output file. An explicitly existing empty journal succeeds with zero rows.

A journal that is still appending can change during the read or end with a
partial tail. Export refuses observed source changes and partial tails.
Rerun against a quiescent journal.

Export keeps original payloads, sequence numbers, event IDs, and digests.
An unfiltered export can be read by `read_journal_bounded` with the same event
digests. Filtering preserves original sequence order without renumbering or
recomputing digests. Timestamps can be non-monotonic, so a filtered projection
can contain sequence gaps. It needs the original journal context to verify
its chain links and cannot claim standalone full-chain verification.

## Source and output boundaries

The command does not create source run directories or change source journals
or archives. Traversal holds no-follow directory descriptors,
including ancestors, and checks the run directory identity against the
shared binding. Ordinary target navigation such as `../workspace` works.
Each directory is checked before collapsing `..`, so `link/../workspace`
refuses a symlink rather than skipping it. POSIX ancestor names can contain
colon or backslash. Exact run-id validation remains stricter.

The journal is opened relative to the bound events directory with no-follow
semantics and POSIX nonblocking flags. Export checks the actual open file's
type, identity, size, modification time, and change time against the initial
source metadata. It copies at most the observed size, within the shared byte
bound, then checks descriptor and named-source metadata before closing the
source file. Each named-source check reopens the currently named events
directory from the held run root with no-follow semantics, compares its
identity against the original held events directory, and reads journal
metadata through that fresh descriptor. Validation reads only the private
copy, so later source changes cannot change the bytes being validated.
After validation, export checks named-source metadata against the initial
open file's metadata and rechecks that the run directory still has its
original named identity.
Observed events-directory replacement, file replacement, truncation, growth,
or metadata changes fail before output. Export does not lock writers.
Changes after the final metadata observation remain possible, and the output represents a validated
snapshot observed during export.

Validation creates a temporary directory in the configured temporary storage,
which can be within the workspace. It reuses the bounded journal reader on
a temporary plaintext copy.
On POSIX, the copy has mode `0600` inside a temporary directory with mode
`0700`. The file and directory are removed on success or failure. Cleanup
does not promise secure erasure or removal after process termination. The
copy contains the original sensitive payloads. Choose a protected temporary
location as well as a protected output location. On Windows, temporary
storage follows inherited ACLs. The shared directory binding currently
requests writable directory access, so a read-only ACL or share can make
export unavailable even though export does not write to the source. Windows
retains the existing handle APIs and containment checks. Symlinks and reparse
points are refused. A host without the required no-follow primitives fails
closed.

File creation is exclusive and requests mode `0600` on POSIX. On Windows,
file access follows the destination directory's ACL, which this command does
not configure. Choose a directory restricted to the intended readers. A file
write failure can leave an incomplete new output file. Stdout is managed by
the caller, whose redirection sets storage permissions and whose downstream
consumer can interrupt the stream.

## Integrity and privacy limits

Payloads are exported without redaction and may contain sensitive data.
Choose storage and recipients with authority to hold that data. Export does
not grant access, establish custody, or set a retention policy.

Journal digests check unsigned local integrity. An actor who can rewrite the
whole chain can produce another internally consistent journal. Export adds
no signature, trusted timestamp, external anchor, or batch manifest, and it
does not establish who performed the recorded actions. OTLP transport is
outside this slice.

This journal export is the residual slice of #1407. The previously shipped
receipt trailers, evidence packages, and acknowledgement work retain their
own contracts. Retention (#1621) and prune (#1622) are separately assigned.
The parent issue remains open.
