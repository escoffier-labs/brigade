package app

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"io/fs"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"
)

// Round-3 sendback helpers (#1201-#1205 security review).

func writeStationTrailApprovalsFile(t *testing.T, content string, perm os.FileMode) string {
	t.Helper()
	path := stationTrailApprovedDigestsPath()
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		t.Fatal(err)
	}
	// A previous subtest may have replaced the path with a directory or
	// symlink; clear it before writing a fresh fixture.
	if err := os.RemoveAll(path); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(content), perm); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(path, perm); err != nil {
		t.Fatal(err)
	}
	return path
}

func removeStationTrailApprovalsFile(t *testing.T) {
	t.Helper()
	if err := os.Remove(stationTrailApprovedDigestsPath()); err != nil && !errors.Is(err, fs.ErrNotExist) {
		t.Fatal(err)
	}
}

func writeShimScript(t *testing.T, dir, markerOrScript string) (string, string) {
	t.Helper()
	script := markerOrScript
	if !strings.Contains(script, "\n") && !strings.Contains(script, ";") && !strings.Contains(script, "fi") {
		script = "echo " + markerOrScript + "; exit 0"
	}
	body := []byte("#!/bin/sh\n" + script + "\n")
	path := filepath.Join(dir, "stationtrail")
	if err := os.WriteFile(path, body, 0o755); err != nil {
		t.Fatal(err)
	}
	sum := sha256.Sum256(body)
	return dir, hex.EncodeToString(sum[:])
}

// Round 3: the approved-digest allowlist lives in an owner-protected file
// under the data directory. The inherited-environment path
// (MISELEDGER_STATIONTRAIL_APPROVED_DIGESTS) is removed entirely: any child
// process — including the scanner being gated — can read the parent
// environment, so env-based approval was configuration visible to the
// attacker. Even a correctly matching env value must be ignored.
func TestStationTrailEnvVarApprovalIsIgnored(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("sh shim test requires a posix shell")
	}
	withTempHome(t)
	runOK(t, "init")
	dir, digest := writeShimScript(t, t.TempDir(), `if [ "$1" = "--version" ]; then echo "stationtrail 0.1.2"; exit 0; fi; echo 'stationtrail: unknown command '"'"'capabilities'"'"'' >&2; exit 64`)
	removeStationTrailApprovalsFile(t)
	t.Setenv("MISELEDGER_STATIONTRAIL_APPROVED_DIGESTS", digest)
	t.Setenv("PATH", dir)
	if err := checkStationTrailCompat("codex"); err == nil {
		t.Fatal("environment-variable digest approval was honored; the env allowlist path must stay removed")
	}
}

// Round 3: allowlist file validation mirrors the MAC key loader — regular
// file, current-uid owner, mode 0600, bounded read — and the parser accepts
// one 64-hex digest per line (optional sha256: prefix, case-normalized),
// refusing the WHOLE list closed on any malformed token.
func TestStationTrailApprovedDigestsFileValidation(t *testing.T) {
	withTempHome(t)
	runOK(t, "init")
	valid := strings.Repeat("ab", 32)
	setOf := func(tokens ...string) map[string]bool {
		m := map[string]bool{}
		for _, tok := range tokens {
			m[tok] = true
		}
		return m
	}
	cases := []struct {
		name       string
		content    string
		perm       os.FileMode
		setup      func(t *testing.T, path string)
		wantAbsent bool
		want       map[string]bool
		wantReason string
	}{
		{
			name:       "missing file means no approvals configured",
			wantAbsent: true,
			want:       map[string]bool{},
		},
		{
			name:     "plain lowercase digest approves",
			content:  valid + "\n",
			perm:     0o600,
			want:     setOf(valid),
		},
		{
			name:     "sha256 prefix and uppercase normalize",
			content:  "SHA256:" + strings.ToUpper(valid) + "\n",
			perm:     0o600,
			want:     setOf(valid),
		},
		{
			name:     "multiple digests and blank lines",
			content:  "\n" + valid + "\n\n" + strings.Repeat("cd", 32) + "\n\n",
			perm:     0o600,
			want:     setOf(valid, strings.Repeat("cd", 32)),
		},
		{
			name:       "group-readable mode refused",
			content:    valid + "\n",
			perm:       0o644,
			wantReason: "want 0600",
		},
		{
			name:       "world-writable mode refused",
			content:    valid + "\n",
			perm:       0o606,
			wantReason: "want 0600",
		},
		{
			name:       "symlinked approvals file refused",
			content:    valid + "\n",
			perm:       0o600,
			setup: func(t *testing.T, path string) {
				target := filepath.Join(filepath.Dir(path), "elsewhere.digests")
				if err := os.WriteFile(target, []byte(valid+"\n"), 0o600); err != nil {
					t.Fatal(err)
				}
				if err := os.Remove(path); err != nil {
					t.Fatal(err)
				}
				if err := os.Symlink(target, path); err != nil {
					t.Fatal(err)
				}
			},
			wantReason: "symlink",
		},
		{
			name:       "directory instead of regular file refused",
			content:    valid + "\n",
			perm:       0o600,
			setup: func(t *testing.T, path string) {
				if err := os.Remove(path); err != nil {
					t.Fatal(err)
				}
				if err := os.Mkdir(path, 0o700); err != nil {
					t.Fatal(err)
				}
			},
			wantReason: "not a regular file",
		},
		{
			name:       "foreign-owner approvals file refused",
			content:    valid + "\n",
			perm:       0o600,
			setup: func(t *testing.T, path string) {
				if err := os.Chown(path, os.Getuid()+1337, -1); err != nil {
					t.Skipf("cannot chown to a foreign uid as this user: %v", err)
				}
			},
			wantReason: "owned by uid",
		},
		{
			name:       "malformed token refuses the whole list",
			content:    valid + "\nshort\n",
			perm:       0o600,
			wantReason: "64-hex",
		},
		{
			name:       "non-hex token refuses the whole list",
			content:    valid + "\n" + strings.Repeat("g", 64) + "\n",
			perm:       0o600,
			wantReason: "64-hex",
		},
		{
			name:       "truncated final token refuses the whole list",
			content:    valid[:63] + "\n",
			perm:       0o600,
			wantReason: "64-hex",
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			var path string
			if !tc.wantAbsent {
				path = writeStationTrailApprovalsFile(t, tc.content, tc.perm)
				if tc.setup != nil {
					tc.setup(t, path)
				}
			} else {
				os.RemoveAll(stationTrailApprovedDigestsPath())
			}
			approved, err := loadStationTrailApprovedDigests()
			if tc.wantReason != "" {
				if err == nil {
					t.Fatalf("malformed approvals file accepted (%s)", tc.name)
				}
				var listErr *StationTrailApprovedDigestsError
				if !errors.As(err, &listErr) {
					t.Fatalf("refusal is not a typed *StationTrailApprovedDigestsError: %v", err)
				}
				if !strings.Contains(listErr.Error(), tc.wantReason) {
					t.Fatalf("typed error %q does not mention %q", listErr.Error(), tc.wantReason)
				}
				return
			}
			if err != nil {
				t.Fatalf("valid approvals file refused: %v", err)
			}
			for token := range tc.want {
				if !approved[token] {
					t.Fatalf("expected digest %s to be approved; got %#v", token, approved)
				}
			}
			for token := range approved {
				if !tc.want[token] {
					t.Fatalf("unexpected approved digest %s", token)
				}
			}
		})
	}

	t.Run("oversized approvals file refused", func(t *testing.T) {
		writeStationTrailApprovalsFile(t, strings.Repeat("a", int(maxApprovedDigestsFileBytes)+8), 0o600)
		if _, err := loadStationTrailApprovedDigests(); err == nil {
			t.Fatal("oversized approvals file accepted")
		}
	})
}

// Round 3 HIGH finding: the capabilities probe, version probe, digest
// calculation, and import each resolved the stationtrail executable
// independently, so a writable PATH entry could present an approved binary
// for hashing and a different one for execution. Resolution is now singular:
// one resolved absolute path backed by an opened descriptor, digest computed
// from that descriptor, and every exec re-verifying the artifact identity
// immediately beforehand.
func TestStationTrailBinaryRefusesSwappedExecutable(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("posix executable-swap semantics")
	}
	withTempHome(t)
	runOK(t, "init")
	dir := t.TempDir()
	_, digest := writeShimScript(t, dir, "echo APPROVED_ARTIFACT_RAN")
	t.Setenv("PATH", dir)
	bin, err := openStationTrailBinary()
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { bin.Close() })
	hashed, err := bin.digest()
	if err != nil {
		t.Fatal(err)
	}
	if hashed != digest {
		t.Fatalf("digest over the opened descriptor %q does not match the file content digest %q", hashed, digest)
	}
	writeStationTrailApprovalsFile(t, hashed+"\n", 0o600)

	// Attacker swaps the executable under the resolved path after hashing.
	path := filepath.Join(dir, "stationtrail")
	if err := os.Remove(path); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte("#!/bin/sh\necho EVIL_ARTIFACT_RAN\n"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := bin.verifyUnchangedBeforeExec(); err == nil {
		t.Fatal("exec proceeded although the file under the resolved path changed identity after hashing")
	}
	var identityErr *scannerBinaryIdentityError
	if err := bin.verifyUnchangedBeforeExec(); err == nil || !errors.As(err, &identityErr) {
		t.Fatalf("swap refusal is not a typed *scannerBinaryIdentityError: %v", err)
	}
	cmd, err := bin.command(context.Background(), "--version")
	if err == nil || cmd != nil {
		t.Fatalf("swapped executable was staged for execution: %v", err)
	}
}

// The single-resolution property: changing PATH after the binary has been
// opened must not redirect execution. Pre-fix, the digest LookPath and the
// exec LookPath were independent, so swapping the PATH entry between hash and
// exec ran a different binary than the one that was approved.
func TestStationTrailPATHSwapAfterOpenRunsOriginalArtifact(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("sh shim test requires a posix shell")
	}
	withTempHome(t)
	runOK(t, "init")
	dirApproved := t.TempDir()
	writeShimScript(t, dirApproved, "echo APPROVED_ARTIFACT_RAN")
	dirEvil := t.TempDir()
	writeShimScript(t, dirEvil, "echo EVIL_ARTIFACT_RAN")
	t.Setenv("PATH", dirApproved)
	bin, err := openStationTrailBinary()
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { bin.Close() })
	t.Setenv("PATH", dirEvil) // swap the PATH entry between hash and exec
	out, err := bin.runBounded([]string{"--version"}, 10*time.Second, stationTrailCapsMaxOutput)
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(out), "EVIL_ARTIFACT_RAN") {
		t.Fatal("execution followed the swapped PATH entry away from the opened artifact")
	}
	if !strings.Contains(string(out), "APPROVED_ARTIFACT_RAN") {
		t.Fatalf("original artifact did not run; got %q", string(out))
	}
}

// Round 3: on platforms without a race-free no-follow open and a platform
// ownership check (everything outside unix under the portable stdlib), MAC
// key storage must fail closed instead of silently degrading to a
// follow-the-symlink, no-owner-check loader. Simulated here by overriding the
// platform support seam; the real !unix build carries the same refusal.
func TestEvidenceMACKeyFailsClosedOnUnsupportedPlatform(t *testing.T) {
	withTempHome(t)
	runOK(t, "init")
	restore := evidenceMACKeyPlatformSupported
	evidenceMACKeyPlatformSupported = func() bool { return false }
	t.Cleanup(func() { evidenceMACKeyPlatformSupported = restore })

	key, err := loadOrCreateEvidenceBundleMACKey()
	if err == nil {
		t.Fatal("MAC key loaded on a simulated unsupported platform")
	}
	if key != nil {
		t.Fatal("unsupported-platform load returned key material")
	}
	var keyErr *EvidenceMACKeyError
	if !errors.As(err, &keyErr) {
		t.Fatalf("refusal is not a typed *EvidenceMACKeyError: %v", err)
	}
	if !strings.Contains(keyErr.Error(), "not supported") {
		t.Fatalf("typed error %q does not explain the platform refusal", keyErr.Error())
	}
	if _, statErr := os.Stat(evidenceBundleMACKeyPath()); !errors.Is(statErr, fs.ErrNotExist) {
		t.Fatalf("unsupported platform created a key file anyway: %v", statErr)
	}
	if err := sealEvidenceBundle(map[string]any{"id": strings.Repeat("a", 24)}); err == nil {
		t.Fatal("sealing succeeded on a simulated unsupported platform")
	}
	if err := verifyEvidenceBundleAuth(map[string]any{"id": strings.Repeat("a", 24)}); err == nil {
		t.Fatal("verification succeeded on a simulated unsupported platform")
	}
}
