//go:build !linux

package app

import (
	"os"
	"runtime"
)

// Descriptor-based execution of the verified snapshot requires a way to
// execute through an already-held open descriptor (linux /proc/self/fd/<fd>).
// Platforms without that primitive must fail closed rather than degrade to
// pathname execution, which would reopen the verification-to-exec swap window
// the snapshot exists to close (#1201 round 5).

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

// closeDescriptor closes a raw held descriptor; none are ever held here.
func closeDescriptor(fd int) error {
	_ = fd
	return nil
}
