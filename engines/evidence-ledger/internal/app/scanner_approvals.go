package app

import (
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"strings"
)

// Operator approval of legacy stationtrail executables moved off the
// inherited environment in round 3 of the #1201/#1204 hardening: any child
// process — including the scanner being gated — can read its parent's
// environment, so MISELEDGER_STATIONTRAIL_APPROVED_DIGESTS published the
// allowlist to the very binaries it gates. The allowlist now lives in an
// owner-protected file under the private data directory and is validated on
// every load exactly like the evidence bundle MAC key: regular file, owned by
// the current uid, mode 0600, bounded read, opened without following a single
// symlink on either hop — the data directory itself is opened with
// O_DIRECTORY|O_NOFOLLOW (unix; refused closed elsewhere) and verified to be
// current-uid owned before the file is opened openat-style relative to that
// handle (#1201 round 4). A missing file simply means no approvals are
// configured; every other refusal is a typed error that fails the probe
// closed.
const (
	evidenceScannerApprovalsFile = "stationtrail-approved-digests"
	maxApprovedDigestsFileBytes  = 64 << 10
)

// StationTrailApprovedDigestsError reports an approved-digests file that was
// refused on load (#1201/#1204 round 3): wrong type, owner, or mode, a
// symlink, a redirected data directory, an oversized read, or any malformed
// digest token.
type StationTrailApprovedDigestsError struct {
	Reason string
}

func (e *StationTrailApprovedDigestsError) Error() string {
	return fmt.Sprintf("stationtrail approved digests rejected: %s", e.Reason)
}

func stationTrailApprovedDigestsPath() string {
	return filepath.Join(ResolvePaths().DataDir, evidenceScannerApprovalsFile)
}

// loadStationTrailApprovedDigests returns the operator-approved legacy
// scanner digests from <DataDir>/stationtrail-approved-digests. A missing
// file yields an empty allowlist; every other problem is refused with a
// typed *StationTrailApprovedDigestsError so callers fail closed.
func loadStationTrailApprovedDigests() (map[string]bool, error) {
	f, err := openStationTrailApprovedDigests()
	if err != nil {
		if errors.Is(err, fs.ErrNotExist) {
			return map[string]bool{}, nil
		}
		return nil, &StationTrailApprovedDigestsError{Reason: fmt.Sprintf("refusing to open approved-digests file: %v", err)}
	}
	defer f.Close()
	return validateApprovedDigestsFile(f)
}

// openStationTrailApprovedDigests binds the allowlist file handle without
// following symlinks on any path component and without keeping a redirectable
// pathname between open and read (#1201 round 4).
func openStationTrailApprovedDigests() (*os.File, error) {
	return openDataDirFile(ResolvePaths().DataDir, evidenceScannerApprovalsFile)
}

// validateApprovedDigestsFile validates an already-opened allowlist handle:
// regular file, current-uid owner, mode 0600, bounded read, strict parsing.
func validateApprovedDigestsFile(f *os.File) (map[string]bool, error) {
	info, err := f.Stat()
	if err != nil {
		return nil, &StationTrailApprovedDigestsError{Reason: fmt.Sprintf("stat approved-digests file: %v", err)}
	}
	if !info.Mode().IsRegular() {
		return nil, &StationTrailApprovedDigestsError{Reason: fmt.Sprintf("not a regular file (mode %s)", info.Mode())}
	}
	if err := checkEvidenceMACKeyOwner(info); err != nil {
		var keyErr *EvidenceMACKeyError
		reason := err.Error()
		if errors.As(err, &keyErr) {
			reason = keyErr.Reason
		}
		return nil, &StationTrailApprovedDigestsError{Reason: reason}
	}
	if perm := info.Mode().Perm(); perm != 0o600 {
		return nil, &StationTrailApprovedDigestsError{Reason: fmt.Sprintf("mode %04o, want 0600", perm)}
	}
	data, err := readAllBounded(f, maxApprovedDigestsFileBytes)
	if err != nil {
		return nil, &StationTrailApprovedDigestsError{Reason: err.Error()}
	}
	return parseStationTrailApprovedDigests(data)
}

// parseStationTrailApprovedDigests accepts one SHA-256 hex digest per line,
// case-normalized, with an optional `sha256:` prefix. Blank lines are
// skipped; ANY malformed token refuses the whole list so a truncated or
// tampered file can never leave a partial allowlist behind (#1204 round 3).
func parseStationTrailApprovedDigests(data []byte) (map[string]bool, error) {
	approved := make(map[string]bool)
	for i, line := range strings.Split(string(data), "\n") {
		token := strings.TrimPrefix(strings.ToLower(strings.TrimSpace(line)), "sha256:")
		if token == "" {
			continue
		}
		if len(token) != 64 || !isLowercaseHex(token) {
			return nil, &StationTrailApprovedDigestsError{Reason: fmt.Sprintf("line %d: token is not a 64-hex sha256 digest", i+1)}
		}
		approved[token] = true
	}
	return approved, nil
}

func isLowercaseHex(s string) bool {
	for _, r := range s {
		switch {
		case r >= '0' && r <= '9':
		case r >= 'a' && r <= 'f':
		default:
			return false
		}
	}
	return true
}
