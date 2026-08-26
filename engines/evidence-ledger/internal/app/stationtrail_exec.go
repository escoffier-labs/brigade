package app

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"time"
)

// stationTrailBinary is the once-resolved stationtrail executable. Round 4
// of the #1201/#1204 sendback: comparing identity metadata (device/inode on
// unix, size/mtime elsewhere) between hash time and exec time was bypassable
// — an in-place rewrite keeps device and inode, both samples can be taken
// after a rewrite, and a pathname swap in the window between verification and
// cmd.Start escapes entirely. The binding is now the hashed content itself:
// the bytes read through the pinned descriptor are copied once into an
// immutable private snapshot (fresh 0700 directory under the data directory,
// O_EXCL-created 0500 file, fsynced), the copy's digest is verified against
// the digest computed over those exact bytes, and every exec runs the
// snapshot's absolute path. Whatever happens afterwards to the resolved PATH
// entry — swap, replace, or in-place rewrite — cannot change what executes.
// The snapshot is reused across all commands of this handle and removed when
// the handle is closed.
//
// Residual: none known for the hash-to-exec binding; scannerFileIdentity
// remains only as a cheap early tamper signal with no security weight.
type stationTrailBinary struct {
	path string // originally resolved absolute path (diagnostics only)
	f    *os.File

	pinned   []byte // executable bytes read through the pinned descriptor
	sum      string // lowercase sha256 hex of pinned
	snapDir  string // private 0700 directory holding the executed snapshot
	snapPath string // absolute path of the immutable 0500 snapshot file
}

// stationTrailLookPath is the executable-resolution seam (tests substitute a
// controlled resolver).
var stationTrailLookPath = exec.LookPath

// scannerBinaryIdentityError reports that the stationtrail executable under
// the resolved path no longer matches the opened artifact (#1201 round 3).
// Since round 4 this is only a cheap early tamper signal: execution is bound
// to the private snapshot, not to the resolved path.
type scannerBinaryIdentityError struct {
	Path string
}

func (e *scannerBinaryIdentityError) Error() string {
	return fmt.Sprintf("stationtrail executable %s changed under its resolved path after hashing; refusing to execute a different artifact", e.Path)
}

// openStationTrailBinary resolves the stationtrail executable once and opens
// it. The returned value is the only execution route for this engine: probes,
// digest calculation, dry-runs, and imports all run through it.
func openStationTrailBinary() (*stationTrailBinary, error) {
	resolved, err := stationTrailLookPath("stationtrail")
	if err != nil {
		return nil, err
	}
	abs, err := filepath.Abs(resolved)
	if err != nil {
		return nil, err
	}
	f, err := os.Open(abs)
	if err != nil {
		return nil, err
	}
	info, statErr := f.Stat()
	if statErr == nil && !info.Mode().IsRegular() {
		statErr = fmt.Errorf("resolved stationtrail %q is not a regular file", abs)
	}
	if statErr != nil {
		_ = f.Close()
		return nil, statErr
	}
	return &stationTrailBinary{path: abs, f: f}, nil
}

// Close releases the pinned descriptor and removes the private snapshot
// directory, so executed scanner bytes do not outlive the handle.
func (b *stationTrailBinary) Close() error {
	err := b.f.Close()
	if b.snapDir != "" {
		if rmErr := os.RemoveAll(b.snapDir); rmErr != nil && err == nil {
			err = rmErr
		}
		b.snapDir = ""
		b.snapPath = ""
	}
	return err
}

// pin reads the executable exactly once through the pinned descriptor with a
// size bound (#1204) and remembers those bytes plus their digest. Approval,
// verification, and every later exec are bound to this one byte sequence:
// re-reading the descriptor would let an in-place rewrite change what runs
// without changing its identity metadata.
func (b *stationTrailBinary) pin() ([]byte, string, error) {
	if b.pinned != nil {
		return b.pinned, b.sum, nil
	}
	if _, err := b.f.Seek(0, io.SeekStart); err != nil {
		return nil, "", err
	}
	data, err := readAllBounded(b.f, maxScannerBinaryDigestBytes)
	if err != nil {
		return nil, "", err
	}
	sum := sha256.Sum256(data)
	b.pinned = data
	b.sum = hex.EncodeToString(sum[:])
	return b.pinned, b.sum, nil
}

// digest returns the lowercase SHA-256 hex digest of the pinned executable
// bytes. Hashing the pinned descriptor bytes rather than re-resolving PATH is
// what binds approval to the artifact that later runs.
func (b *stationTrailBinary) digest() (string, error) {
	_, sum, err := b.pin()
	return sum, err
}

// verifyUnchangedBeforeExec is retained only as a cheap early tamper signal:
// it detects a removed-and-replaced executable before staging a command. It
// carries no security weight — an in-place rewrite preserves device/inode on
// unix, and size/mtime elsewhere proves nothing about content — so refusal
// here is defense in depth on top of the snapshot binding below.
func (b *stationTrailBinary) verifyUnchangedBeforeExec() error {
	openInfo, err := b.f.Stat()
	if err == nil {
		var pathInfo os.FileInfo
		pathInfo, err = os.Stat(b.path)
		if err == nil {
			openID, okOpen := scannerFileIdentity(openInfo)
			pathID, okPath := scannerFileIdentity(pathInfo)
			if okOpen && okPath && openID == pathID {
				return nil
			}
			return &scannerBinaryIdentityError{Path: b.path}
		}
	}
	return fmt.Errorf("stationtrail executable %s could not be re-verified before exec: %w", b.path, err)
}

// snapshot materializes the pinned executable bytes as an immutable private
// copy and returns its absolute path. The copy lives in a fresh 0700
// directory under the private data directory (falling back to the OS temp
// directory), is created O_EXCL, fsynced, and chmodded to 0500 — readable and
// executable by the current user but NOT writable — and its digest is
// verified against the digest of the exact bytes that were hashed, fail-closed
// on any mismatch (#1201 round 4).
func (b *stationTrailBinary) snapshot() (string, error) {
	if b.snapPath != "" {
		return b.snapPath, nil
	}
	data, sum, err := b.pin()
	if err != nil {
		return "", err
	}
	dir, err := makePrivateSnapshotDir()
	if err != nil {
		return "", fmt.Errorf("stationtrail snapshot directory could not be created: %w", err)
	}
	b.snapDir = dir
	name := "stationtrail"
	if runtime.GOOS == "windows" {
		name += ".exe"
	}
	path := filepath.Join(dir, name)
	f, err := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0o500)
	if err != nil {
		b.dropSnapshot()
		return "", fmt.Errorf("stationtrail snapshot %s could not be created exclusively: %w", path, err)
	}
	if _, err := f.Write(data); err != nil {
		_ = f.Close()
		b.dropSnapshot()
		return "", fmt.Errorf("stationtrail snapshot %s could not be written: %w", path, err)
	}
	if err := f.Sync(); err != nil {
		_ = f.Close()
		b.dropSnapshot()
		return "", fmt.Errorf("stationtrail snapshot %s could not be synced: %w", path, err)
	}
	// Chmod rather than relying on the creation mode: creation modes are
	// umask-subjected, chmod is not, and the executed artifact must end up
	// non-writable even under a hostile umask.
	if err := f.Chmod(0o500); err != nil {
		_ = f.Close()
		b.dropSnapshot()
		return "", fmt.Errorf("stationtrail snapshot %s could not be made immutable: %w", path, err)
	}
	if err := f.Close(); err != nil {
		b.dropSnapshot()
		return "", fmt.Errorf("stationtrail snapshot %s could not be sealed: %w", path, err)
	}
	got, err := fileSHA256Hex(path)
	if err != nil {
		b.dropSnapshot()
		return "", fmt.Errorf("stationtrail snapshot %s could not be verified: %w", path, err)
	}
	if got != sum {
		b.dropSnapshot()
		return "", fmt.Errorf("stationtrail snapshot %s does not match the hashed artifact (sha256 %s, want %s); refusing to execute", path, got, sum)
	}
	b.snapPath = path
	return path, nil
}

// dropSnapshot removes the snapshot directory after a failed materialization.
func (b *stationTrailBinary) dropSnapshot() {
	if b.snapDir != "" {
		_ = os.RemoveAll(b.snapDir)
	}
	b.snapDir = ""
	b.snapPath = ""
}

// makePrivateSnapshotDir creates a fresh 0700 directory for executable
// snapshots, preferring the private data directory and falling back to the OS
// temp directory when that has not been initialized yet.
func makePrivateSnapshotDir() (string, error) {
	dir, err := os.MkdirTemp(ResolvePaths().DataDir, "stationtrail-snapshot-")
	if err == nil {
		return dir, nil
	}
	return os.MkdirTemp("", "stationtrail-snapshot-")
}

func fileSHA256Hex(path string) (string, error) {
	f, err := os.Open(path)
	if err != nil {
		return "", err
	}
	defer f.Close()
	data, err := readAllBounded(f, maxScannerBinaryDigestBytes)
	if err != nil {
		return "", err
	}
	sum := sha256.Sum256(data)
	return hex.EncodeToString(sum[:]), nil
}

// command stages an exec.Cmd targeting the immutable private snapshot of the
// hashed bytes. Callers own the returned command.
func (b *stationTrailBinary) command(ctx context.Context, args ...string) (*exec.Cmd, error) {
	if err := b.verifyUnchangedBeforeExec(); err != nil {
		return nil, err
	}
	snap, err := b.snapshot()
	if err != nil {
		return nil, err
	}
	return exec.CommandContext(ctx, snap, args...), nil
}

// runBounded runs a stationtrail subcommand of THIS binary with a timeout
// and hard output caps (former package-level runStationTrailBounded, bound
// to the single resolution). A deadline, read-limit, start, or wait failure
// is returned as an error; a nonzero exit is returned as
// *stationTrailCommandError so the caller can inspect the scanner's output.
func (b *stationTrailBinary) runBounded(args []string, timeout time.Duration, maxOutput int64) ([]byte, error) {
	ctx, cancel := context.WithTimeout(context.Background(), timeout)
	defer cancel()
	cmd, err := b.command(ctx, args...)
	if err != nil {
		return nil, err
	}
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		return nil, err
	}
	// #1205: scanner stderr is capped so a flooding scanner cannot drive an
	// unbounded engine-side allocation through its own diagnostics.
	stderr := &cappedWriter{limit: maxScannerStderrBytes}
	cmd.Stderr = stderr
	if err := cmd.Start(); err != nil {
		return nil, err
	}
	out, readErr := readAllBounded(stdout, maxOutput)
	waitErr := cmd.Wait()
	if ctx.Err() == context.DeadlineExceeded {
		return nil, fmt.Errorf("stationtrail %s timed out after %s", strings.Join(args, " "), timeout)
	}
	if readErr != nil {
		return nil, readErr
	}
	if waitErr != nil {
		return out, &stationTrailCommandError{Stdout: string(out), Stderr: stderr.String()}
	}
	return out, nil
}
