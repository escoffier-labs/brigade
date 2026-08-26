package app

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"io/fs"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"
)

// Round-4 sendback tests (#1201/#1204 security review, round 4).

// Round 4 HIGH finding: the hash-to-exec binding compared device/inode (unix)
// or size/mtime (elsewhere) between the opened descriptor and a fresh stat of
// the resolved path. An in-place rewrite keeps the device and inode, so the
// comparison passes while the bytes under the resolved path are attacker
// controlled. The engine must run the hashed bytes, not whatever currently
// sits at the resolved path.
func TestStationTrailInPlaceRewriteAfterHashingRunsOriginalBytes(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("sh shim test requires a posix shell")
	}
	withTempHome(t)
	runOK(t, "init")
	dir := t.TempDir()
	_, digest := writeShimScript(t, dir, "echo ORIGINAL_ARTIFACT_RAN")
	t.Setenv("PATH", dir)
	bin, err := openStationTrailBinary()
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { bin.Close() })
	hashed, err := bin.digest()
	if err != nil {
		t.Fatal(err)
	}
	if hashed != digest {
		t.Fatalf("digest over the opened descriptor %q does not match the file content digest %q", hashed, digest)
	}
	writeStationTrailApprovalsFile(t, hashed+"\n", 0o600)

	// Attacker rewrites the executable IN PLACE after hashing: same device,
	// same inode, so the pre-exec identity comparison still passes.
	path := filepath.Join(dir, "stationtrail")
	if err := os.WriteFile(path, []byte("#!/bin/sh\necho EVIL_ARTIFACT_RAN\n"), 0o755); err != nil {
		t.Fatal(err)
	}
	out, err := bin.runBounded([]string{"--version"}, 10*time.Second, stationTrailCapsMaxOutput)
	if err != nil {
		t.Fatalf("in-place rewrite made the approved binary unrunnable: %v", err)
	}
	if strings.Contains(string(out), "EVIL_ARTIFACT_RAN") {
		t.Fatal("an in-place rewrite after hashing changed what executed; hash-to-exec binding is bypassable")
	}
	if !strings.Contains(string(out), "ORIGINAL_ARTIFACT_RAN") {
		t.Fatalf("hashed artifact did not run after an in-place rewrite; got %q", string(out))
	}
}

// Round 4 HIGH finding: verification and cmd.Start do not execute atomically,
// so a pathname swap in that window replaces what Start launches even though
// the identity check passed moments earlier. The staged command must target a
// private immutable snapshot of the hashed bytes, not the attacker-writable
// resolved path.
func TestStationTrailPathnameSwapBetweenVerifyAndStartRunsOriginalBytes(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("sh shim test requires a posix shell")
	}
	withTempHome(t)
	runOK(t, "init")
	dir := t.TempDir()
	_, digest := writeShimScript(t, dir, "echo ORIGINAL_ARTIFACT_RAN")
	t.Setenv("PATH", dir)
	bin, err := openStationTrailBinary()
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { bin.Close() })
	hashed, err := bin.digest()
	if err != nil {
		t.Fatal(err)
	}
	if hashed != digest {
		t.Fatalf("digest over the opened descriptor %q does not match the file content digest %q", hashed, digest)
	}
	writeStationTrailApprovalsFile(t, hashed+"\n", 0o600)

	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	cmd, err := bin.command(ctx, "--version")
	if err != nil {
		t.Fatal(err)
	}
	if cmd.Path == filepath.Join(dir, "stationtrail") {
		t.Fatalf("staged command executes the mutable resolved path %q; a swap between verification and Start escapes", cmd.Path)
	}

	// Attacker swaps the pathname between verification and cmd.Start.
	resolved := filepath.Join(dir, "stationtrail")
	if err := os.Remove(resolved); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(resolved, []byte("#!/bin/sh\necho EVIL_ARTIFACT_RAN\n"), 0o755); err != nil {
		t.Fatal(err)
	}

	stdout, err := cmd.StdoutPipe()
	if err != nil {
		t.Fatal(err)
	}
	stderr := &bytes.Buffer{}
	cmd.Stderr = stderr
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	out, readErr := readAllBounded(stdout, stationTrailCapsMaxOutput)
	if waitErr := cmd.Wait(); waitErr != nil {
		t.Fatalf("staged command failed: %v (stderr: %s)", waitErr, stderr.String())
	}
	if readErr != nil {
		t.Fatal(readErr)
	}
	if strings.Contains(string(out), "EVIL_ARTIFACT_RAN") {
		t.Fatal("a pathname swap between verification and Start changed what executed")
	}
	if !strings.Contains(string(out), "ORIGINAL_ARTIFACT_RAN") {
		t.Fatalf("hashed artifact did not run after the post-staging swap; got %q", string(out))
	}
}

// Round 4 MEDIUM finding: filepath.Join plus a final-component-only symlink
// refusal follow symlinks in PARENT components, so a DataDir whose last hop is
// a symlink redirects approvals and MAC key loading to an attacker-chosen
// directory holding current-uid 0600 files. Both loaders must refuse.
func TestApprovalsAndMACKeyRefuseSymlinkedDataDir(t *testing.T) {
	withTempHome(t)
	runOK(t, "init")
	dataDir := ResolvePaths().DataDir
	fake := t.TempDir()

	valid := strings.Repeat("ab", 32)
	if err := os.MkdirAll(fake, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(fake, evidenceScannerApprovalsFile), []byte(valid+"\n"), 0o600); err != nil {
		t.Fatal(err)
	}

	// Replace the real data directory with a symlink to the fake one.
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

	approved, err := loadStationTrailApprovedDigests()
	var listErr *StationTrailApprovedDigestsError
	if err == nil {
		if approved[valid] {
			t.Fatal("approved-digests allowlist was loaded through a symlinked data directory")
		}
		t.Fatalf("symlinked data directory was followed without refusal (got %#v)", approved)
	}
	if !errors.As(err, &listErr) {
		t.Fatalf("refusal is not a typed *StationTrailApprovedDigestsError: %v", err)
	}

	key := bytes.Repeat([]byte{0x42}, 32)
	if err := os.WriteFile(filepath.Join(fake, evidenceBundleMACKeyFile), key, 0o600); err != nil {
		t.Fatal(err)
	}
	got, err := loadOrCreateEvidenceBundleMACKey()
	if err == nil && hex.EncodeToString(got) == hex.EncodeToString(key) {
		t.Fatal("evidence bundle MAC key was loaded through a symlinked data directory")
	}
	var keyErr *EvidenceMACKeyError
	if !errors.As(err, &keyErr) {
		t.Fatalf("MAC key refusal is not a typed *EvidenceMACKeyError: %v", err)
	}
}

// Round 4: the executed snapshot must be private (0700 directory), immutable
// (0500, non-writable file), byte-identical to the hashed artifact, and must
// not outlive the binary handle.
func TestStationTrailSnapshotIsPrivateImmutableAndTransient(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("posix permission-bit semantics")
	}
	withTempHome(t)
	runOK(t, "init")
	dir := t.TempDir()
	_, digest := writeShimScript(t, dir, "echo SNAPSHOT_PROBE_OK")
	t.Setenv("PATH", dir)
	bin, err := openStationTrailBinary()
	if err != nil {
		t.Fatal(err)
	}
	hashed, err := bin.digest()
	if err != nil {
		t.Fatal(err)
	}
	if hashed != digest {
		t.Fatalf("digest over the opened descriptor %q does not match the file content digest %q", hashed, digest)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	if _, err := bin.command(ctx, "--version"); err != nil {
		t.Fatal(err)
	}
	snapDir, snapPath := bin.snapDir, bin.snapPath
	if snapDir == "" || snapPath == "" {
		t.Fatal("staging a command did not materialize a private snapshot")
	}

	dirInfo, err := os.Stat(snapDir)
	if err != nil {
		t.Fatal(err)
	}
	if !dirInfo.IsDir() || dirInfo.Mode().Perm() != 0o700 {
		t.Fatalf("snapshot directory mode = %04o, want exactly 0700", dirInfo.Mode().Perm())
	}
	fileInfo, err := os.Stat(snapPath)
	if err != nil {
		t.Fatal(err)
	}
	if !fileInfo.Mode().IsRegular() {
		t.Fatalf("snapshot %s is not a regular file (mode %s)", snapPath, fileInfo.Mode())
	}
	if fileInfo.Mode().Perm() != 0o500 {
		t.Fatalf("snapshot mode = %04o, want exactly 0500 (non-writable)", fileInfo.Mode().Perm())
	}
	body, err := os.ReadFile(snapPath)
	if err != nil {
		t.Fatal(err)
	}
	sum := sha256.Sum256(body)
	if got := hex.EncodeToString(sum[:]); got != hashed {
		t.Fatalf("snapshot bytes sha256 %s do not match the approved digest %s", got, hashed)
	}

	if err := bin.Close(); err != nil {
		t.Fatalf("closing the binary handle failed: %v", err)
	}
	if _, err := os.Stat(snapDir); !errors.Is(err, fs.ErrNotExist) {
		t.Fatalf("snapshot directory survived the handle close: %v", err)
	}
}

// Round 4: once the allowlist handle is bound, swapping or redirecting the
// data directory must not change what is read — validation follows the
// directory-relative descriptor, not the pathname.
func TestApprovalsSwappedDataDirBetweenOpenAndReadIsRefused(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("posix rename/symlink semantics")
	}
	withTempHome(t)
	runOK(t, "init")
	valid := strings.Repeat("cd", 32)
	path := writeStationTrailApprovalsFile(t, valid+"\n", 0o600)

	f, err := openStationTrailApprovedDigests()
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()

	// Swap the data directory out from under the open handle: displace the
	// real one, plant a fresh directory with a DIFFERENT valid allowlist.
	dataDir := ResolvePaths().DataDir
	displaced := dataDir + ".displaced"
	if err := os.Rename(dataDir, displaced); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		_ = os.RemoveAll(dataDir)
		_ = os.Rename(displaced, dataDir)
	})
	other := strings.Repeat("ef", 32)
	if err := os.MkdirAll(dataDir, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(other+"\n"), 0o600); err != nil {
		t.Fatal(err)
	}

	got, err := validateApprovedDigestsFile(f)
	if err != nil {
		t.Fatalf("valid allowlist refused after an unrelated data-directory swap: %v", err)
	}
	if len(got) != 1 || !got[valid] {
		t.Fatalf("handle read picked up swapped content instead of the opened file's bytes: %#v", got)
	}
}
