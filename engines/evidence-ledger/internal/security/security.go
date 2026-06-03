package security

import (
	"os"
	"path/filepath"
)

const (
	PrivateDirMode  os.FileMode = 0o700
	PrivateFileMode os.FileMode = 0o600
)

func EnsurePrivateDir(path string) error {
	if err := os.MkdirAll(path, PrivateDirMode); err != nil {
		return err
	}
	return os.Chmod(path, PrivateDirMode)
}

func EnsurePrivateParent(path string) error {
	return EnsurePrivateDir(filepath.Dir(path))
}

func ChmodPrivateFile(path string) error {
	return os.Chmod(path, PrivateFileMode)
}
