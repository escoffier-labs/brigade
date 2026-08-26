package app

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/escoffier-labs/miseledger/internal/ingest"
	"github.com/escoffier-labs/miseledger/internal/provenance"
	"github.com/escoffier-labs/miseledger/internal/security"
	"github.com/escoffier-labs/miseledger/internal/textnorm"
)

const artifactSecretBody = "UNIQUE_ARTIFACT_SECRET forged artifact payload must not ship"

func insertIntegrityItemWithArtifact(t *testing.T, text, artifactHash string) string {
	t.Helper()
	db := openTestDB(t)
	defer db.Close()
	ensureIntegrityParents(t, db)
	id := "item-" + provenance.ContentSHA256(text)[:12]
	at := "2026-08-17T00:00:00Z"
	raw := []byte(`{"fixture":"` + id + `"}`)
	env, err := provenance.NewEvidenceEnvelope(provenance.EvidenceInput{
		SourceSystem: "miseledger", SourceKind: "synthetic", SourceProducer: "integrity_test",
		Origin: "workspace", RepositoryID: "unknown",
		CollectionID: "integrity-col", ItemID: id,
		LocatorKind: "uri", LocatorValue: "miseledger://synthetic/integrity-col/" + id,
		Attribution: "observed", Modality: "tool-output",
		TrustLabel: "untrusted", TrustAssignedBy: "test:integrity", TrustAssignedAt: &at,
		InjectionStatus: "clean", InjectionRules: []string{},
		Text: text, RawBytes: raw, CapturedAt: &at, IngestedAt: &at,
	})
	if err != nil {
		t.Fatal(err)
	}
	meta, err := json.Marshal(map[string]any{"provenance": env})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec(`insert into items(id, source_id, collection_id, external_id, kind, created_at, updated_at, text, summary, content_hash, raw_json, raw_hash, raw_path, raw_ordinal, metadata_json)
values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`,
		id, "integrity-src", "integrity-col", "ext:"+id, "message", at, at, text, "", "sha256:"+strings.Repeat("c", 64), string(raw), "sha256:"+provenance.SHA256Bytes(raw), "integrity.jsonl", 1, string(meta)); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec(`insert or ignore into item_metadata(item_id, key, value) values(?,?,?)`, id, ingest.MetaKeyProvenanceTrustLabel, "untrusted"); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec(`insert into item_fts(item_id, source_kind, collection_kind, item_kind, actor_type, body) values(?,?,?,?,?,?)`, id, "synthetic", "agent_session", "message", "agent", text); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec(`insert into artifacts(id, source_id, item_id, external_id, kind, path, url, mime_type, text, content_hash, metadata_json) values(?,?,?,?,?,?,?,?,?,?,?)`,
		"art-"+id, "integrity-src", id, "ext-art:"+id, "attachment", "", "", "text/plain", artifactSecretBody, artifactHash, "{}"); err != nil {
		t.Fatal(err)
	}
	return id
}

// #1203: artifact bodies emitted through --include-artifact-text must carry a
// canonical SHA-256 content digest; missing or malformed digests make the
// artifact (and therefore the emitting item) ineligible.
func TestEvidenceArtifactTextRequiresCanonicalContentHash(t *testing.T) {
	correct := sha256.Sum256([]byte(textnorm.Normalize(artifactSecretBody)))
	cases := []struct {
		name    string
		hash    string
		wantOK  bool
		wantHex string
	}{
		{name: "missing content_hash fails closed", hash: "", wantOK: false},
		{name: "malformed content_hash fails closed", hash: "sha256:short", wantOK: false},
		{name: "wrong canonical content_hash fails closed", hash: "sha256:" + strings.Repeat("b", 64), wantOK: false},
		{name: "matching canonical content_hash stays eligible", hash: "sha256:" + hex.EncodeToString(correct[:]), wantOK: true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			withTempHome(t)
			runOK(t, "init")
			insertIntegrityItemWithArtifact(t, "UNIQUE_ARTIFACT_GATE_NEEDLE clean verified body", tc.hash)
			bundle := runJSON(t, "evidence", "UNIQUE_ARTIFACT_GATE_NEEDLE", "--json", "--include-artifact-text")
			results, _ := bundle["results"].([]any)
			if len(results) != 1 {
				t.Fatalf("expected exactly one bundle result: %#v", bundle)
			}
			item := results[0].(map[string]any)
			raw, err := json.Marshal(item)
			if err != nil {
				t.Fatal(err)
			}
			if tc.wantOK {
				if item["eligibility_status"] == eligibilityIneligible {
					t.Fatalf("matching digest should stay eligible: %s", raw)
				}
				if !strings.Contains(string(raw), artifactSecretBody) {
					t.Fatalf("eligible artifact text missing from bundle: %s", raw)
				}
				return
			}
			if item["eligibility_status"] != eligibilityIneligible || item["reason_code"] != reasonIntegrityMismatch {
				t.Fatalf("artifact without usable content_hash stayed eligible: %s", raw)
			}
			if strings.Contains(string(raw), artifactSecretBody) {
				t.Fatalf("artifact text leaked under unusable content_hash: %s", raw)
			}
		})
	}
}

// #1202: search snippets must come from the verified item text, never from a
// tampered or stale FTS row.
func TestSearchSnippetDerivedFromVerifiedTextNotFTSRow(t *testing.T) {
	withTempHome(t)
	runOK(t, "init")
	text := "clean verified UNIQUE_SNIPPET_GATE_NEEDLE body surrounded by verified context words for the window"
	id := insertCleanIntegrityItem(t, text, "untrusted", "clean")
	db := openTestDB(t)
	if _, err := db.Exec(`update item_fts set body = ? where item_id = ?`, "pwned injected UNIQUE_SNIPPET_GATE_NEEDLE forged snippet text", id); err != nil {
		t.Fatal(err)
	}
	db.Close()

	search := runJSON(t, "search", "UNIQUE_SNIPPET_GATE_NEEDLE", "--json")
	results, _ := search["results"].([]any)
	if len(results) != 1 {
		t.Fatalf("search results = %#v", search)
	}
	hit := results[0].(map[string]any)
	snippet, _ := hit["snippet"].(string)
	if strings.Contains(snippet, "pwned") || strings.Contains(snippet, "forged") {
		t.Fatalf("snippet trusted the FTS row over the verified item text: %q", snippet)
	}
	if !strings.Contains(snippet, "verified") {
		t.Fatalf("snippet was not derived from verified item text: %q", snippet)
	}
}

// #1204: the stationtrail capability probe must block on any failure it
// cannot positively attribute to a legacy scanner. Round 2: the same
// executable controls both the unknown-command text and the parseable
// --version output, so implicit legacy tolerance was forgeable; it now
// additionally requires the operator to approve the executable's SHA-256
// digest.
func TestStationTrailProbeBlocksUnidentifiedFailures(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("sh shim test requires a posix shell")
	}
	shim := func(script string) (string, string) {
		dir := t.TempDir()
		path := filepath.Join(dir, "stationtrail")
		if err := os.WriteFile(path, []byte("#!/bin/sh\n"+script+"\n"), 0o755); err != nil {
			t.Fatal(err)
		}
		body, err := os.ReadFile(path)
		if err != nil {
			t.Fatal(err)
		}
		sum := sha256.Sum256(body)
		return dir, hex.EncodeToString(sum[:])
	}
	forgedLegacy := `if [ "$1" = "--version" ]; then echo "stationtrail 0.1.2"; exit 0; fi; echo 'stationtrail: unknown command '"'"'capabilities'"'"'' >&2; exit 64`
	cases := []struct {
		name     string
		script   string
		approval string // "", "self", or "other"
		wantPass bool
	}{
		{
			name:     "malformed capabilities JSON blocks",
			script:   `echo 'this is not json'; exit 0`,
			wantPass: false,
		},
		{
			name:     "nonzero exit without legacy marker blocks",
			script:   `echo 'fatal: cache corrupted' >&2; exit 9`,
			wantPass: false,
		},
		{
			name:     "forged legacy fingerprint with parseable version blocks without operator approval",
			script:   forgedLegacy,
			approval: "",
			wantPass: false,
		},
		{
			name:     "approval naming a different binary does not tolerate the forged legacy fingerprint",
			script:   forgedLegacy,
			approval: "other",
			wantPass: false,
		},
		{
			name:     "operator-approved executable digest tolerates the identified legacy version",
			script:   forgedLegacy,
			approval: "self",
			wantPass: true,
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			withTempHome(t)
			runOK(t, "init")
			dir, digest := shim(tc.script)
			switch tc.approval {
			case "self":
				t.Setenv(stationTrailApprovedDigestsEnv, digest)
			case "other":
				t.Setenv(stationTrailApprovedDigestsEnv, strings.Repeat("a", 64))
			}
			t.Setenv("PATH", dir)
			err := checkStationTrailCompat("codex")
			if tc.wantPass && err != nil {
				t.Fatalf("approved scanner blocked: %v", err)
			}
			if !tc.wantPass && err == nil {
				t.Fatal("unidentified probe failure was tolerated as a compatible legacy scanner")
			}
		})
	}
}

func TestStationTrailProbeTimeoutBlocks(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("sh shim test requires a posix shell")
	}
	withTempHome(t)
	runOK(t, "init")
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "stationtrail"), []byte("#!/bin/sh\nsleep 30\n"), 0o755); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PATH", dir)
	oldTimeout := stationTrailProbeTimeout
	stationTrailProbeTimeout = 150 * time.Millisecond
	t.Cleanup(func() { stationTrailProbeTimeout = oldTimeout })
	if err := checkStationTrailCompat("codex"); err == nil {
		t.Fatal("timed-out capability probe was tolerated as a compatible legacy scanner")
	}
}

// #1205: engine reads of scanner stdout and cached files are bounded.
func TestReadAllBoundedRejectsOversizedStream(t *testing.T) {
	big := strings.Repeat("x", int(maxScannerSubcommandOutput)+8)
	if _, err := readAllBounded(strings.NewReader(big), maxScannerSubcommandOutput); err == nil {
		t.Fatal("oversized stream accepted")
	}
	if _, err := readAllBounded(strings.NewReader("small"), maxScannerSubcommandOutput); err != nil {
		t.Fatalf("bounded stream rejected: %v", err)
	}
}

func TestLoadEvidenceBundleRejectsOversizedFile(t *testing.T) {
	withTempHome(t)
	runOK(t, "init")
	oldCap := maxEvidenceBundleBytes
	maxEvidenceBundleBytes = 64
	t.Cleanup(func() { maxEvidenceBundleBytes = oldCap })
	id := strings.Repeat("a", 24)
	path, err := evidenceBundlePath(id)
	if err != nil {
		t.Fatal(err)
	}
	if err := security.EnsurePrivateDir(filepath.Dir(path)); err != nil {
		t.Fatal(err)
	}
	forged := map[string]any{
		"id":           id,
		"generated_at": "2026-08-17T00:00:00Z",
		"filters":      map[string]any{},
		"results":      []map[string]any{},
		"filler":       strings.Repeat("p", 512),
	}
	raw, err := json.Marshal(forged)
	if err != nil {
		t.Fatal(err)
	}
	if len(raw) <= 64 {
		t.Fatalf("test fixture smaller than the shrunken cap: %d", len(raw))
	}
	if err := security.WritePrivateFileAtomic(path, append(raw, '\n')); err != nil {
		t.Fatal(err)
	}
	if _, err := loadEvidenceBundle(id); err == nil {
		t.Fatal("oversized cached bundle accepted")
	}
}

// #1201: cached bundles are HMAC-bound to their canonical reference.
func TestEvidenceBundleCacheMACBindsReference(t *testing.T) {
	withTempHome(t)
	runOK(t, "init")
	insertCleanIntegrityItem(t, "UNIQUE_CACHE_MAC_NEEDLE verified body", "untrusted", "clean")
	out := runJSON(t, "evidence", "UNIQUE_CACHE_MAC_NEEDLE", "--json")
	id, _ := out["id"].(string)
	if id == "" {
		t.Fatalf("evidence output missing bundle id: %#v", out)
	}

	ref, err := loadEvidenceBundleRef(id)
	if err != nil {
		t.Fatalf("freshly saved bundle failed authentication: %v", err)
	}
	if len(ref.ItemIDs) == 0 {
		t.Fatalf("authenticated ref lost its items: %#v", ref)
	}

	bundle, err := loadEvidenceBundle(id)
	if err != nil {
		t.Fatal(err)
	}
	results := bundleResultMaps(bundle)
	if len(results) == 0 {
		t.Fatalf("bundle has no results to tamper with: %#v", bundle)
	}
	tamperedID := "ffffffffffffffffffffffff"
	results[0]["id"] = tamperedID
	raw, err := json.Marshal(bundle)
	if err != nil {
		t.Fatal(err)
	}
	path, err := evidenceBundlePath(id)
	if err != nil {
		t.Fatal(err)
	}
	if err := security.WritePrivateFileAtomic(path, append(raw, '\n')); err != nil {
		t.Fatal(err)
	}
	if _, err := loadEvidenceBundleRef(id); err == nil {
		t.Fatal("bundle with edited item ids passed authentication")
	}
}

func TestEvidenceBundleCacheRejectsMissingMAC(t *testing.T) {
	withTempHome(t)
	runOK(t, "init")
	id := strings.Repeat("a", 24)
	forged := map[string]any{
		"id":           id,
		"generated_at": "2026-08-17T00:00:00Z",
		"filters":      map[string]any{"include_related": false, "include_artifact_text": false},
		"results":      []map[string]any{{"id": "ffffffffffffffffffffffff"}},
	}
	raw, err := json.Marshal(forged)
	if err != nil {
		t.Fatal(err)
	}
	path, err := evidenceBundlePath(id)
	if err != nil {
		t.Fatal(err)
	}
	if err := security.EnsurePrivateDir(filepath.Dir(path)); err != nil {
		t.Fatal(err)
	}
	if err := security.WritePrivateFileAtomic(path, append(raw, '\n')); err != nil {
		t.Fatal(err)
	}
	if _, err := loadEvidenceBundleRef(id); err == nil {
		t.Fatal("bundle without authentication was accepted")
	}
}

// Round-2 sendback helpers.

func writeEvidenceMACKeyFile(t *testing.T, data []byte, perm os.FileMode) string {
	t.Helper()
	path := evidenceBundleMACKeyPath()
	if err := security.EnsurePrivateDir(filepath.Dir(path)); err != nil {
		t.Fatal(err)
	}
	if err := os.RemoveAll(path); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, data, perm); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(path, perm); err != nil {
		t.Fatal(err)
	}
	return path
}

// #1201 round 2: loading the MAC key must validate it is a regular file,
// owned by the current uid, mode 0600, exactly 32 bytes — refusing otherwise
// with a typed error instead of sealing bundles with an attacker-supplied key.
func TestEvidenceBundleMACKeyLoadValidatesFile(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("posix key-file semantics (symlinks, uid, modes)")
	}
	withTempHome(t)
	runOK(t, "init")

	t.Run("generated key reloads", func(t *testing.T) {
		key, err := loadOrCreateEvidenceBundleMACKey()
		if err != nil {
			t.Fatal(err)
		}
		reloaded, err := loadOrCreateEvidenceBundleMACKey()
		if err != nil {
			t.Fatalf("valid generated key refused on reload: %v", err)
		}
		if !bytes.Equal(key, reloaded) {
			t.Fatal("reload returned a different key than was generated")
		}
	})

	cases := []struct {
		name   string
		mutate func(t *testing.T, path string)
		reason string
	}{
		{
			name: "symlinked key file refused",
			mutate: func(t *testing.T, path string) {
				target := filepath.Join(filepath.Dir(path), "elsewhere.key")
				if err := os.WriteFile(target, bytes.Repeat([]byte{7}, 32), 0o600); err != nil {
					t.Fatal(err)
				}
				if err := os.Remove(path); err != nil {
					t.Fatal(err)
				}
				if err := os.Symlink(target, path); err != nil {
					t.Fatal(err)
				}
			},
			reason: "refusing to open",
		},
		{
			name: "directory instead of regular file refused",
			mutate: func(t *testing.T, path string) {
				if err := os.Remove(path); err != nil {
					t.Fatal(err)
				}
				if err := os.Mkdir(path, 0o700); err != nil {
					t.Fatal(err)
				}
			},
			reason: "not a regular file",
		},
		{
			name: "group-readable mode refused",
			mutate: func(t *testing.T, path string) {
				writeEvidenceMACKeyFile(t, bytes.Repeat([]byte{7}, 32), 0o644)
			},
			reason: "want 0600",
		},
		{
			name: "oversized key file refused",
			mutate: func(t *testing.T, path string) {
				writeEvidenceMACKeyFile(t, bytes.Repeat([]byte{7}, 40), 0o600)
			},
			reason: "expected exactly 32 bytes",
		},
		{
			name: "short key file refused",
			mutate: func(t *testing.T, path string) {
				writeEvidenceMACKeyFile(t, bytes.Repeat([]byte{7}, 8), 0o600)
			},
			reason: "expected exactly 32 bytes",
		},
		{
			name: "empty key file refused",
			mutate: func(t *testing.T, path string) {
				writeEvidenceMACKeyFile(t, nil, 0o600)
			},
			reason: "expected exactly 32 bytes",
		},
		{
			name: "foreign-owner key file refused",
			mutate: func(t *testing.T, path string) {
				writeEvidenceMACKeyFile(t, bytes.Repeat([]byte{7}, 32), 0o600)
				if err := os.Chown(path, os.Getuid()+1337, -1); err != nil {
					t.Skipf("cannot chown to a foreign uid as this user: %v", err)
				}
			},
			reason: "owned by uid",
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			path := evidenceBundleMACKeyPath()
			tc.mutate(t, path)
			key, err := loadOrCreateEvidenceBundleMACKey()
			if err == nil {
				t.Fatalf("malformed MAC key accepted (%s): sealed bundles would use attacker-controlled bytes", tc.name)
			}
			var keyErr *EvidenceMACKeyError
			if !errors.As(err, &keyErr) {
				t.Fatalf("refusal is not a typed *EvidenceMACKeyError: %v", err)
			}
			if !strings.Contains(keyErr.Error(), tc.reason) {
				t.Fatalf("typed error %q does not mention %q", keyErr.Error(), tc.reason)
			}
			if key != nil {
				t.Fatal("refused load returned key material")
			}
		})
	}

	// After every refusal above the file may be gone or damaged; a fresh
	// environment must still be able to regenerate cleanly.
	t.Run("recovers by regenerating after refusal", func(t *testing.T) {
		withTempHome(t)
		runOK(t, "init")
		path := evidenceBundleMACKeyPath()
		writeEvidenceMACKeyFile(t, []byte("too short"), 0o644)
		if _, err := loadOrCreateEvidenceBundleMACKey(); err == nil {
			t.Fatal("damaged key accepted")
		}
		os.Remove(path)
		key, err := loadOrCreateEvidenceBundleMACKey()
		if err != nil || len(key) != 32 {
			t.Fatalf("regeneration after refusal failed: %v", err)
		}
	})
}

// #1201 round 2: concurrent first use must produce one shared key, never two
// competing keys where a bundle gets sealed with the loser.
func TestEvidenceBundleMACKeyConcurrentFirstUseAgreesOnOneKey(t *testing.T) {
	const racers = 8
	// Each round races creation in a fresh HOME; several rounds make a
	// competing-key loss deterministic to catch instead of timing-lucky.
	for round := 0; round < 12; round++ {
		t.Run(fmt.Sprintf("round-%02d", round), func(t *testing.T) {
			withTempHome(t)
			runOK(t, "init")
			start := make(chan struct{})
			keys := make([][]byte, racers)
			errs := make([]error, racers)
			var wg sync.WaitGroup
			for i := 0; i < racers; i++ {
				wg.Add(1)
				go func(i int) {
					defer wg.Done()
					<-start
					keys[i], errs[i] = loadOrCreateEvidenceBundleMACKey()
				}(i)
			}
			close(start)
			wg.Wait()
			for i, err := range errs {
				if err != nil {
					t.Fatalf("racer %d failed: %v", i, err)
				}
				if len(keys[i]) != 32 {
					t.Fatalf("racer %d returned %d-byte key", i, len(keys[i]))
				}
			}
			for i := 1; i < racers; i++ {
				if !bytes.Equal(keys[0], keys[i]) {
					t.Fatalf("concurrent initializers disagreed: racer 0 and racer %d hold different keys; a bundle sealed with the loser would not verify", i)
				}
			}
			onDisk, err := os.ReadFile(evidenceBundleMACKeyPath())
			if err != nil {
				t.Fatal(err)
			}
			if len(onDisk) != 32 {
				t.Fatalf("on-disk key is %d bytes, want 32", len(onDisk))
			}
			if !bytes.Equal(onDisk, keys[0]) {
				t.Fatal("on-disk winner differs from the loaded keys")
			}
			if runtime.GOOS != "windows" {
				info, err := os.Stat(evidenceBundleMACKeyPath())
				if err != nil {
					t.Fatal(err)
				}
				if info.Mode().Perm() != 0o600 {
					t.Fatalf("on-disk key mode = %o, want 600", info.Mode().Perm())
				}
			}
		})
	}
}

// #1205 round 2: scanner stderr retained for diagnostics is capped.
func TestCappedWriterBoundsRetainedBytes(t *testing.T) {
	w := &cappedWriter{limit: maxScannerStderrBytes}
	big := bytes.Repeat([]byte("e"), int(maxScannerStderrBytes)+1024)
	if n, err := w.Write(big); err != nil || n != len(big) {
		t.Fatalf("Write reported n=%d err=%v, want full count with no error", n, err)
	}
	got := w.String()
	if int64(len(got)) > maxScannerStderrBytes+256 {
		t.Fatalf("retained %d bytes for a %d byte cap", len(got), maxScannerStderrBytes)
	}
	if !w.truncated || !strings.Contains(got, "truncated") {
		t.Fatalf("truncation not surfaced: truncated=%v tail=%q", w.truncated, got[max(0, len(got)-120):])
	}
	small := &cappedWriter{limit: maxScannerStderrBytes}
	if _, err := small.Write([]byte("boom: real cause")); err != nil {
		t.Fatal(err)
	}
	if small.truncated || !strings.Contains(small.String(), "real cause") {
		t.Fatalf("small stderr lost or marked: %q truncated=%v", small.String(), small.truncated)
	}
}

// #1205 round 2: end-to-end — a scanner that floods stderr cannot make the
// engine buffer an unbounded amount of scanner-controlled text.
func TestScannerStderrIsCappedInProbeRunner(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("sh shim test requires a posix shell")
	}
	dir := t.TempDir()
	script := fmt.Sprintf("#!/bin/sh\nhead -c %d /dev/zero >&2\nexit 7\n", maxScannerStderrBytes*4)
	if err := os.WriteFile(filepath.Join(dir, "stationtrail"), []byte(script), 0o755); err != nil {
		t.Fatal(err)
	}
	oldPath := os.Getenv("PATH")
	t.Setenv("PATH", dir)
	defer t.Setenv("PATH", oldPath)
	oldTimeout := stationTrailProbeTimeout
	stationTrailProbeTimeout = 30 * time.Second
	t.Cleanup(func() { stationTrailProbeTimeout = oldTimeout })

	out, err := runStationTrailBounded([]string{"capabilities", "--json"}, stationTrailProbeTimeout, stationTrailCapsMaxOutput)
	if err == nil {
		t.Fatal("flooding scanner exited zero")
	}
	var cmdErr *stationTrailCommandError
	if !errors.As(err, &cmdErr) {
		t.Fatalf("expected *stationTrailCommandError, got %T: %v", err, err)
	}
	if len(cmdErr.Stderr) > int(maxScannerStderrBytes)+256 {
		t.Fatalf("retained %d bytes of scanner stderr against a %d byte cap", len(cmdErr.Stderr), maxScannerStderrBytes)
	}
	if len(out) != 0 {
		t.Fatalf("unexpected stdout payload from failing scanner: %q", out[:min(len(out), 64)])
	}
}
