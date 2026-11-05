package app

import (
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// engineCases mirrors the evidence half of tests/fixtures/engine-paths-cases.json,
// the storage resolution cases shared with the Python and Rust tests.
type engineCases struct {
	Evidence struct {
		Layouts map[string]struct {
			Leaf string `json:"leaf"`
			DB   string `json:"db"`
		} `json:"layouts"`
		MACKey string `json:"mac_key"`
		Config string `json:"config"`
		Cases  []struct {
			Name    string     `json:"name"`
			XDG     bool       `json:"xdg"`
			Present [][]string `json:"present"`
			Expect  struct {
				Source        string `json:"source"`
				Layout        string `json:"layout"`
				LegacyPresent bool   `json:"legacy_present"`
			} `json:"expect"`
		} `json:"cases"`
	} `json:"evidence"`
}

func loadEngineCases(t *testing.T) engineCases {
	t.Helper()
	data, err := os.ReadFile(repoPath(t, filepath.Join("..", "..", "tests", "fixtures", "engine-paths-cases.json")))
	if err != nil {
		t.Fatal(err)
	}
	var cases engineCases
	if err := json.Unmarshal(data, &cases); err != nil {
		t.Fatal(err)
	}
	if len(cases.Evidence.Cases) == 0 {
		t.Fatal("shared fixture has no evidence cases")
	}
	return cases
}

func fixtureLayout(t *testing.T, cases engineCases, role string) evidenceLayout {
	t.Helper()
	layout, ok := cases.Evidence.Layouts[role]
	if !ok {
		t.Fatalf("shared fixture has no %q layout", role)
	}
	return evidenceLayout{leaf: strings.Split(layout.Leaf, "/"), dbName: layout.DB}
}

// homeRoots points the environment at a fresh home and returns the data,
// config and cache roots, either from XDG variables or the HOME fallbacks.
func homeRoots(t *testing.T, xdg bool) map[string]string {
	t.Helper()
	home := t.TempDir()
	t.Setenv("HOME", home)
	roots := map[string]string{
		"data":   filepath.Join(home, ".local", "share"),
		"config": filepath.Join(home, ".config"),
		"cache":  filepath.Join(home, ".cache"),
	}
	for role, variable := range map[string]string{"data": "XDG_DATA_HOME", "config": "XDG_CONFIG_HOME", "cache": "XDG_CACHE_HOME"} {
		if xdg {
			roots[role] = filepath.Join(home, "xdg-"+role)
			t.Setenv(variable, roots[role])
		} else {
			t.Setenv(variable, "")
		}
	}
	return roots
}

func TestResolvePathsSharedCases(t *testing.T) {
	cases := loadEngineCases(t)
	if cases.Evidence.MACKey != evidenceBundleMACKeyFile || cases.Evidence.Config != "config.toml" {
		t.Fatalf("fixture key/config names %q/%q drifted from the engine", cases.Evidence.MACKey, cases.Evidence.Config)
	}
	for role, layout := range map[string]evidenceLayout{"canonical": canonicalLayout, "legacy": legacyLayout} {
		if got := fixtureLayout(t, cases, role); strings.Join(got.leaf, "/") != strings.Join(layout.leaf, "/") || got.dbName != layout.dbName {
			t.Fatalf("%s layout = %+v, fixture says %+v", role, layout, got)
		}
	}
	for _, tc := range cases.Evidence.Cases {
		t.Run(tc.Name, func(t *testing.T) {
			roots := homeRoots(t, tc.XDG)
			for _, present := range tc.Present {
				dir := fixtureLayout(t, cases, present[1]).dir(roots[present[0]])
				if err := os.MkdirAll(dir, 0o700); err != nil {
					t.Fatal(err)
				}
			}
			got := ResolvePaths()
			layout := fixtureLayout(t, cases, tc.Expect.Layout)
			want := Paths{
				ConfigPath:    filepath.Join(layout.dir(roots["config"]), cases.Evidence.Config),
				DataDir:       layout.dir(roots["data"]),
				DBPath:        filepath.Join(layout.dir(roots["data"]), layout.dbName),
				CacheDir:      layout.dir(roots["cache"]),
				Source:        tc.Expect.Source,
				LegacyPresent: tc.Expect.LegacyPresent,
			}
			if got != want {
				t.Fatalf("ResolvePaths() = %+v, want %+v", got, want)
			}
			if key := evidenceBundleMACKeyPath(); key != filepath.Join(want.DataDir, cases.Evidence.MACKey) {
				t.Fatalf("MAC key path %s does not follow DataDir %s", key, want.DataDir)
			}
		})
	}
}

// An archive in the legacy layout, as an older build left it, is opened in
// place: its bundle MACs still verify against the key beside the database,
// and the canonical directories are never created.
func TestLegacyArchiveOpensInPlaceAndBundleMACVerifies(t *testing.T) {
	if !evidenceMACKeyPlatformSupported() {
		t.Skip("bundle MAC key storage is not supported on this platform")
	}
	cases := loadEngineCases(t)
	roots := homeRoots(t, false)
	legacy := fixtureLayout(t, cases, "legacy")
	if err := os.MkdirAll(legacy.dir(roots["data"]), 0o700); err != nil {
		t.Fatal(err)
	}
	runOK(t, "init")
	runOK(t, "import", "codex", repoPath(t, "testdata/harnesses/codex-session.fixture.jsonl"), "--json")
	bundle := runJSON(t, "evidence", "adapter contract", "--source", "codex", "--json")
	id, _ := bundle["id"].(string)
	if id == "" {
		t.Fatalf("evidence bundle has no id: %v", bundle)
	}

	paths := ResolvePaths()
	if paths.Source != "legacy" || paths.DataDir != legacy.dir(roots["data"]) {
		t.Fatalf("legacy archive resolved to %+v", paths)
	}
	for _, path := range []string{paths.DBPath, filepath.Join(paths.DataDir, cases.Evidence.MACKey)} {
		if _, err := os.Stat(path); err != nil {
			t.Fatalf("legacy archive file missing: %v", err)
		}
	}
	shown := runJSON(t, "evidence", "show", id, "--json")
	if shown["id"] != id {
		t.Fatalf("evidence show = %v, want bundle %s", shown, id)
	}

	// The check is real: a different key in the legacy directory fails it.
	keyPath := filepath.Join(paths.DataDir, cases.Evidence.MACKey)
	if err := os.WriteFile(keyPath, []byte(strings.Repeat("k", 32)), 0o600); err != nil {
		t.Fatal(err)
	}
	if code, _, _ := run("evidence", "show", id, "--json"); code == 0 {
		t.Fatal("evidence show accepted a bundle sealed under a different key")
	}

	canonical := fixtureLayout(t, cases, "canonical")
	for _, root := range roots {
		if _, err := os.Stat(canonical.dir(root)); !errors.Is(err, os.ErrNotExist) {
			t.Fatalf("canonical storage created beside a legacy archive at %s: %v", canonical.dir(root), err)
		}
	}
}

func TestIdentityForArgv0(t *testing.T) {
	for argv0, want := range map[string]string{
		"":                                CanonicalProgram,
		CanonicalProgram:                  CanonicalProgram,
		"/opt/bin/" + CanonicalProgram:    CanonicalProgram,
		LegacyProgram:                     LegacyProgram,
		"/usr/local/bin/" + LegacyProgram: LegacyProgram,
		`C:\tools\` + strings.ToUpper(LegacyProgram) + ".EXE": LegacyProgram,
		LegacyProgram + ".exe":                                LegacyProgram,
		LegacyProgram + "-old":                                CanonicalProgram,
		"renamed-copy":                                        CanonicalProgram,
	} {
		if got := IdentityFor(argv0); got != want {
			t.Errorf("IdentityFor(%q) = %q, want %q", argv0, got, want)
		}
	}
}

func TestMCPServerNameAndDoctorFollowIdentity(t *testing.T) {
	withTempHome(t)
	defer func(prev string) { program = prev }(program)
	for _, identity := range []string{CanonicalProgram, LegacyProgram} {
		program = identity
		resp := handleMCPRequest(mcpRequest{JSONRPC: "2.0", ID: "init", Method: "initialize"})
		result, _ := resp.Result.(map[string]any)
		server, _ := result["serverInfo"].(map[string]any)
		if server["name"] != identity {
			t.Fatalf("%s: serverInfo.name = %v", identity, server["name"])
		}
		for _, check := range mcpDoctorChecks() {
			if check.Name == "mcp_initialize" && !check.OK {
				t.Fatalf("%s: doctor --mcp rejected its own server name: %s", identity, check.Detail)
			}
		}
	}
	if isServerName("other") {
		t.Fatal("doctor --mcp accepted an unrelated server name")
	}
}
