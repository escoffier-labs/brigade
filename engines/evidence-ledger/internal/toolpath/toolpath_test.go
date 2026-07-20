package toolpath

import (
	"errors"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

func TestRequireMissingBinary(t *testing.T) {
	t.Setenv("PATH", t.TempDir())
	err := Require("stationtrail", HintStationTrail)
	if err == nil {
		t.Fatal("Require: expected error for missing binary")
	}
	msg := err.Error()
	if strings.Contains(msg, "\n") {
		t.Fatalf("diagnostic must be one line, got %q", msg)
	}
	for _, want := range []string{
		"stationtrail",
		"not found on PATH",
		"github.com/escoffier-labs/stationtrail",
	} {
		if !strings.Contains(msg, want) {
			t.Fatalf("Require error %q missing %q", msg, want)
		}
	}
}

func TestRequirePresentBinary(t *testing.T) {
	dir := t.TempDir()
	script := filepath.Join(dir, "sourceharvest")
	if err := os.WriteFile(script, []byte("#!/bin/sh\nexit 0\n"), 0o700); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PATH", dir)
	if err := Require("sourceharvest", HintSourceHarvest); err != nil {
		t.Fatalf("Require present binary: %v", err)
	}
}

func TestWrapExecErrNotFound(t *testing.T) {
	err := WrapExecErr("discrawl", HintCrawler("discrawl"), exec.ErrNotFound)
	if err == nil {
		t.Fatal("expected upgraded error")
	}
	msg := err.Error()
	if strings.Contains(msg, "\n") {
		t.Fatalf("diagnostic must be one line, got %q", msg)
	}
	for _, want := range []string{"discrawl", "not found on PATH", "install discrawl"} {
		if !strings.Contains(msg, want) {
			t.Fatalf("WrapExecErr %q missing %q", msg, want)
		}
	}
}

func TestWrapExecErrWrappedPathError(t *testing.T) {
	pathErr := &exec.Error{Name: "opencode", Err: exec.ErrNotFound}
	err := WrapExecErr("opencode", HintOpenCode("sess-1"), pathErr)
	if err == nil {
		t.Fatal("expected upgraded error")
	}
	msg := err.Error()
	if !strings.Contains(msg, "opencode") || !strings.Contains(msg, "sess-1") {
		t.Fatalf("WrapExecErr %q missing tool/session context", msg)
	}
}

func TestWrapExecErrPreservesOtherErrors(t *testing.T) {
	orig := errors.New("permission denied")
	if got := WrapExecErr("telecrawl", HintCrawler("telecrawl"), orig); !errors.Is(got, orig) {
		t.Fatalf("got %v, want original %v", got, orig)
	}
	if got := WrapExecErr("telecrawl", HintCrawler("telecrawl"), nil); got != nil {
		t.Fatalf("nil in => nil out, got %v", got)
	}
}

func TestMissingMessage(t *testing.T) {
	msg := Missing("gitcrawl", HintCrawler("gitcrawl")).Error()
	if !strings.Contains(msg, "gitcrawl binary not found on PATH") {
		t.Fatalf("Missing message = %q", msg)
	}
	if !strings.Contains(msg, "install gitcrawl and ensure it is on PATH") {
		t.Fatalf("Missing message missing install hint: %q", msg)
	}
}
