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
	"strings"
	"time"
)

// stationTrailBinary is the once-resolved stationtrail executable. Round 3
// of the #1201/#1204 sendback: the capabilities probe, version probe, digest
// calculation, and import each resolved the executable independently, so a
// writable PATH entry could present an approved binary for hashing and a
// different one for execution. Every stationtrail subprocess now goes
// through exactly one resolved absolute path backed by an opened descriptor:
// the approval digest is computed from that descriptor, and every exec is
// preceded by an immediate re-verification that the file at the resolved
// path is still the opened artifact (device/inode on unix; size plus mtime
// elsewhere).
//
// Residual: an in-place rewrite that preserves the compared identity
// attributes between verification and exec is not detected — installing
// stationtrail in a root-owned directory remains the operator mitigation.
type stationTrailBinary struct {
	path string // resolved absolute path used for every exec
	f    *os.File
}

// stationTrailLookPath is the executable-resolution seam (tests substitute a
// controlled resolver).
var stationTrailLookPath = exec.LookPath

// scannerBinaryIdentityError reports that the stationtrail executable under
// the resolved path changed identity after it was opened and hashed (#1201
// round 3).
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

// Close releases the pinned descriptor.
func (b *stationTrailBinary) Close() error { return b.f.Close() }

// digest returns the lowercase SHA-256 hex digest of the opened executable,
// read through the pinned descriptor with a size bound (#1204). Hashing the
// descriptor rather than re-resolving PATH is what binds approval to the
// artifact that later runs.
func (b *stationTrailBinary) digest() (string, error) {
	if _, err := b.f.Seek(0, io.SeekStart); err != nil {
		return "", err
	}
	data, err := readAllBounded(b.f, maxScannerBinaryDigestBytes)
	if err != nil {
		return "", err
	}
	sum := sha256.Sum256(data)
	return hex.EncodeToString(sum[:]), nil
}

// verifyUnchangedBeforeExec confirms immediately before an exec that the
// file now present at the resolved path is still the opened artifact.
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

// command stages an exec.Cmd for the resolved absolute path after the
// pre-exec identity verification. Callers own the returned command.
func (b *stationTrailBinary) command(ctx context.Context, args ...string) (*exec.Cmd, error) {
	if err := b.verifyUnchangedBeforeExec(); err != nil {
		return nil, err
	}
	return exec.CommandContext(ctx, b.path, args...), nil
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
