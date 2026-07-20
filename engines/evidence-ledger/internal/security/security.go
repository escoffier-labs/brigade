package security

import (
	"errors"
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

type AtomicFile struct {
	File      *os.File
	finalPath string
	tempPath  string
	closed    bool
	committed bool
}

func CreateAtomicFile(path string) (*AtomicFile, error) {
	if err := EnsurePrivateParent(path); err != nil {
		return nil, err
	}
	dir := filepath.Dir(path)
	pattern := "." + filepath.Base(path) + ".tmp-*"
	f, err := os.CreateTemp(dir, pattern)
	if err != nil {
		return nil, err
	}
	if err := f.Chmod(PrivateFileMode); err != nil {
		_ = f.Close()
		_ = os.Remove(f.Name())
		return nil, err
	}
	return &AtomicFile{File: f, finalPath: path, tempPath: f.Name()}, nil
}

func (f *AtomicFile) Close() error {
	if f == nil || f.closed {
		return nil
	}
	err := f.File.Close()
	f.closed = true
	return err
}

func (f *AtomicFile) Commit() error {
	if f == nil {
		return nil
	}
	if err := f.Close(); err != nil {
		_ = os.Remove(f.tempPath)
		return err
	}
	if err := os.Rename(f.tempPath, f.finalPath); err != nil {
		_ = os.Remove(f.tempPath)
		return err
	}
	f.committed = true
	return nil
}

func (f *AtomicFile) Abort() error {
	if f == nil || f.committed {
		return nil
	}
	closeErr := f.Close()
	removeErr := os.Remove(f.tempPath)
	if closeErr != nil {
		return closeErr
	}
	if removeErr != nil && !errors.Is(removeErr, os.ErrNotExist) {
		return removeErr
	}
	return nil
}

func WritePrivateFileAtomic(path string, data []byte) error {
	f, err := CreateAtomicFile(path)
	if err != nil {
		return err
	}
	defer func() { _ = f.Abort() }()
	if _, err := f.File.Write(data); err != nil {
		return err
	}
	return f.Commit()
}
