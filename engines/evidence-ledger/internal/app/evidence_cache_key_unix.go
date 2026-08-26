//go:build unix

package app

import (
	"fmt"
	"io/fs"
	"os"
	"syscall"
)

// defaultEvidenceMACKeyPlatformSupported reports that unix provides the
// no-follow open (O_NOFOLLOW) and the uid ownership check the MAC key trust
// requirements are built on. On linux the key file is opened strictly
// relative to a held validated data-directory descriptor
// (evidence_cache_key_linux.go); on darwin and the other non-linux unix
// platforms the open is pathname-based with parent-identity checks around it
// (evidence_cache_key_nolinux.go).
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

// openValidatedDataDirForAuth opens the data directory for MAC key storage
// through the shared validated-descriptor helper: O_DIRECTORY|O_NOFOLLOW,
// refused unless it is a directory owned by the current uid (#1201 round 4).
// The caller owns the returned descriptor.
func openValidatedDataDirForAuth() (int, error) {
	return openDataDirDescriptor(ResolvePaths().DataDir)
}
