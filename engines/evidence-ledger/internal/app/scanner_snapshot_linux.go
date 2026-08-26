//go:build linux

package app

import (
	"crypto/rand"
	"encoding/hex"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"syscall"
	"unsafe"
)

// atRemovedir is the AT_REMOVEDIR flag for unlinkat (not exported by the
// portable syscall package on every platform).
const atRemovedir = 0x200

// stationTrailDescriptorExecSupported reports that this platform can execute
// a file through an already-held open descriptor (/proc/self/fd/<fd>).
func stationTrailDescriptorExecSupported() bool { return true }

// mkdiratRaw wraps the mkdirat syscall, which the portable syscall package
// does not expose on every build.
func mkdiratRaw(dirFd int, name string, mode uint32) error {
	p, err := syscall.ByteSliceFromString(name)
	if err != nil {
		return err
	}
	if _, _, errno := syscall.Syscall6(syscall.SYS_MKDIRAT, uintptr(dirFd), uintptr(unsafe.Pointer(&p[0])), uintptr(mode), 0, 0, 0); errno != 0 {
		return errno
	}
	return nil
}

// makePrivateSnapshotDirAt creates a fresh 0700 snapshot directory relative
// to the validated data-directory descriptor (#1201 round 5): the parent hop
// is opened O_DIRECTORY|O_NOFOLLOW with a uid check, the directory itself is
// mkdirat'ed under that held descriptor, reopened O_DIRECTORY|O_NOFOLLOW,
// chmodded 0700 (umask-proof), and re-verified. There is deliberately no
// fallback to the OS temp directory: if the validated data directory cannot
// host the snapshot, creation fails closed.
func makePrivateSnapshotDirAt(dataDir string) (*stationTrailSnapshotDir, error) {
	parentFd, err := openDataDirDescriptor(dataDir)
	if err != nil {
		return nil, err
	}
	sd := &stationTrailSnapshotDir{dataDir: dataDir, dataDirFd: parentFd}
	for attempt := 0; attempt < 8; attempt++ {
		suffix := make([]byte, 8)
		if _, err := rand.Read(suffix); err != nil {
			closeDescriptor(parentFd)
			return nil, err
		}
		name := "stationtrail-snapshot-" + hex.EncodeToString(suffix)
		if err := mkdiratRaw(parentFd, name, 0o700); err != nil {
			if errors.Is(err, syscall.EEXIST) {
				continue
			}
			closeDescriptor(parentFd)
			return nil, fmt.Errorf("snapshot directory could not be created: %w", err)
		}
		fd, err := syscall.Openat(parentFd, name, syscall.O_RDONLY|syscall.O_DIRECTORY|syscall.O_NOFOLLOW|syscall.O_CLOEXEC, 0)
		if err != nil {
			closeDescriptor(parentFd)
			_ = unlinkatRaw(parentFd, name, atRemovedir)
			return nil, fmt.Errorf("created snapshot directory %s could not be opened no-follow: %w", filepath.Join(dataDir, name), err)
		}
		sd.name = name
		sd.path = filepath.Join(dataDir, name)
		sd.fd = fd
		if err := validateSnapshotDirAt(sd); err != nil {
			_ = unlinkatRaw(parentFd, name, atRemovedir)
			_ = syscall.Close(fd)
			closeDescriptor(parentFd)
			return nil, err
		}
		return sd, nil
	}
	closeDescriptor(parentFd)
	return nil, errors.New("could not allocate a unique private snapshot directory name")
}

// validateSnapshotDirAt confirms the freshly created snapshot directory is a
// current-uid-owned 0700 directory behind the held descriptor.
func validateSnapshotDirAt(sd *stationTrailSnapshotDir) error {
	var st syscall.Stat_t
	if err := syscall.Fstat(sd.fd, &st); err != nil {
		return fmt.Errorf("snapshot directory %s could not be stated: %w", sd.path, err)
	}
	if st.Mode&syscall.S_IFMT != syscall.S_IFDIR {
		return fmt.Errorf("snapshot directory %s is not a directory", sd.path)
	}
	// Creation modes are umask-subjected; fchmod is not, and the executed
	// artifact's container must end up exactly owner-private.
	if err := syscall.Fchmod(sd.fd, 0o700); err != nil {
		return fmt.Errorf("snapshot directory %s could not be made private: %w", sd.path, err)
	}
	st = syscall.Stat_t{}
	if err := syscall.Fstat(sd.fd, &st); err != nil {
		return fmt.Errorf("snapshot directory %s could not be restated: %w", sd.path, err)
	}
	if perm := st.Mode & 0o777; perm != 0o700 {
		return fmt.Errorf("snapshot directory %s mode = %04o, want exactly 0700", sd.path, perm)
	}
	if uid := os.Getuid(); st.Uid != uint32(uid) {
		return fmt.Errorf("snapshot directory %s owned by uid %d, running uid %d", sd.path, st.Uid, uid)
	}
	return nil
}

// createSnapshotFileAt creates the snapshot file exclusively relative to the
// held snapshot-directory descriptor (O_EXCL|O_NOFOLLOW, so neither a pre-
// planted file nor a symlink at the name can be adopted) and chmods it to
// 0500 (umask-proof). The returned descriptor is a transient read-write
// handle for staging the bytes; it must be closed before execution (linux
// refuses execve of a write-opened file with ETXTBSY).
func createSnapshotFileAt(sd *stationTrailSnapshotDir, name string) (*os.File, error) {
	fd, err := syscall.Openat(sd.fd, name, syscall.O_RDWR|syscall.O_CREAT|syscall.O_EXCL|syscall.O_NOFOLLOW|syscall.O_CLOEXEC, 0o500)
	if err != nil {
		return nil, err
	}
	f := os.NewFile(uintptr(fd), filepath.Join(sd.path, name))
	if err := f.Chmod(0o500); err != nil {
		_ = f.Close()
		return nil, fmt.Errorf("stationtrail snapshot %s could not be made immutable: %w", filepath.Join(sd.path, name), err)
	}
	return f, nil
}

// reopenSnapshotFileAt reopens the finished snapshot read-only relative to
// the held snapshot-directory descriptor with O_NOFOLLOW. This descriptor is
// the one kept open and executed through; close-on-exec is cleared so the
// forked child inherits it across execve for /proc/self/fd/<fd> execution.
func reopenSnapshotFileAt(sd *stationTrailSnapshotDir, name string) (*os.File, error) {
	fd, err := syscall.Openat(sd.fd, name, syscall.O_RDONLY|syscall.O_NOFOLLOW|syscall.O_CLOEXEC, 0)
	if err != nil {
		return nil, err
	}
	f := os.NewFile(uintptr(fd), filepath.Join(sd.path, name))
	if err := clearCloseOnExec(fd); err != nil {
		_ = f.Close()
		return nil, fmt.Errorf("stationtrail snapshot %s could not be prepared for descriptor exec: %w", filepath.Join(sd.path, name), err)
	}
	return f, nil
}

// clearCloseOnExec clears FD_CLOEXEC so a child process forked by os/exec
// still holds the snapshot descriptor when it execve()s /proc/self/fd/<fd>.
func clearCloseOnExec(fd int) error {
	if _, _, errno := syscall.Syscall(syscall.SYS_FCNTL, uintptr(fd), uintptr(syscall.F_SETFD), 0); errno != 0 {
		return errno
	}
	return nil
}

// unlinkSnapshotFileAt unlinks the snapshot file relative to the held
// snapshot-directory descriptor; ENOENT is tolerated (already removed).
func unlinkSnapshotFileAt(sd *stationTrailSnapshotDir, name string) error {
	if name == "" {
		return nil
	}
	if err := unlinkatRaw(sd.fd, name, 0); err != nil && !errors.Is(err, syscall.ENOENT) {
		return err
	}
	return nil
}

// rmdirSnapshotDirAt removes the (now empty) snapshot directory relative to
// the held validated data-directory descriptor; ENOENT is tolerated.
func rmdirSnapshotDirAt(sd *stationTrailSnapshotDir) error {
	if err := unlinkatRaw(sd.dataDirFd, sd.name, atRemovedir); err != nil && !errors.Is(err, syscall.ENOENT) {
		return err
	}
	return nil
}

// closeDescriptor closes a raw held descriptor.
func closeDescriptor(fd int) error {
	if fd < 0 {
		return nil
	}
	return syscall.Close(fd)
}
