package app

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"time"
)

// stationTrailBinary is the once-resolved stationtrail executable. Round 4
// of the #1201/#1204 sendback: comparing identity metadata (device/inode on
// unix, size/mtime elsewhere) between hash time and exec time was bypassable
// — an in-place rewrite keeps device and inode, both samples can be taken
// after a rewrite, and a pathname swap in the window between verification and
// cmd.Start escapes entirely. The binding is now the hashed content itself:
// the bytes read through the pinned descriptor are copied once into an
// immutable private snapshot (fresh 0700 directory created mkdirat-style
// under the validated data-directory descriptor, O_EXCL-created 0500 file,
// fsynced), and every exec runs through the snapshot's held descriptor.
//
// Round 5 (#1201): executing the snapshot by pathname still left a same-uid
// window — a rename over the cached snapshot entry between verification and
// exec changed what launched, and once materialized the cached snapPath made
// later commands skip every digest check. The verified snapshot is now kept
// open: its digest is re-read from that open descriptor immediately before
// each exec (the cached pathname is never trusted), and on platforms with a
// descriptor-exec primitive the command targets /proc/self/fd/<fd> so the
// kernel executes exactly the verified inode regardless of any rename,
// replacement, or rewrite in the snapshot directory. Platforms without
// descriptor exec fail closed with a typed error instead of degrading to
// pathname execution.
//
// Residual: none known for the hash-to-exec binding; scannerFileIdentity
// remains only as a cheap early tamper signal with no security weight. A
// same-uid writer with write access to the data directory is outside this
// engine's trust model (#1093).
type stationTrailBinary struct {
	path string // originally resolved absolute path (diagnostics only)
	f    *os.File

	pinned []byte // executable bytes read through the pinned descriptor
	sum    string // lowercase sha256 hex of pinned

	mu            sync.Mutex
	snapDirHandle *stationTrailSnapshotDir // held dir + parent descriptors
	snapDir       string                   // absolute path of the snapshot directory (diagnostics/tests)
	snapPath      string                   // absolute path of the immutable 0500 snapshot file (diagnostics/tests)
	snapFileName  string                   // basename within the held directory
	snapFile      *os.File                 // held open descriptor to the verified snapshot inode
	pinClosed     bool                     // pinned descriptor close is idempotent across retries
}

// stationTrailSnapshotDir holds the two descriptors backing one snapshot:
// fd is the opened 0700 snapshot directory itself; dataDirFd is the validated
// parent (the data directory) it was created under. Cleanup unlinks the file
// relative to fd and removes the directory relative to dataDirFd, so neither
// step can be redirected by swapping pathnames afterwards.
type stationTrailSnapshotDir struct {
	dataDir   string
	dataDirFd int
	path      string
	name      string
	fd        int
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

// stationTrailDescriptorExecError reports that this platform cannot execute a
// file through an already-held open descriptor. Rather than degrade to
// pathname execution — which would reopen the verification-to-exec window —
// the engine refuses to stage the scanner at all (#1201 round 5).
type stationTrailDescriptorExecError struct {
	GOOS   string
	Reason string
}

func (e *stationTrailDescriptorExecError) Error() string {
	return fmt.Sprintf("descriptor-based execution of the verified stationtrail snapshot is not supported on %s (%s); refusing to fall back to pathname execution", e.GOOS, e.Reason)
}

// stationTrailSnapshotMismatchError reports that the held snapshot descriptor
// no longer carries the approved digest immediately before an exec.
type stationTrailSnapshotMismatchError struct {
	Got  string
	Want string
}

func (e *stationTrailSnapshotMismatchError) Error() string {
	return fmt.Sprintf("stationtrail snapshot no longer matches the hashed artifact (sha256 %s, want %s); refusing to execute", e.Got, e.Want)
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
// directory, so executed scanner bytes do not outlive the handle. Both the
// pinned-descriptor close error and the removal error are preserved (joined),
// and snapshot state survives a failed removal so it can be retried (#1201
// round 5).
func (b *stationTrailBinary) Close() error {
	b.mu.Lock()
	defer b.mu.Unlock()
	var closeErr error
	if !b.pinClosed {
		closeErr = b.f.Close()
		b.pinClosed = true
	}
	rmErr := b.removeSnapshotLocked()
	if closeErr == nil && rmErr == nil {
		return nil
	}
	return errors.Join(closeErr, rmErr)
}

// pin reads the executable exactly once through the pinned descriptor with a
// size bound (#1204) and remembers those bytes plus their digest. Approval,
// verification, and every later exec are bound to this one byte sequence:
// re-reading the descriptor would let an in-place rewrite change what runs
// without changing its identity metadata.
func (b *stationTrailBinary) pin() ([]byte, string, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.pinLocked()
}

func (b *stationTrailBinary) pinLocked() ([]byte, string, error) {
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

// ensureSnapshotLocked materializes the pinned executable bytes exactly once
// as an immutable private copy behind a held open descriptor: a fresh 0700
// directory created relative to the validated data-directory descriptor, an
// O_EXCL|O_NOFOLLOW-created 0500 non-writable file, fsynced, its digest
// verified from that same open descriptor against the digest of the exact
// bytes that were hashed — fail-closed on any mismatch (#1201 rounds 4-5).
func (b *stationTrailBinary) ensureSnapshotLocked() error {
	if b.snapFile != nil {
		return nil
	}
	if _, _, err := b.pinLocked(); err != nil {
		return err
	}
	sd, err := makePrivateSnapshotDirAt(ResolvePaths().DataDir)
	if err != nil {
		return fmt.Errorf("stationtrail snapshot directory could not be created: %w", err)
	}
	b.snapDirHandle = sd
	b.snapDir = sd.path
	name := "stationtrail"
	if runtime.GOOS == "windows" {
		name += ".exe"
	}
	b.snapFileName = name
	fail := func(err error) error {
		if dropErr := b.dropSnapshot(); dropErr != nil {
			return errors.Join(err, fmt.Errorf("snapshot rollback: %w", dropErr))
		}
		return err
	}
	f, err := createSnapshotFileAt(sd, name)
	if err != nil {
		return fail(fmt.Errorf("stationtrail snapshot could not be created exclusively under %s: %w", sd.path, err))
	}
	if _, err := f.Write(b.pinned); err != nil {
		_ = f.Close()
		return fail(fmt.Errorf("stationtrail snapshot could not be written: %w", err))
	}
	if err := f.Sync(); err != nil {
		_ = f.Close()
		return fail(fmt.Errorf("stationtrail snapshot could not be synced: %w", err))
	}
	got, err := sha256HexFromSeekable(f)
	if err != nil {
		_ = f.Close()
		return fail(fmt.Errorf("stationtrail snapshot could not be verified: %w", err))
	}
	if got != b.sum {
		_ = f.Close()
		return fail(&stationTrailSnapshotMismatchError{Got: got, Want: b.sum})
	}
	// Release the write handle (linux refuses execve of a write-opened file)
	// and bind the held execution descriptor through a no-follow reopen
	// relative to the held directory descriptor. The digest is verified again
	// across that handoff so nothing can interpose between the two
	// descriptors (#1201 round 5).
	if err := f.Close(); err != nil {
		return fail(fmt.Errorf("stationtrail snapshot could not be sealed: %w", err))
	}
	execFile, err := reopenSnapshotFileAt(sd, name)
	if err != nil {
		return fail(fmt.Errorf("stationtrail snapshot %s could not be reopened no-follow: %w", filepath.Join(sd.path, name), err))
	}
	got, err = sha256HexFromSeekable(execFile)
	if err != nil {
		_ = execFile.Close()
		return fail(fmt.Errorf("stationtrail snapshot could not be verified after reopening: %w", err))
	}
	if got != b.sum {
		_ = execFile.Close()
		return fail(&stationTrailSnapshotMismatchError{Got: got, Want: b.sum})
	}
	b.snapFile = execFile
	b.snapPath = filepath.Join(sd.path, name)
	return nil
}

// sha256HexFromSeekable digests a bounded read of an open seekable
// descriptor, rewinding it to the start first so a descriptor positioned at
// EOF (for example right after writing the snapshot bytes) cannot silently
// hash zero bytes.
func sha256HexFromSeekable(f *os.File) (string, error) {
	if _, err := f.Seek(0, io.SeekStart); err != nil {
		return "", err
	}
	data, err := readAllBounded(f, maxScannerBinaryDigestBytes)
	if err != nil {
		return "", err
	}
	sum := sha256.Sum256(data)
	return hex.EncodeToString(sum[:]), nil
}

// verifySnapshotIntactLocked re-verifies, from the OPEN snapshot descriptor,
// that the bytes about to execute still hash to the approved digest. This
// runs immediately before every exec; the cached pathname is never trusted
// (#1201 round 5).
func (b *stationTrailBinary) verifySnapshotIntactLocked() error {
	if b.snapFile == nil {
		return errors.New("stationtrail snapshot is not materialized")
	}
	got, err := sha256HexFromSeekable(b.snapFile)
	if err != nil {
		return fmt.Errorf("stationtrail snapshot could not be re-verified: %w", err)
	}
	if got != b.sum {
		return &stationTrailSnapshotMismatchError{Got: got, Want: b.sum}
	}
	return nil
}

// removeSnapshotLocked performs descriptor-backed cleanup: the snapshot file
// is unlinked relative to the held snapshot-directory descriptor and the
// directory itself relative to the held validated data-directory descriptor.
// Every failure is preserved and snapshot state is retained so the removal
// can be retried; state clears only after a fully successful removal (#1201
// round 5).
func (b *stationTrailBinary) removeSnapshotLocked() error {
	sd := b.snapDirHandle
	if sd == nil {
		return nil
	}
	var errs []error
	if b.snapFile != nil {
		if err := b.snapFile.Close(); err != nil {
			errs = append(errs, fmt.Errorf("stationtrail snapshot descriptor could not be closed: %w", err))
		}
		b.snapFile = nil
	}
	if err := unlinkSnapshotFileAt(sd, b.snapFileName); err != nil {
		errs = append(errs, fmt.Errorf("stationtrail snapshot %s could not be removed: %w", b.snapPath, err))
	}
	if err := rmdirSnapshotDirAt(sd); err != nil {
		errs = append(errs, fmt.Errorf("stationtrail snapshot directory %s could not be removed: %w", sd.path, err))
	}
	if len(errs) > 0 {
		// Keep the handles and names: a later Close retries the removal.
		return errors.Join(errs...)
	}
	fdErr := closeDescriptor(sd.fd)
	parentErr := closeDescriptor(sd.dataDirFd)
	b.snapDirHandle = nil
	b.snapDir = ""
	b.snapPath = ""
	b.snapFileName = ""
	if fdErr != nil || parentErr != nil {
		return errors.Join(fdErr, parentErr)
	}
	return nil
}

// dropSnapshot rolls back a failed materialization. Unlike earlier rounds the
// removal error is surfaced to the caller instead of suppressed (#1201 round
// 5).
func (b *stationTrailBinary) dropSnapshot() error {
	return b.removeSnapshotLocked()
}

// command stages an exec.Cmd targeting the held, just-re-verified snapshot
// descriptor: on linux the command path is /proc/self/fd/<fd>, which the
// kernel resolves to exactly the verified inode at exec time — immune to
// renames or replacements inside the snapshot directory. Callers own the
// returned command.
func (b *stationTrailBinary) command(ctx context.Context, args ...string) (*exec.Cmd, error) {
	if err := b.verifyUnchangedBeforeExec(); err != nil {
		return nil, err
	}
	if !stationTrailDescriptorExecSupported() {
		return nil, &stationTrailDescriptorExecError{GOOS: runtime.GOOS, Reason: "no /proc/self/fd descriptor exec"}
	}
	b.mu.Lock()
	defer b.mu.Unlock()
	if _, _, err := b.pinLocked(); err != nil {
		return nil, err
	}
	if err := b.ensureSnapshotLocked(); err != nil {
		return nil, err
	}
	if err := b.verifySnapshotIntactLocked(); err != nil {
		return nil, err
	}
	execPath, err := snapshotExecPath(b.snapFile)
	if err != nil {
		return nil, err
	}
	cmd := exec.CommandContext(ctx, execPath, args...)
	cmd.Args[0] = filepath.Base(b.snapFileName)
	return cmd, nil
}

// snapshotExecPath returns the kernel-visible path that executes through the
// held snapshot descriptor. The descriptor stays open (with CLOEXEC already
// cleared at creation) so the forked child inherits it across execve.
func snapshotExecPath(f *os.File) (string, error) {
	fd := int(f.Fd())
	if _, err := os.Stat("/proc/self/fd"); err != nil {
		return "", &stationTrailDescriptorExecError{GOOS: runtime.GOOS, Reason: "/proc/self/fd unavailable"}
	}
	return fmt.Sprintf("/proc/self/fd/%d", fd), nil
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
