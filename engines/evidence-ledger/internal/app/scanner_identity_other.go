//go:build !unix

package app

import (
	"fmt"
	"io/fs"
)

// No stable device/inode pair is exposed by the portable stdlib on this
// platform; size plus nanosecond mtime is a weaker stand-in that still
// detects swapped or replaced executables in practice. The residual — an
// in-place rewrite preserving size and mtime between verification and exec —
// is documented on stationTrailBinary (#1201 round 3).
func scannerFileIdentity(info fs.FileInfo) (string, bool) {
	return fmt.Sprintf("%d:%d", info.Size(), info.ModTime().UnixNano()), true
}
