// Package safeio provides race-resistant, no-follow file writes for agent-notify.
//
// The patterns mirror Brigade's Python helpers in localio.write_text_atomic /
// write_text_exclusive and run_journal._open_nofollow / _fsync_directory:
// same-directory temp, fsync the file, exclusive link or replace publish, then
// fsync the parent directory without following a symlinked final component.
package safeio

import (
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"runtime"
)

// ErrExists is returned when an exclusive (non-force) write finds the
// destination already occupied by a regular file, symlink, or other inode.
var ErrExists = errors.New("destination already exists")

var chmodTemp = func(tmp *os.File, mode os.FileMode) error {
	return tmp.Chmod(mode)
}

// WriteFile publishes data at path with mode.
//
// Without force, publication is exclusive: a same-directory temp is fsynced
// and hard-linked into place so a raced-in symlink or file at path cannot
// redirect the write (link fails with ErrExists). With force, the temp is
// published via Rename, which replaces a symlink at the destination rather
// than following it. The parent directory is fsynced on POSIX after publish.
func WriteFile(path string, data []byte, mode os.FileMode, force bool) error {
	if path == "" {
		return errors.New("path is empty")
	}
	dir := filepath.Dir(path)
	base := filepath.Base(path)

	// Refuse a symlinked parent before creating or publishing a temp file.
	// The post-publish fsync below still makes the name durable.
	if err := fsyncParent(dir); err != nil {
		return fmt.Errorf("refuse parent of %s: %w", base, err)
	}

	if !force {
		if err := refuseExisting(path); err != nil {
			return err
		}
	}

	tmp, err := os.CreateTemp(dir, "."+base+".*.tmp")
	if err != nil {
		return fmt.Errorf("create temp for %s: %w", base, err)
	}
	tmpName := tmp.Name()
	cleanup := true
	defer func() {
		if cleanup {
			_ = os.Remove(tmpName)
		}
	}()

	if _, err := tmp.Write(data); err != nil {
		_ = tmp.Close()
		return fmt.Errorf("write temp for %s: %w", base, err)
	}
	if err := chmodTemp(tmp, mode); err != nil {
		_ = tmp.Close()
		return fmt.Errorf("chmod temp for %s: %w", base, err)
	}
	if err := tmp.Sync(); err != nil {
		_ = tmp.Close()
		return fmt.Errorf("sync temp for %s: %w", base, err)
	}
	if err := tmp.Close(); err != nil {
		return fmt.Errorf("close temp for %s: %w", base, err)
	}

	if force {
		if err := os.Rename(tmpName, path); err != nil {
			return fmt.Errorf("replace %s: %w", base, err)
		}
		cleanup = false
	} else {
		if err := os.Link(tmpName, path); err != nil {
			if isExist(err) {
				return fmt.Errorf("%w: %s", ErrExists, path)
			}
			return fmt.Errorf("publish %s: %w", base, err)
		}
		// Temp remains until defer removes it; the hard link is the durable name.
	}

	if err := fsyncParent(dir); err != nil {
		return fmt.Errorf("fsync parent of %s: %w", base, err)
	}
	return nil
}

func refuseExisting(path string) error {
	fi, err := os.Lstat(path)
	if err != nil {
		if errors.Is(err, fs.ErrNotExist) {
			return nil
		}
		return fmt.Errorf("stat %s: %w", filepath.Base(path), err)
	}
	_ = fi
	return fmt.Errorf("%w: %s", ErrExists, path)
}

func isExist(err error) bool {
	return errors.Is(err, fs.ErrExist)
}

func supportsDirectoryFsync() bool {
	return runtime.GOOS != "windows" && runtime.GOOS != "js" && runtime.GOOS != "plan9"
}
