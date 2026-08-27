//go:build unix && !linux

package app

import (
	"os"
	"runtime"
	"syscall"
)

// Non-linux unix platforms (darwin included) have no descriptor-relative
// directory creation, no close-on-exec control, and — without linux
// /proc/self/fd — no way to execute through an already-held open descriptor.
// Snapshot materialization therefore fails closed here exactly as on the
// other non-linux platforms; unlike them, validated data-directory
// descriptors ARE held for key storage and private-file loading
// (scanner_datadir_nolinux.go), so closeDescriptor must really close.

// stationTrailDescriptorExecSupported reports whether this platform can
// execute a file through an already-held open descriptor.
func stationTrailDescriptorExecSupported() bool { return false }

// makePrivateSnapshotDirAt fails closed on platforms without descriptor-based
// execution; no snapshot is ever materialized there.
func makePrivateSnapshotDirAt(dataDir string) (*stationTrailSnapshotDir, error) {
	_ = dataDir
	return nil, &stationTrailDescriptorExecError{GOOS: runtime.GOOS, Reason: "no descriptor-relative directory creation"}
}

// createSnapshotFileAt is unreachable where descriptor exec is unsupported.
func createSnapshotFileAt(sd *stationTrailSnapshotDir, name string) (*os.File, error) {
	_, _ = sd, name
	return nil, &stationTrailDescriptorExecError{GOOS: runtime.GOOS, Reason: "no descriptor-relative file creation"}
}

// reopenSnapshotFileAt is unreachable where descriptor exec is unsupported.
func reopenSnapshotFileAt(sd *stationTrailSnapshotDir, name string) (*os.File, error) {
	_, _ = sd, name
	return nil, &stationTrailDescriptorExecError{GOOS: runtime.GOOS, Reason: "no descriptor-relative reopen"}
}

// clearCloseOnExec is unreachable where descriptor exec is unsupported.
func clearCloseOnExec(fd int) error {
	_ = fd
	return &stationTrailDescriptorExecError{GOOS: runtime.GOOS, Reason: "no close-on-exec control"}
}

// unlinkSnapshotFileAt is unreachable where no snapshot was materialized.
func unlinkSnapshotFileAt(sd *stationTrailSnapshotDir, name string) error {
	_, _ = sd, name
	return &stationTrailDescriptorExecError{GOOS: runtime.GOOS, Reason: "no descriptor-relative unlink"}
}

// rmdirSnapshotDirAt is unreachable where no snapshot was materialized.
func rmdirSnapshotDirAt(sd *stationTrailSnapshotDir) error {
	_ = sd
	return &stationTrailDescriptorExecError{GOOS: runtime.GOOS, Reason: "no descriptor-relative rmdir"}
}

// closeDescriptor closes a raw held descriptor.
func closeDescriptor(fd int) error {
	if fd < 0 {
		return nil
	}
	return syscall.Close(fd)
}
