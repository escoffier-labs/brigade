//go:build unix

package app

import (
	"crypto/rand"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"syscall"
)

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

// openValidatedDataDirForAuth opens the data directory for MAC key storage
// through the shared validated-descriptor helper: O_DIRECTORY|O_NOFOLLOW,
// refused unless it is a directory owned by the current uid (#1201 round 4).
// The caller owns the returned descriptor.
func openValidatedDataDirForAuth() (int, error) {
	return openDataDirDescriptor(ResolvePaths().DataDir)
}

// readEvidenceBundleMACKeyAt reads and validates the key file relative to the
// held validated data-directory descriptor with O_NOFOLLOW (#1201 rounds 4-5).
// A missing file surfaces as fs.ErrNotExist so callers can fall through to
// creation; every other refusal is a typed *EvidenceMACKeyError.
func readEvidenceBundleMACKeyAt(dfd int) ([]byte, error) {
	fd, err := syscall.Openat(dfd, evidenceBundleMACKeyFile, syscall.O_RDONLY|syscall.O_NOFOLLOW|syscall.O_CLOEXEC, 0)
	if err != nil {
		if errors.Is(err, fs.ErrNotExist) || errors.Is(err, syscall.ENOENT) {
			return nil, err // ENOENT propagates as fs.ErrNotExist
		}
		if errors.Is(err, syscall.ELOOP) {
			return nil, &EvidenceMACKeyError{Reason: "refusing to open key file: path is a symlink"}
		}
		return nil, &EvidenceMACKeyError{Reason: fmt.Sprintf("refusing to open key file: %v", err)}
	}
	f := os.NewFile(uintptr(fd), filepath.Join(ResolvePaths().DataDir, evidenceBundleMACKeyFile))
	defer f.Close()
	return validateEvidenceMACKeyContents(f)
}

// createEvidenceBundleMACKeyAt claims creation of the key file exclusively —
// openat(O_CREAT|O_EXCL|O_NOFOLLOW, 0600) against the held validated
// data-directory descriptor — and writes a fresh random key. Because creation
// is descriptor-relative it cannot be redirected by swapping the data
// directory's pathname after validation (#1201 round 5), and because the name
// is O_EXCL|O_NOFOLLOW neither a planted regular file nor a symlink can be
// adopted. Losing racers get fs.ErrExist and load the winner (#1201 round 2).
func createEvidenceBundleMACKeyAt(dfd int) ([]byte, error) {
	key := make([]byte, 32)
	if _, err := rand.Read(key); err != nil {
		return nil, err
	}
	displayPath := filepath.Join(ResolvePaths().DataDir, evidenceBundleMACKeyFile)
	fd, err := syscall.Openat(dfd, evidenceBundleMACKeyFile, syscall.O_WRONLY|syscall.O_CREAT|syscall.O_EXCL|syscall.O_NOFOLLOW|syscall.O_CLOEXEC, 0o600)
	if err != nil {
		return nil, err
	}
	f := os.NewFile(uintptr(fd), displayPath)
	fail := func(err error) ([]byte, error) {
		_ = f.Close()
		_ = unlinkatRaw(dfd, evidenceBundleMACKeyFile, 0) // ours alone: O_EXCL guaranteed we created it
		return nil, err
	}
	if _, err := f.Write(key); err != nil {
		return fail(err)
	}
	// Creation modes are umask-subjected; fchmod is not, and the stored key
	// must end up exactly 0600 or its next load refuses it closed.
	if err := f.Chmod(0o600); err != nil {
		return fail(err)
	}
	if err := f.Close(); err != nil {
		_ = unlinkatRaw(dfd, evidenceBundleMACKeyFile, 0)
		return nil, err
	}
	return key, nil
}
