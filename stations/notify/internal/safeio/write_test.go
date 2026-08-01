package safeio_test

import (
	"errors"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"

	"github.com/escoffier-labs/agent-notify/internal/safeio"
)

func TestWriteFile_CreatesExclusive(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "config.toml")
	body := []byte("hello = true\n")
	if err := safeio.WriteFile(path, body, 0o600, false); err != nil {
		t.Fatalf("WriteFile: %v", err)
	}
	got, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if string(got) != string(body) {
		t.Fatalf("content = %q, want %q", got, body)
	}
}

func TestWriteFile_RefusesExistingWithoutForce(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "config.toml")
	if err := os.WriteFile(path, []byte("old\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	err := safeio.WriteFile(path, []byte("new\n"), 0o600, false)
	if !errors.Is(err, safeio.ErrExists) {
		t.Fatalf("err = %v, want ErrExists", err)
	}
	if !strings.Contains(err.Error(), path) {
		t.Fatalf("error should identify refused target %q: %v", path, err)
	}
	got, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if string(got) != "old\n" {
		t.Fatalf("existing file was modified: %q", got)
	}
}

func TestWriteFile_ForceReplacesRegularFile(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "config.toml")
	if err := os.WriteFile(path, []byte("old\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := safeio.WriteFile(path, []byte("new\n"), 0o600, true); err != nil {
		t.Fatalf("WriteFile force: %v", err)
	}
	got, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if string(got) != "new\n" {
		t.Fatalf("content = %q, want new", got)
	}
}

func TestWriteFile_SymlinkCannotRedirectWithoutForce(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("symlink redirect tests require unix-style link semantics")
	}
	dir := t.TempDir()
	victim := filepath.Join(dir, "victim-secret.txt")
	if err := os.WriteFile(victim, []byte("KEEP\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(dir, "config.toml")
	if err := os.Symlink(victim, path); err != nil {
		t.Skipf("symlink not supported: %v", err)
	}

	err := safeio.WriteFile(path, []byte("ATTACK\n"), 0o600, false)
	if !errors.Is(err, safeio.ErrExists) {
		t.Fatalf("err = %v, want ErrExists", err)
	}
	if !strings.Contains(err.Error(), path) {
		t.Fatalf("error should identify refused target %q: %v", path, err)
	}
	got, err := os.ReadFile(victim)
	if err != nil {
		t.Fatal(err)
	}
	if string(got) != "KEEP\n" {
		t.Fatalf("victim overwritten via symlink: %q", got)
	}
}

func TestWriteFile_ForceReplacesSymlinkWithoutTouchingTarget(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("symlink redirect tests require unix-style link semantics")
	}
	dir := t.TempDir()
	victim := filepath.Join(dir, "victim-secret.txt")
	if err := os.WriteFile(victim, []byte("KEEP\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(dir, "config.toml")
	if err := os.Symlink(victim, path); err != nil {
		t.Skipf("symlink not supported: %v", err)
	}

	if err := safeio.WriteFile(path, []byte("SAFE\n"), 0o600, true); err != nil {
		t.Fatalf("WriteFile force over symlink: %v", err)
	}

	// Destination must now be a regular file with the new contents.
	fi, err := os.Lstat(path)
	if err != nil {
		t.Fatal(err)
	}
	if fi.Mode()&os.ModeSymlink != 0 {
		t.Fatal("destination is still a symlink after force write")
	}
	got, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if string(got) != "SAFE\n" {
		t.Fatalf("config content = %q, want SAFE", got)
	}
	victimGot, err := os.ReadFile(victim)
	if err != nil {
		t.Fatal(err)
	}
	if string(victimGot) != "KEEP\n" {
		t.Fatalf("victim overwritten via force+symlink: %q", victimGot)
	}
}

func TestWriteFile_ForceReplaceAfterSwapCannotClobberVictim(t *testing.T) {
	// Models the TOCTOU window: an attacker replaces a regular config path
	// with a symlink to a victim between "validation" and publish. Rename
	// publish must replace the symlink inode, not follow it.
	if runtime.GOOS == "windows" {
		t.Skip("symlink redirect tests require unix-style link semantics")
	}
	dir := t.TempDir()
	path := filepath.Join(dir, "config.toml")
	if err := os.WriteFile(path, []byte("old\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	victim := filepath.Join(dir, "outside.txt")
	if err := os.WriteFile(victim, []byte("KEEP\n"), 0o600); err != nil {
		t.Fatal(err)
	}

	// Simulate the race: remove the validated file and plant a symlink.
	if err := os.Remove(path); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(victim, path); err != nil {
		t.Skipf("symlink not supported: %v", err)
	}

	if err := safeio.WriteFile(path, []byte("new\n"), 0o600, true); err != nil {
		t.Fatalf("WriteFile force after swap: %v", err)
	}
	victimGot, err := os.ReadFile(victim)
	if err != nil {
		t.Fatal(err)
	}
	if string(victimGot) != "KEEP\n" {
		t.Fatalf("victim clobbered after replacement race: %q", victimGot)
	}
	fi, err := os.Lstat(path)
	if err != nil {
		t.Fatal(err)
	}
	if fi.Mode()&os.ModeSymlink != 0 {
		t.Fatal("path still a symlink; rename did not replace it")
	}
}
