//go:build unix && !linux

package app

import (
	"crypto/rand"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"syscall"
)

// Non-linux unix platforms (darwin included) have no openat in the portable
// syscall package, so the key file is opened by pathname relative to the
// already-validated data-directory path with O_NOFOLLOW|O_CLOEXEC (and
// O_CREAT|O_EXCL for creation), the validated data-directory descriptor is
// fstat'ed before and after every open or removal and must keep an identical
// dev/ino pair, and the opened key file must be regular, singly linked, and
// exactly 0600 before its contents are trusted. See scanner_datadir_nolinux.go
// for the residual window: between those parent identity checks the operation
// resolves by pathname, which only a same-uid DataDir writer could exploit —
// a writer outside this engine's model (#1093).

// readEvidenceBundleMACKeyAt reads and validates the key file under the
// already-validated data directory. A missing file surfaces as fs.ErrNotExist
// so callers can fall through to creation; every other refusal is a typed
// *EvidenceMACKeyError.
func readEvidenceBundleMACKeyAt(dfd int) ([]byte, error) {
	dataDir := ResolvePaths().DataDir
	keyPath := filepath.Join(dataDir, evidenceBundleMACKeyFile)
	before, err := fstatDataDirIdentity(dfd)
	if err != nil {
		return nil, &EvidenceMACKeyError{Reason: fmt.Sprintf("refusing to open key file: data directory could not be stated: %v", err)}
	}
	f, err := os.OpenFile(keyPath, os.O_RDONLY|syscall.O_NOFOLLOW|syscall.O_CLOEXEC, 0)
	if err != nil {
		if errors.Is(err, syscall.ENOENT) {
			return nil, err // ENOENT propagates as fs.ErrNotExist
		}
		if errors.Is(err, syscall.ELOOP) {
			return nil, &EvidenceMACKeyError{Reason: "refusing to open key file: path is a symlink"}
		}
		return nil, &EvidenceMACKeyError{Reason: fmt.Sprintf("refusing to open key file: %v", err)}
	}
	fail := func(err error) ([]byte, error) {
		_ = f.Close()
		return nil, err
	}
	after, err := fstatDataDirIdentity(dfd)
	if err != nil {
		return fail(&EvidenceMACKeyError{Reason: fmt.Sprintf("refusing to open key file: data directory could not be restated: %v", err)})
	}
	if after != before {
		return fail(&EvidenceMACKeyError{Reason: fmt.Sprintf("refusing to open key file: data directory changed identity (%d:%d then %d:%d) during the pathname-based open", before.dev, before.ino, after.dev, after.ino)})
	}
	info, statErr := f.Stat()
	if statErr != nil {
		return fail(&EvidenceMACKeyError{Reason: fmt.Sprintf("refusing to open key file: %v", statErr)})
	}
	if st, ok := info.Sys().(*syscall.Stat_t); !ok || st.Nlink != 1 {
		return fail(&EvidenceMACKeyError{Reason: "refusing to open key file: hard-linked (nlink > 1)"})
	}
	return validateEvidenceMACKeyContents(f)
}

// createEvidenceBundleMACKeyAt claims creation of the key file exclusively —
// O_CREAT|O_EXCL|O_NOFOLLOW at 0600 under the already-validated data
// directory, with the held descriptor's identity re-checked around the
// pathname-based open — and writes a fresh random key. Losing racers get
// fs.ErrExist and load the winner (#1201 round 2).
func createEvidenceBundleMACKeyAt(dfd int) ([]byte, error) {
	key := make([]byte, 32)
	if _, err := rand.Read(key); err != nil {
		return nil, err
	}
	dataDir := ResolvePaths().DataDir
	keyPath := filepath.Join(dataDir, evidenceBundleMACKeyFile)
	before, err := fstatDataDirIdentity(dfd)
	if err != nil {
		return nil, &EvidenceMACKeyError{Reason: fmt.Sprintf("refusing to create key file: data directory could not be stated: %v", err)}
	}
	f, err := os.OpenFile(keyPath, os.O_WRONLY|os.O_CREATE|os.O_EXCL|syscall.O_NOFOLLOW|syscall.O_CLOEXEC, 0o600)
	if err != nil {
		return nil, err // fs.ErrExist (lost race) propagates to the winner-loading path
	}
	fail := func(err error) ([]byte, error) {
		_ = f.Close()
		_ = removeUnderValidatedDataDir(dfd, dataDir, evidenceBundleMACKeyFile) // ours alone: O_EXCL guaranteed we created it
		return nil, err
	}
	after, err := fstatDataDirIdentity(dfd)
	if err != nil {
		return fail(fmt.Errorf("data directory could not be restated after key creation: %w", err))
	}
	if after != before {
		return fail(errors.New("data directory changed identity during the pathname-based key creation; refusing to trust it"))
	}
	if _, err := f.Write(key); err != nil {
		return fail(err)
	}
	// Creation modes are umask-subjected; fchmod is not, and the stored key
	// must end up exactly 0600 or its next load refuses it closed.
	if err := f.Chmod(0o600); err != nil {
		return fail(err)
	}
	info, err := f.Stat()
	if err != nil {
		return fail(err)
	}
	st, ok := info.Sys().(*syscall.Stat_t)
	if !ok || st.Nlink != 1 {
		return fail(errors.New("created key file is hard-linked; refusing"))
	}
	if perm := info.Mode().Perm(); perm != 0o600 {
		return fail(fmt.Errorf("created key file mode = %04o, want exactly 0600", perm))
	}
	if err := f.Close(); err != nil {
		_ = removeUnderValidatedDataDir(dfd, dataDir, evidenceBundleMACKeyFile)
		return nil, err
	}
	return key, nil
}
