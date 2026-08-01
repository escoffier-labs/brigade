package safeio

import (
	"errors"
	"os"
	"path/filepath"
	"runtime"
	"testing"
)

func TestWriteFile_ChmodTargetSwappedMidWrite(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("symlink redirect tests require unix-style link semantics")
	}
	dir := t.TempDir()
	victim := filepath.Join(dir, "victim.txt")
	if err := os.WriteFile(victim, []byte("KEEP\n"), 0o644); err != nil {
		t.Fatal(err)
	}

	stop := errors.New("stop after chmod")
	original := chmodTemp
	t.Cleanup(func() { chmodTemp = original })
	chmodTemp = func(tmp *os.File, mode os.FileMode) error {
		if err := os.Remove(tmp.Name()); err != nil {
			return err
		}
		if err := os.Symlink(victim, tmp.Name()); err != nil {
			return err
		}
		if err := tmp.Chmod(mode); err != nil {
			return err
		}
		return stop
	}

	err := WriteFile(filepath.Join(dir, "config.toml"), []byte("SAFE\n"), 0o600, true)
	if !errors.Is(err, stop) {
		t.Fatalf("WriteFile error = %v, want %v", err, stop)
	}
	fi, err := os.Stat(victim)
	if err != nil {
		t.Fatal(err)
	}
	if got := fi.Mode().Perm(); got != 0o644 {
		t.Fatalf("victim permissions = %#o, want 0644", got)
	}
}
