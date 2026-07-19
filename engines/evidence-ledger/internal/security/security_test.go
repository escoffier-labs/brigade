package security

import (
	"os"
	"path/filepath"
	"runtime"
	"testing"
)

func skipOnWindows(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("POSIX file modes not enforced on Windows")
	}
}

func mode(t *testing.T, path string) os.FileMode {
	t.Helper()
	info, err := os.Stat(path)
	if err != nil {
		t.Fatalf("stat %s: %v", path, err)
	}
	return info.Mode().Perm()
}

func TestEnsurePrivateDirCreatesAnd0700(t *testing.T) {
	skipOnWindows(t)
	dir := filepath.Join(t.TempDir(), "nested", "private")
	if err := EnsurePrivateDir(dir); err != nil {
		t.Fatalf("EnsurePrivateDir: %v", err)
	}
	if got := mode(t, dir); got != PrivateDirMode {
		t.Fatalf("dir mode = %o, want %o", got, PrivateDirMode)
	}
}

func TestEnsurePrivateDirTightensExistingDir(t *testing.T) {
	skipOnWindows(t)
	dir := filepath.Join(t.TempDir(), "loose")
	if err := os.Mkdir(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := EnsurePrivateDir(dir); err != nil {
		t.Fatalf("EnsurePrivateDir: %v", err)
	}
	if got := mode(t, dir); got != PrivateDirMode {
		t.Fatalf("dir mode = %o, want %o", got, PrivateDirMode)
	}
}

func TestEnsurePrivateParent(t *testing.T) {
	skipOnWindows(t)
	parent := filepath.Join(t.TempDir(), "parent")
	target := filepath.Join(parent, "file.db")
	if err := EnsurePrivateParent(target); err != nil {
		t.Fatalf("EnsurePrivateParent: %v", err)
	}
	if got := mode(t, parent); got != PrivateDirMode {
		t.Fatalf("parent mode = %o, want %o", got, PrivateDirMode)
	}
}

func TestChmodPrivateFile(t *testing.T) {
	skipOnWindows(t)
	path := filepath.Join(t.TempDir(), "secret")
	if err := os.WriteFile(path, []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := ChmodPrivateFile(path); err != nil {
		t.Fatalf("ChmodPrivateFile: %v", err)
	}
	if got := mode(t, path); got != PrivateFileMode {
		t.Fatalf("file mode = %o, want %o", got, PrivateFileMode)
	}
}

func TestWritePrivateFileAtomicWritesContentAnd0600(t *testing.T) {
	skipOnWindows(t)
	path := filepath.Join(t.TempDir(), "deep", "data.bin")
	data := []byte("private evidence bytes")
	if err := WritePrivateFileAtomic(path, data); err != nil {
		t.Fatalf("WritePrivateFileAtomic: %v", err)
	}
	got, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if string(got) != string(data) {
		t.Fatalf("content = %q, want %q", got, data)
	}
	if m := mode(t, path); m != PrivateFileMode {
		t.Fatalf("file mode = %o, want %o", m, PrivateFileMode)
	}
}

func TestWritePrivateFileAtomicLeavesNoTempFile(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "final.bin")
	if err := WritePrivateFileAtomic(path, []byte("ok")); err != nil {
		t.Fatalf("WritePrivateFileAtomic: %v", err)
	}
	entries, err := os.ReadDir(dir)
	if err != nil {
		t.Fatal(err)
	}
	if len(entries) != 1 || entries[0].Name() != "final.bin" {
		var names []string
		for _, e := range entries {
			names = append(names, e.Name())
		}
		t.Fatalf("expected only final.bin, got %v", names)
	}
}

func TestAtomicFileCommitRenamesTempToFinal(t *testing.T) {
	skipOnWindows(t)
	dir := t.TempDir()
	path := filepath.Join(dir, "out.txt")
	f, err := CreateAtomicFile(path)
	if err != nil {
		t.Fatalf("CreateAtomicFile: %v", err)
	}
	// The temp file must be private from creation, before any commit.
	if m := mode(t, f.tempPath); m != PrivateFileMode {
		t.Fatalf("temp file mode = %o, want %o", m, PrivateFileMode)
	}
	if _, err := f.File.WriteString("committed"); err != nil {
		t.Fatal(err)
	}
	if err := f.Commit(); err != nil {
		t.Fatalf("Commit: %v", err)
	}
	got, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if string(got) != "committed" {
		t.Fatalf("content = %q", got)
	}
	if _, err := os.Stat(f.tempPath); !os.IsNotExist(err) {
		t.Fatalf("temp file still present: %v", err)
	}
}

func TestAtomicFileAbortRemovesTempAndNoFinal(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "aborted.txt")
	f, err := CreateAtomicFile(path)
	if err != nil {
		t.Fatalf("CreateAtomicFile: %v", err)
	}
	if _, err := f.File.WriteString("scratch"); err != nil {
		t.Fatal(err)
	}
	if err := f.Abort(); err != nil {
		t.Fatalf("Abort: %v", err)
	}
	if _, err := os.Stat(path); !os.IsNotExist(err) {
		t.Fatal("final file should not exist after abort")
	}
	if _, err := os.Stat(f.tempPath); !os.IsNotExist(err) {
		t.Fatal("temp file should be removed after abort")
	}
}

func TestAtomicFileAbortAfterCommitIsNoop(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "kept.txt")
	f, err := CreateAtomicFile(path)
	if err != nil {
		t.Fatalf("CreateAtomicFile: %v", err)
	}
	if _, err := f.File.WriteString("keep"); err != nil {
		t.Fatal(err)
	}
	if err := f.Commit(); err != nil {
		t.Fatalf("Commit: %v", err)
	}
	if err := f.Abort(); err != nil {
		t.Fatalf("Abort after commit should be a no-op: %v", err)
	}
	if _, err := os.Stat(path); err != nil {
		t.Fatalf("committed file removed by abort: %v", err)
	}
}

func TestAtomicFileNilReceiverSafe(t *testing.T) {
	var f *AtomicFile
	if err := f.Close(); err != nil {
		t.Fatalf("nil Close: %v", err)
	}
	if err := f.Commit(); err != nil {
		t.Fatalf("nil Commit: %v", err)
	}
	if err := f.Abort(); err != nil {
		t.Fatalf("nil Abort: %v", err)
	}
}
