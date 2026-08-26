//go:build unix

package app

import (
	"fmt"
	"io/fs"
	"syscall"
)

// scannerFileIdentity names the mounted file (device, inode) backing a Stat
// result. Since #1201 round 4 this is only a cheap early tamper signal: an
// in-place rewrite keeps device and inode, so it proves nothing about file
// content and carries no security weight. The hash-to-exec binding is the
// immutable private snapshot executed by absolute path — see
// stationTrailBinary.
func scannerFileIdentity(info fs.FileInfo) (string, bool) {
	st, ok := info.Sys().(*syscall.Stat_t)
	if !ok {
		return "", false
	}
	return fmt.Sprintf("%d:%d", uint64(st.Dev), uint64(st.Ino)), true
}
