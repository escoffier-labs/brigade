//go:build unix

package safeio

import (
	"fmt"
	"path/filepath"
	"syscall"
)

// fsyncParent opens dir without following a final-component symlink, verifies
// it is a directory, and fsyncs it. Mirrors run_journal._fsync_directory.
func fsyncParent(dir string) error {
	if !supportsDirectoryFsync() {
		return nil
	}
	flags := syscall.O_RDONLY | syscall.O_DIRECTORY | syscall.O_NOFOLLOW
	fd, err := syscall.Open(dir, flags, 0)
	if err != nil {
		if err == syscall.ELOOP || err == syscall.ENOTDIR {
			// Linux reports ENOTDIR (not ELOOP) for O_DIRECTORY|O_NOFOLLOW on a
			// symlinked directory; treat both as symlink refusal.
			return fmt.Errorf("refusing symlinked path: %s", filepath.Base(dir))
		}
		return err
	}
	defer func() { _ = syscall.Close(fd) }()

	var st syscall.Stat_t
	if err := syscall.Fstat(fd, &st); err != nil {
		return err
	}
	if st.Mode&syscall.S_IFMT != syscall.S_IFDIR {
		return fmt.Errorf("parent is not a directory: %s", filepath.Base(dir))
	}
	return syscall.Fsync(fd)
}
