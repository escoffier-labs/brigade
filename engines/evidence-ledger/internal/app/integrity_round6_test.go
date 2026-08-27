package app

import (
	"context"
	"errors"
	"os"
	"runtime"
	"testing"
	"time"
)

// Round-6 sendback tests (#1201/#1205 security review, round 6).

// Round 6 LOW finding: the round-5 rename-over test only swaps the snapshot
// pathname; it would still pass if the per-exec digest re-read were deleted,
// because descriptor-based exec runs the verified inode no matter what sits
// at the path. This test attacks the held snapshot inode itself: after
// verification and before exec, the bytes of the SAME file are rewritten in
// place (no rename — device/inode preserved), which is exactly what only the
// digest re-read through the held descriptor can catch.
func TestStationTrailSnapshotInPlaceRewriteBeforeExecIsRefusedByDigestReread(t *testing.T) {
	if runtime.GOOS != "linux" {
		t.Skip("descriptor-based exec of the verified snapshot requires linux /proc/self/fd")
	}
	withTempHome(t)
	runOK(t, "init")
	bin := openApprovedShimBinary(t, "echo ORIGINAL_ARTIFACT_RAN")

	requireExecutes(t, bin, "ORIGINAL_ARTIFACT_RAN", "EVIL_ARTIFACT_RAN")
	if bin.snapPath == "" {
		t.Fatal("first exec did not materialize a snapshot")
	}
	before, err := os.Stat(bin.snapPath)
	if err != nil {
		t.Fatal(err)
	}

	// Same-uid attacker rewrites the held snapshot's bytes in place after
	// verification and before the next exec: make the owner-writable bit
	// writable again and truncate-and-write through a plain open-by-path.
	// No rename happens, so the entry must keep its exact identity.
	if err := os.Chmod(bin.snapPath, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(bin.snapPath, []byte("#!/bin/sh\necho EVIL_ARTIFACT_RAN\n"), 0o700); err != nil {
		t.Fatal(err)
	}
	after, err := os.Stat(bin.snapPath)
	if err != nil {
		t.Fatal(err)
	}
	idBefore, ok := scannerFileIdentity(before)
	if !ok {
		t.Fatal("could not determine pre-rewrite snapshot identity")
	}
	idAfter, ok := scannerFileIdentity(after)
	if !ok {
		t.Fatal("could not determine post-rewrite snapshot identity")
	}
	if idBefore != idAfter {
		t.Fatalf("test setup failed: rewrite was not in place (identity %s became %s)", idBefore, idAfter)
	}

	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	cmd, err := bin.command(ctx, "--version")
	if err == nil {
		t.Fatal("staging accepted a snapshot whose held inode was rewritten in place; the per-exec digest re-read must refuse it")
	}
	var mismatch *stationTrailSnapshotMismatchError
	if !errors.As(err, &mismatch) {
		t.Fatalf("refusal is not the digest mismatch error: %v", err)
	}
	if mismatch.Got == mismatch.Want || mismatch.Got == "" || mismatch.Want == "" {
		t.Fatalf("digest mismatch error carries wrong hashes: got %q want %q", mismatch.Got, mismatch.Want)
	}
	if cmd != nil && cmd.Process != nil {
		t.Fatal("a process was staged despite the refused verification")
	}
}
