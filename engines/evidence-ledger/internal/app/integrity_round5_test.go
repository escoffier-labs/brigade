package app

import (
	"bytes"
	"context"
	"errors"
	"io/fs"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"
)

// Round-5 sendback tests (#1201/#1205 security review, round 5).

// openApprovedShimBinary is the common round-5 setup: a shim stationtrail on
// PATH, opened through the engine's single-resolution handle, with its digest
// approved in the owner-protected allowlist file.
func openApprovedShimBinary(t *testing.T, marker string) *stationTrailBinary {
	t.Helper()
	dir := t.TempDir()
	_, digest := writeShimScript(t, dir, marker)
	t.Setenv("PATH", dir)
	bin, err := openStationTrailBinary()
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = bin.Close() })
	hashed, err := bin.digest()
	if err != nil {
		t.Fatal(err)
	}
	if hashed != digest {
		t.Fatalf("digest over the opened descriptor %q does not match the file content digest %q", hashed, digest)
	}
	writeStationTrailApprovalsFile(t, hashed+"\n", 0o600)
	return bin
}

// requireExecutes runs one bounded command through the handle and asserts the
// verified marker ran while the forbidden one did not.
func requireExecutes(t *testing.T, bin *stationTrailBinary, want, forbidden string) string {
	t.Helper()
	out, err := bin.runBounded([]string{"--version"}, 30*time.Second, stationTrailCapsMaxOutput)
	if err != nil {
		t.Fatalf("verified snapshot could not be run: %v", err)
	}
	got := string(out)
	if strings.Contains(got, forbidden) {
		t.Fatalf("forbidden artifact output leaked into execution: %q", got)
	}
	if !strings.Contains(got, want) {
		t.Fatalf("verified bytes did not run; got %q", got)
	}
	return got
}

// stageCommand stages one exec through the handle.
func stageCommand(t *testing.T, bin *stationTrailBinary) *exec.Cmd {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	t.Cleanup(cancel)
	cmd, err := bin.command(ctx, "--version")
	if err != nil {
		t.Fatal(err)
	}
	return cmd
}

// Round 5 HIGH finding: after digest re-verification the snapshot was still
// executed by pathname. A same-uid process can rename another executable over
// the snapshot entry through the writable-by-owner snapshot directory in the
// window between verification and exec, so the executed inode is no longer
// the verified one.
func TestStationTrailSnapshotRenamedOverBeforeExecRunsVerifiedBytes(t *testing.T) {
	if runtime.GOOS != "linux" {
		t.Skip("descriptor-based exec of the verified snapshot requires linux /proc/self/fd")
	}
	withTempHome(t)
	runOK(t, "init")
	bin := openApprovedShimBinary(t, "echo ORIGINAL_ARTIFACT_RAN")

	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	cmd, err := bin.command(ctx, "--version")
	if err != nil {
		t.Fatal(err)
	}
	if bin.snapPath == "" {
		t.Fatal("staging a command did not materialize a verified snapshot")
	}

	// Same-uid attacker renames its own executable over the snapshot entry
	// after verification and before cmd.Start.
	evil := filepath.Join(t.TempDir(), "evil-stationtrail")
	if err := os.WriteFile(evil, []byte("#!/bin/sh\necho EVIL_ARTIFACT_RAN\n"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.Rename(evil, bin.snapPath); err != nil {
		t.Fatal(err)
	}

	stdout, err := cmd.StdoutPipe()
	if err != nil {
		t.Fatal(err)
	}
	stderr := &bytes.Buffer{}
	cmd.Stderr = stderr
	if err := cmd.Start(); err != nil {
		t.Fatalf("verified snapshot could not be started after the rename-over attack: %v", err)
	}
	out, readErr := readAllBounded(stdout, stationTrailCapsMaxOutput)
	if waitErr := cmd.Wait(); waitErr != nil {
		t.Fatalf("staged command failed: %v (stderr: %s)", waitErr, stderr.String())
	}
	if readErr != nil {
		t.Fatal(readErr)
	}
	if strings.Contains(string(out), "EVIL_ARTIFACT_RAN") {
		t.Fatal("a rename over the snapshot entry between verification and exec changed what executed; exec must bind to the held descriptor")
	}
	if !strings.Contains(string(out), "ORIGINAL_ARTIFACT_RAN") {
		t.Fatalf("verified bytes did not run after the rename-over attack; got %q", string(out))
	}
}

// Round 5 HIGH finding: the cached snapPath made every later command skip the
// digest check entirely — once materialized, whatever sat at the cached path
// was trusted for the rest of the handle's life.
func TestStationTrailCachedSnapshotPathIsReverifiedOnEveryExec(t *testing.T) {
	if runtime.GOOS != "linux" {
		t.Skip("descriptor-based exec of the verified snapshot requires linux /proc/self/fd")
	}
	withTempHome(t)
	runOK(t, "init")
	bin := openApprovedShimBinary(t, "echo ORIGINAL_ARTIFACT_RAN")

	// First exec materializes and caches the snapshot path.
	requireExecutes(t, bin, "ORIGINAL_ARTIFACT_RAN", "EVIL_ARTIFACT_RAN")
	if bin.snapPath == "" {
		t.Fatal("first exec did not materialize a snapshot")
	}

	// Attacker swaps the entry at the cached path afterwards.
	evil := filepath.Join(t.TempDir(), "evil-stationtrail")
	if err := os.WriteFile(evil, []byte("#!/bin/sh\necho EVIL_ARTIFACT_RAN\n"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.Rename(evil, bin.snapPath); err != nil {
		t.Fatal(err)
	}

	// A later command must not trust the cached path: it re-verifies from the
	// held descriptor and executes those exact bytes.
	requireExecutes(t, bin, "ORIGINAL_ARTIFACT_RAN", "EVIL_ARTIFACT_RAN")
}

// Round 5 HIGH finding: snapshot creation followed a symlinked data directory,
// and when creation under the data directory failed it silently fell back to
// the OS temp directory (attacker-influenceable via TMPDIR).
func TestStationTrailSnapshotRefusesSymlinkedDataDirWithoutTempFallback(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("posix symlink semantics")
	}
	withTempHome(t)
	runOK(t, "init")
	bin := openApprovedShimBinary(t, "echo SNAPSHOT_PROBE_OK")

	dataDir := ResolvePaths().DataDir
	fake := t.TempDir()
	controlledTmp := t.TempDir()
	t.Setenv("TMPDIR", controlledTmp)

	real := dataDir + ".attacker-displaced"
	if err := os.Rename(dataDir, real); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		_ = os.Remove(dataDir)
		_ = os.Rename(real, dataDir)
	})
	if err := os.Symlink(fake, dataDir); err != nil {
		t.Fatal(err)
	}

	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	if _, err := bin.command(ctx, "--version"); err == nil {
		t.Fatal("snapshot creation followed a symlinked data directory instead of refusing")
	}

	fakeEntries, err := os.ReadDir(fake)
	if err != nil {
		t.Fatal(err)
	}
	for _, entry := range fakeEntries {
		if strings.HasPrefix(entry.Name(), "stationtrail-snapshot-") {
			t.Fatalf("snapshot directory was created inside the symlink target %s", fake)
		}
	}
	tmpEntries, err := os.ReadDir(controlledTmp)
	if err != nil {
		t.Fatal(err)
	}
	if len(tmpEntries) != 0 {
		t.Fatalf("snapshot creation silently fell back to the OS temp directory (%d entries left behind)", len(tmpEntries))
	}
}

// Round 5 MEDIUM finding: after the safe directory-relative ENOENT, missing-key
// creation returned to the absolute pathname. A data directory swapped into
// that pathname in the window between the ENOENT and the creation received
// the fresh key material — or an attacker-planted candidate file at that
// pathname was adopted as the create-race "winner". The swap is interposed
// deterministically via the after-miss probe seam.
func TestEvidenceMACKeyCreationAfterENOENTRefusesSwappedPathPlantedKey(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("posix rename/uid/mode semantics")
	}
	withTempHome(t)
	runOK(t, "init")
	keyPath := evidenceBundleMACKeyPath()
	if err := os.Remove(keyPath); err != nil && !errors.Is(err, fs.ErrNotExist) {
		t.Fatal(err)
	}

	dataDir := ResolvePaths().DataDir
	swapInAttackerDir(t, dataDir, keyPath)

	got, err := loadOrCreateEvidenceBundleMACKey()
	if err != nil {
		t.Fatalf("missing-key creation failed: %v", err)
	}
	planted := bytes.Repeat([]byte{0xAB}, 32)
	if bytes.Equal(got, planted) {
		t.Fatal("creation adopted an attacker-planted key found at the swapped pathname")
	}
	onReal, err := os.ReadFile(filepath.Join(dataDir+".held-real", evidenceBundleMACKeyFile))
	if err != nil {
		t.Fatalf("key was not created in the validated directory that produced the ENOENT: %v", err)
	}
	if !bytes.Equal(onReal, got) {
		t.Fatal("returned key differs from the key created behind the held descriptor")
	}
	onFake, err := os.ReadFile(keyPath)
	if err != nil || !bytes.Equal(onFake, planted) {
		t.Fatalf("attacker-planted file at the swapped pathname must stay untouched, got %q (%v)", onFake, err)
	}
}

// Round 5 MEDIUM finding, landing variant: with no attacker-planted file, the
// created key itself must materialize inside the validated directory that
// produced the ENOENT — never behind the mutable absolute pathname.
func TestEvidenceMACKeyCreationAfterENOENTDoesNotFollowSwappedPath(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("posix rename/uid/mode semantics")
	}
	withTempHome(t)
	runOK(t, "init")
	keyPath := evidenceBundleMACKeyPath()
	if err := os.Remove(keyPath); err != nil && !errors.Is(err, fs.ErrNotExist) {
		t.Fatal(err)
	}

	dataDir := ResolvePaths().DataDir
	swapInEmptyDir(t, dataDir)

	got, err := loadOrCreateEvidenceBundleMACKey()
	if err != nil {
		t.Fatalf("missing-key creation failed: %v", err)
	}
	if len(got) != 32 {
		t.Fatalf("created key is %d bytes, want 32", len(got))
	}
	if _, err := os.Stat(keyPath); !errors.Is(err, fs.ErrNotExist) {
		t.Fatalf("creation followed the swapped pathname instead of the validated descriptor: %v", err)
	}
	onReal, err := os.ReadFile(filepath.Join(dataDir+".held-real", evidenceBundleMACKeyFile))
	if err != nil {
		t.Fatalf("key did not land in the validated data directory: %v", err)
	}
	if !bytes.Equal(onReal, got) {
		t.Fatal("on-disk key in the validated directory differs from the returned key")
	}
}

// swapInEmptyDir displaces the data directory mid-call: the after-miss probe
// renames the validated directory away and plants a fresh attacker directory
// at its pathname before exclusive creation runs.
func swapInEmptyDir(t *testing.T, dataDir string) {
	t.Helper()
	evidenceMACKeyAfterMissProbe = func() { plantSwappedDataDir(t, dataDir) }
	t.Cleanup(func() { evidenceMACKeyAfterMissProbe = nil })
}

// swapInAttackerDir behaves like swapInEmptyDir but pre-plants an owned,
// mode-0600 candidate key in the swapped-in directory.
func swapInAttackerDir(t *testing.T, dataDir, keyPath string) {
	t.Helper()
	evidenceMACKeyAfterMissProbe = func() {
		plantSwappedDataDir(t, dataDir)
		planted := bytes.Repeat([]byte{0xAB}, 32)
		if err := os.WriteFile(keyPath, planted, 0o600); err != nil {
			t.Fatal(err)
		}
	}
	t.Cleanup(func() { evidenceMACKeyAfterMissProbe = nil })
}

func plantSwappedDataDir(t *testing.T, dataDir string) {
	t.Helper()
	held := dataDir + ".held-real"
	if err := os.Rename(dataDir, held); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		_ = os.RemoveAll(dataDir)
		_ = os.Rename(held, dataDir)
	})
	if err := os.MkdirAll(dataDir, 0o700); err != nil {
		t.Fatal(err)
	}
}

// Round 5 LOW finding: Close discarded the removal error whenever the pinned-
// descriptor close also failed, cleared snapshot state regardless, and
// dropSnapshot suppressed removal errors entirely.
func TestStationTrailClosePreservesBothErrorsAndRetainsStateOnFailedRemoval(t *testing.T) {
	if runtime.GOOS != "linux" {
		t.Skip("descriptor-backed cleanup semantics")
	}
	withTempHome(t)
	runOK(t, "init")
	bin := openApprovedShimBinary(t, "echo CLEANUP_PROBE_OK")
	stageCommand(t, bin)
	if bin.snapDir == "" || bin.snapPath == "" {
		t.Fatal("staging a command did not materialize a snapshot")
	}

	// Obstruct the removal: an extra file makes the snapshot directory
	// non-empty, so removing it must fail.
	junk := filepath.Join(bin.snapDir, "stuck-junk")
	if err := os.WriteFile(junk, []byte("x"), 0o600); err != nil {
		t.Fatal(err)
	}
	// And force the pinned-descriptor close to fail too.
	if err := bin.f.Close(); err != nil {
		t.Fatal(err)
	}

	closeErr := bin.Close()
	if closeErr == nil {
		t.Fatal("Close discarded the failed snapshot removal error")
	}
	msg := closeErr.Error()
	if !strings.Contains(msg, "already closed") {
		t.Fatalf("Close dropped the pinned-descriptor close error: %q", msg)
	}
	if !strings.Contains(msg, "could not be removed") {
		t.Fatalf("Close dropped the snapshot removal error: %q", msg)
	}
	if bin.snapDir == "" || bin.snapPath == "" {
		t.Fatal("Close cleared snapshot state although the removal failed")
	}
	if _, err := os.Stat(bin.snapDir); err != nil {
		t.Fatalf("failed removal deleted the snapshot directory anyway: %v", err)
	}
}

// Round 5 LOW finding, retry variant: after a failed removal the handle keeps
// its state so the removal can complete once the obstruction is gone.
func TestStationTrailCloseRemovalRetriesAfterObstructionClears(t *testing.T) {
	if runtime.GOOS != "linux" {
		t.Skip("descriptor-backed cleanup semantics")
	}
	withTempHome(t)
	runOK(t, "init")
	bin := openApprovedShimBinary(t, "echo CLEANUP_RETRY_OK")
	stageCommand(t, bin)

	junk := filepath.Join(bin.snapDir, "stuck-junk")
	if err := os.WriteFile(junk, []byte("x"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := bin.Close(); err == nil {
		t.Fatal("Close succeeded although the snapshot directory could not be fully removed")
	}
	if bin.snapDir == "" {
		t.Fatal("snapshot state cleared despite the failed removal")
	}

	if err := os.Remove(junk); err != nil {
		t.Fatal(err)
	}
	if err := bin.Close(); err != nil {
		t.Fatalf("retry after clearing the obstruction failed: %v", err)
	}
	if _, err := os.Stat(filepath.Dir(junk)); !errors.Is(err, fs.ErrNotExist) {
		t.Fatalf("snapshot directory survived the successful removal: %v", err)
	}
	if bin.snapDir != "" || bin.snapPath != "" {
		t.Fatal("snapshot state not cleared after the successful removal")
	}
}
