package main

import (
	"encoding/json"
	"os"
	"path/filepath"
	"reflect"
	"testing"
)

type stationManifest struct {
	Schema    string        `json:"schema"`
	Name      string        `json:"name"`
	Station   string        `json:"station"`
	Lifecycle string        `json:"lifecycle"`
	Tools     []stationTool `json:"tools"`
}

type stationTool struct {
	Name     string           `json:"name"`
	Kind     string           `json:"kind"`
	Command  string           `json:"command"`
	Install  []string         `json:"install"`
	Surfaces []stationSurface `json:"surfaces"`
}

type stationSurface struct {
	Kind           string   `json:"kind"`
	Command        []string `json:"command"`
	ReadOnly       bool     `json:"read_only"`
	TimeoutSeconds int      `json:"timeout_seconds"`
	MaxChars       int      `json:"max_chars"`
	Probe          []string `json:"probe"`
	ProbeContains  []string `json:"probe_contains"`
}

func TestBrigadeStationManifestMatchesMiseLedgerCLI(t *testing.T) {
	body, err := os.ReadFile(filepath.Join("..", "..", "station.json"))
	if err != nil {
		t.Fatalf("read station.json: %v", err)
	}
	var manifest stationManifest
	if err := json.Unmarshal(body, &manifest); err != nil {
		t.Fatalf("parse station.json: %v", err)
	}

	if manifest.Schema != "brigade.station.v1" || manifest.Name != "miseledger" || manifest.Station != "evidence" || manifest.Lifecycle != "active" {
		t.Fatalf("unexpected station identity: %#v", manifest)
	}
	if len(manifest.Tools) != 1 {
		t.Fatalf("tools = %d, want 1", len(manifest.Tools))
	}
	tool := manifest.Tools[0]
	if tool.Name != "miseledger" || tool.Kind != "executable" || tool.Command != "miseledger" {
		t.Fatalf("unexpected tool identity: %#v", tool)
	}
	wantInstall := []string{"go", "install", "github.com/escoffier-labs/miseledger/cmd/miseledger@latest"}
	if !reflect.DeepEqual(tool.Install, wantInstall) {
		t.Fatalf("install = %q, want %q", tool.Install, wantInstall)
	}

	wantSurfaces := []stationSurface{
		{
			Kind: "doctor-json", Command: []string{"miseledger", "doctor", "--json"},
			ReadOnly: false, TimeoutSeconds: 120,
			Probe: []string{"miseledger", "doctor", "--help"}, ProbeContains: []string{"--json", "--mcp", "--archive"},
		},
		{
			Kind: "brief-markdown", Command: []string{"miseledger", "evidence", "<task>", "--markdown", "--limit", "5"},
			ReadOnly: false, TimeoutSeconds: 10, MaxChars: 4000,
			Probe: []string{"miseledger", "evidence", "--help"}, ProbeContains: []string{"--markdown", "--limit"},
		},
		{
			Kind: "verify-exit", Command: []string{"miseledger", "version"},
			ReadOnly: true, TimeoutSeconds: 10,
		},
	}
	if !reflect.DeepEqual(tool.Surfaces, wantSurfaces) {
		t.Fatalf("surfaces = %#v, want %#v", tool.Surfaces, wantSurfaces)
	}

	for _, surface := range tool.Surfaces {
		if reflect.DeepEqual(surface.Command, []string{"miseledger", "export", "markdown"}) {
			t.Fatal("bulk Markdown export must not be advertised as a bounded brief surface")
		}
	}
}
