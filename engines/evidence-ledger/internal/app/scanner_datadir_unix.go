//go:build unix

package app

import (
	"errors"
	"fmt"
	"os"
	"syscall"
)

// openDataDirDescriptor opens the data directory itself without following a
// final-component symlink, refuses anything that is not a directory owned by
// the current uid (#1201 round 4), and returns the raw held descriptor.
// Callers own the descriptor: subsequent child operations bound to it can
// never be redirected by renaming or replacing the directory's pathname
// afterwards (#1201 round 5).
//
// On linux, children are opened and unlinked strictly relative to this
// descriptor (openat/unlinkat; see scanner_datadir_linux.go). On darwin and
// the other non-linux unix platforms the portable syscall package exposes
// neither primitive, so children are opened by pathname with the held
// descriptor's identity re-checked around every operation
// (scanner_datadir_nolinux.go).
func openDataDirDescriptor(dirPath string) (int, error) {
	dfd, err := syscall.Open(dirPath, syscall.O_RDONLY|syscall.O_DIRECTORY|syscall.O_NOFOLLOW|syscall.O_CLOEXEC, 0)
	if err != nil {
		if errors.Is(err, syscall.ELOOP) || errors.Is(err, syscall.ENOTDIR) {
			return -1, fmt.Errorf("data directory %s refused (symlink or not a directory): %w", dirPath, err)
		}
		return -1, fmt.Errorf("data directory %s could not be opened: %w", dirPath, err)
	}
	var st syscall.Stat_t
	if err := syscall.Fstat(dfd, &st); err != nil {
		_ = syscall.Close(dfd)
		return -1, fmt.Errorf("data directory %s could not be stated: %w", dirPath, err)
	}
	if st.Mode&syscall.S_IFMT != syscall.S_IFDIR {
		_ = syscall.Close(dfd)
		return -1, fmt.Errorf("data directory %s is not a directory", dirPath)
	}
	if uid := os.Getuid(); st.Uid != uint32(uid) {
		_ = syscall.Close(dfd)
		return -1, fmt.Errorf("data directory %s owned by uid %d, running uid %d", dirPath, st.Uid, uid)
	}
	return dfd, nil
}
