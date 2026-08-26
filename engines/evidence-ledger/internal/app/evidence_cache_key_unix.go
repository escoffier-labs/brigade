//go:build unix

package app

import (
	"fmt"
	"io/fs"
	"os"
	"syscall"
)

// evidenceMACKeyCreateNoFollow refuses to create over a symlink at the key
// path, so a same-UID attacker cannot interpose between creation and first
// load (#1201).
const evidenceMACKeyCreateNoFollow = syscall.O_NOFOLLOW

// defaultEvidenceMACKeyPlatformSupported reports that unix provides the
// race-free no-follow open (O_NOFOLLOW) and the uid ownership check the MAC
// key trust requirements are built on.
func defaultEvidenceMACKeyPlatformSupported() bool { return true }

// checkEvidenceMACKeyOwner rejects a key file the current uid does not own.
func checkEvidenceMACKeyOwner(info fs.FileInfo) error {
	st, ok := info.Sys().(*syscall.Stat_t)
	if !ok {
		return &EvidenceMACKeyError{Reason: "cannot determine key file owner"}
	}
	if uid := os.Getuid(); st.Uid != uint32(uid) {
		return &EvidenceMACKeyError{Reason: fmt.Sprintf("owned by uid %d, running uid %d", st.Uid, uid)}
	}
	return nil
}
