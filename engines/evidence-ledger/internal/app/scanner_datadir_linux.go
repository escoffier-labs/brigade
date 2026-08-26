//go:build linux

package app

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"syscall"
	"unsafe"
)

// openDataDirFile opens <dirPath>/<name> without ever walking a symlinked
// final component on either hop (#1201 round 4): the directory itself is
// opened with O_DIRECTORY|O_NOFOLLOW (so a symlinked or non-directory data
// directory is refused outright), verified to be a directory owned by the
// current uid, and the file is then opened openat-style relative to that
// directory handle with O_NOFOLLOW. Holding the directory handle also closes
// the swap window: renaming or replacing the directory after this open cannot
// redirect an already-bound descriptor. A missing file surfaces as
// fs.ErrNotExist; every other problem is a descriptive refusal.
func openDataDirFile(dirPath, name string) (*os.File, error) {
	dfd, err := openDataDirDescriptor(dirPath)
	if err != nil {
		return nil, err
	}
	fd, err := syscall.Openat(dfd, name, syscall.O_RDONLY|syscall.O_NOFOLLOW|syscall.O_CLOEXEC, 0)
	_ = syscall.Close(dfd)
	if err != nil {
		if errors.Is(err, syscall.ELOOP) {
			return nil, fmt.Errorf("%s under %s refused: path is a symlink", name, dirPath)
		}
		return nil, err // ENOENT and friends propagate as fs.ErrNotExist
	}
	return os.NewFile(uintptr(fd), filepath.Join(dirPath, name)), nil
}

// unlinkatRaw wraps the flags-taking unlinkat (the portable syscall package
// exposes inconsistent arities across platforms).
func unlinkatRaw(dirFd int, name string, flags int) error {
	p, err := syscall.ByteSliceFromString(name)
	if err != nil {
		return err
	}
	if _, _, errno := syscall.Syscall(syscall.SYS_UNLINKAT, uintptr(dirFd), uintptr(unsafe.Pointer(&p[0])), uintptr(flags)); errno != 0 {
		return errno
	}
	return nil
}
