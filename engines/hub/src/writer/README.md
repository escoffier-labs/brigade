# Hub writer API

S4a adds a library API. The transport binary still proxies every route to
Python. S5 will submit domain commands and connect their wakeups to
`Command::post_commit`. S4b owns readers, schema checks and maintenance.

`Writer::start` opens an existing database, without DDL. Its private opener
sets WAL, FULL (and verifies level 2), foreign keys, zero automatic checkpoint
and a statement cache. It syncs the parent directory after the first BEGIN,
so a newly created WAL has a durable directory entry before any answer.
Every reopened connection gets these settings and passes `quick_check`.

Commands use `ReadTx` and `WriteTx`, run all decision reads and credential
checks inside their transaction, and have no external side effects. They must
be replayable. SQL belongs to the domain implementation. Commands must never
issue transaction control, DDL or connection pragmas. A command may run again
once after SQLite aborts its batch. Wakeups belong in `post_commit`, which
receives whether `data_version` changed since the previous batch. Callback
panics are contained. The transport will encode `Failure.status` and
`Failure.reason`. S4a does not add HTTP handlers.

Admission holds slot and payload permits through COMMIT. The defaults are
4,096 slots, 512 reserved for durable commands, and 64 MiB. Non-durable
admission refuses excess work with 503. Durable admission waits until its
deadline and uses 504 for admission and command timeouts. Storage failures
still use 503. Payload estimates must include retained command and reply
allocations. S5 must keep ingress byte permits until admission finishes.
requests waiting for a durable slot still own their input buffers.

Batches have no linger. Limits are 256 commands, 4,096 events, 8 MiB and 50 ms
of apply time. A command is not split. The time budget stops the next command.
the command deadline also interrupts long SQLite statements. COMMIT is never
interrupted by a request deadline. A caller disconnect does not cancel a
transaction that has started.

Pass one shared `Arbiter` to the hub writer and the future memory authority
writer. It alternates turns when both wait, gives queued durable hub work
priority, and meters memory residence with a 250 ms bucket that refills at
250 ms per second. Hub writes do not use the bucket. Memory must acquire its
memory transaction first, then the hub turn, and release the turn only after
ending the hub transaction. Call `Turn::lock_acquired` after the hub BEGIN
succeeds, or `Turn::lock_failed` when no hub lock was acquired. The memory authority protocol belongs to S13a.

# Verification

The default Cargo tests cover savepoints, real SQLITE_FULL, COMMIT failure,
command deadlines, batch budgets, reserved admission, three-times-capacity
RSS growth, the memory bucket and a Python transaction held for three seconds.
`tests/writer_front.rs` repeats the lock case with the managed front running.
Set `PYTHONPATH=src` from the repository root for Python subprocesses.

The two storage fault tests are ignored by default. The power-cut matrix
runs 100 operation cut points in seven phases, in both directory modes, at
FULL and NORMAL. It models whole loss, sector prefixes, reordered sectors,
partial sync, unsynced sizes and directory entries, and discards shared
memory. Its external acknowledgement ledger is outside VFS-managed storage.
It checks integrity, acknowledged rows, message IDs, fence generations and
multi-table atomicity. NORMAL is a negative control and must lose acknowledged
writes. The SIGKILL test starts and kills 20 writer subprocesses and compares
recovered rows against an external ledger.

Run these separately from ordinary PR jobs because they perform thousands of
fault recoveries and many filesystem syncs. Do not reduce their schedules for
CI. From the repository root, use these bare Brigade commands:

```sh
brigade work verify run --target . --argv-json '["env","PYTHONPATH=src","cargo","test","--locked","--manifest-path","engines/hub/Cargo.toml","--lib","writer::powercut::power_cut_matrix","--","--ignored","--exact","--nocapture"]' --timeout 900 --capture brigade-work
brigade work verify run --target . --argv-json '["cargo","test","--locked","--manifest-path","engines/hub/Cargo.toml","--lib","writer::tests::kill_nine_twenty_runs","--","--ignored","--exact","--nocapture"]' --timeout 180 --capture brigade-work
```

The VFS models Rust storage only. Python uses its own SQLite library. These
checks do not replace the design's manual VM reset that covers both writers.
