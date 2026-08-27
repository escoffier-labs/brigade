//go:build unix && !linux

package app

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"syscall"
)

// Non-linux unix platforms (darwin included) get neither openat nor unlinkat
// from the portable syscall package, so the descriptor-relative primitives
// behind the linux guarantees do not exist there. This file keeps the engine
// functional on macOS with a strictly weaker pathname-based equivalent:
//
//   - the child is opened by pathname with O_NOFOLLOW|O_CLOEXEC relative to
//     the already-validated data-directory path (O_CREAT|O_EXCL where
//     creation is required);
//   - the validated data-directory descriptor is fstat'ed before and after
//     every open or removal and must keep an identical dev/ino pair, so a
//     directory swapped in wholesale is detected;
//   - the opened file must be a regular, singly linked (nlink == 1),
//     exactly-0600 file.
//
// Residual window (#1201 round 6): between those two parent identity checks
// the child operation resolves by pathname, so entries inside the data
// directory can be mutated in that instant. Exploiting this requires a
// same-uid process with write access to the data directory — precisely the
// writer that is outside this engine's model (#1093) — so the residual risk
// is documented and accepted rather than mitigated here.

// dataDirIdentity is the device/inode pair of a held data-directory
// descriptor; comparing before and after a pathname-based child operation
// detects the directory itself being displaced mid-operation.
type dataDirIdentity struct {
	dev uint64
	ino uint64
}

func fstatDataDirIdentity(fd int) (dataDirIdentity, error) {
	var st syscall.Stat_t
	if err := syscall.Fstat(fd, &st); err != nil {
		return dataDirIdentity{}, err
	}
	return dataDirIdentity{dev: uint64(st.Dev), ino: uint64(st.Ino)}, nil
}

// openDataDirFile opens <dirPath>/<name> for reading without following a
// symlinked final component and with the parent-identity checks described in
// the file comment. A missing file surfaces as fs.ErrNotExist; every other
// problem is a descriptive refusal. The mode/nlink/type requirements mirror
// what every consumer of this helper validates on load (the approved-digests
// and evidence bundle MAC key files are owner-private 0600 regular files).
func openDataDirFile(dirPath, name string) (*os.File, error) {
	dfd, err := openDataDirDescriptor(dirPath)
	if err != nil {
		return nil, err
	}
	defer func() { _ = syscall.Close(dfd) }()
	before, err := fstatDataDirIdentity(dfd)
	if err != nil {
		return nil, fmt.Errorf("data directory %s could not be stated before %s open: %w", dirPath, name, err)
	}
	f, err := os.OpenFile(filepath.Join(dirPath, name), os.O_RDONLY|syscall.O_NOFOLLOW|syscall.O_CLOEXEC, 0)
	if err != nil {
		if errors.Is(err, syscall.ELOOP) {
			return nil, fmt.Errorf("%s under %s refused: path is a symlink", name, dirPath)
		}
		return nil, err // ENOENT and friends propagate as fs.ErrNotExist
	}
	fail := func(err error) (*os.File, error) {
		_ = f.Close()
		return nil, err
	}
	after, err := fstatDataDirIdentity(dfd)
	if err != nil {
		return fail(fmt.Errorf("data directory %s could not be restated after %s open: %w", dirPath, name, err))
	}
	if after != before {
		return fail(fmt.Errorf("data directory %s changed identity (%d:%d then %d:%d) during the pathname-based %s open; refusing", dirPath, before.dev, before.ino, after.dev, after.ino, name))
	}
	info, err := f.Stat()
	if err != nil {
		return fail(fmt.Errorf("%s under %s could not be stated: %w", name, dirPath, err))
	}
	st, ok := info.Sys().(*syscall.Stat_t)
	if !ok {
		return fail(fmt.Errorf("%s under %s refused: cannot determine file type and link count", name, dirPath))
	}
	if !info.Mode().IsRegular() {
		return fail(fmt.Errorf("%s under %s refused: not a regular file", name, dirPath))
	}
	if st.Nlink != 1 {
		return fail(fmt.Errorf("%s under %s refused: hard-linked (nlink = %d)", name, dirPath, st.Nlink))
	}
	if perm := info.Mode().Perm(); perm != 0o600 {
		return fail(fmt.Errorf("%s under %s refused: mode %04o, want 0600", name, dirPath, perm))
	}
	return f, nil
}

// removeUnderValidatedDataDir unlinks <dirPath>/<name> by pathname (no
// unlinkat on these platforms) around a re-check of the held validated
// data-directory descriptor's identity, per the file-comment contract.
func removeUnderValidatedDataDir(dfd int, dirPath, name string) error {
	before, err := fstatDataDirIdentity(dfd)
	if err != nil {
		return fmt.Errorf("data directory %s could not be stated before removing %s: %w", dirPath, name, err)
	}
	if err := os.Remove(filepath.Join(dirPath, name)); err != nil {
		return err
	}
	after, err := fstatDataDirIdentity(dfd)
	if err != nil {
		return fmt.Errorf("data directory %s could not be restated after removing %s: %w", dirPath, name, err)
	}
	if after != before {
		return fmt.Errorf("data directory %s changed identity (%d:%d then %d:%d) while removing %s; refusing to trust the outcome", dirPath, before.dev, before.ino, after.dev, after.ino, name)
	}
	return nil
}
