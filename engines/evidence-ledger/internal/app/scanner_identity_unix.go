//go:build unix

package app

import (
	"fmt"
	"io/fs"
	"syscall"
)

// scannerFileIdentity names the mounted file (device, inode) backing a Stat
// result, so hash-time and exec-time can prove they observed the same
// artifact (#1201 round 3).
func scannerFileIdentity(info fs.FileInfo) (string, bool) {
	st, ok := info.Sys().(*syscall.Stat_t)
	if !ok {
		return "", false
	}
	return fmt.Sprintf("%d:%d", uint64(st.Dev), uint64(st.Ino)), true
}
