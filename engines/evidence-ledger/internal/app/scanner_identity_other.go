//go:build !unix

package app

import (
	"fmt"
	"io/fs"
)

// The portable stdlib on this platform exposes no stable device/inode pair,
// so this stand-in compares size plus nanosecond mtime. It is ONLY a cheap
// early tamper signal with no security weight: it proves nothing about file
// content — an in-place rewrite changes both samples, and two samples taken
// after a rewrite always match — so it does not bind anything. The actual
// hash-to-exec binding is the same on every platform: exec runs an immutable,
// private, 0500 byte-for-byte snapshot of the hashed descriptor bytes; see
// stationTrailBinary (#1201 round 4).
func scannerFileIdentity(info fs.FileInfo) (string, bool) {
	return fmt.Sprintf("%d:%d", info.Size(), info.ModTime().UnixNano()), true
}
