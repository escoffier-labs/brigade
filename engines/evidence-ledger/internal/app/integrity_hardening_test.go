package app

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"os"
	"path/filepath"
	"runtime"
	"strings"
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
// cannot positively attribute to a legacy scanner.
func TestStationTrailProbeBlocksUnidentifiedFailures(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("sh shim test requires a posix shell")
	}
	shim := func(script string) string {
		dir := t.TempDir()
		path := filepath.Join(dir, "stationtrail")
		if err := os.WriteFile(path, []byte("#!/bin/sh\n"+script+"\n"), 0o755); err != nil {
			t.Fatal(err)
		}
		return dir
	}
	cases := []struct {
		name      string
		script    string
		wantBlock bool
	}{
		{
			name:      "malformed capabilities JSON blocks",
			script:    `echo 'this is not json'; exit 0`,
			wantBlock: true,
		},
		{
			name:      "nonzero exit without legacy marker blocks",
			script:    `echo 'fatal: cache corrupted' >&2; exit 9`,
			wantBlock: true,
		},
		{
			name:      "positively identified legacy version is tolerated",
			script:    `if [ "$1" = "--version" ]; then echo "stationtrail 0.1.2"; exit 0; fi; echo 'stationtrail: unknown command '"'"'capabilities'"'"'' >&2; exit 64`,
			wantBlock: false,
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			withTempHome(t)
			runOK(t, "init")
			t.Setenv("PATH", shim(tc.script))
			err := checkStationTrailCompat("codex")
			if tc.wantBlock && err == nil {
				t.Fatal("unidentified probe failure was tolerated as a compatible legacy scanner")
			}
			if !tc.wantBlock && err != nil {
				t.Fatalf("identified legacy scanner blocked: %v", err)
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
