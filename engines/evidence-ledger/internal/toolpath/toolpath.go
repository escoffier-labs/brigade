// Package toolpath reports actionable diagnostics when wrapper import tools
// are missing from PATH. Both internal/app and internal/sources/* import this
// leaf package so LookPath preflight stays shared without import cycles.
package toolpath

import (
	"errors"
	"fmt"
	"os/exec"
)

// Install hints for known external wrappers. Keep them one-line and actionable.
const (
	HintStationTrail  = "install stationtrail from github.com/escoffier-labs/stationtrail and ensure it is on PATH; native imports prefer miseledger crawl sessions"
	HintSourceHarvest = "install sourceharvest from github.com/escoffier-labs/sourceharvest and ensure it is on PATH; built-in crawls (crawl docs/files/repo/…) cover the same paths"
)

// HintCrawler returns an install hint for a native crawler exporter binary.
func HintCrawler(binary string) string {
	return fmt.Sprintf("install %s and ensure it is on PATH", binary)
}

// HintOpenCode returns an install hint for session-ID export via the OpenCode CLI.
func HintOpenCode(sessionID string) string {
	return fmt.Sprintf("install the OpenCode CLI (opencode) to export session ID %q, or pass a sanitized export file path instead", sessionID)
}

// Missing returns the canonical one-line diagnostic for a missing tool.
func Missing(name, installHint string) error {
	return fmt.Errorf("%s binary not found on PATH: %s", name, installHint)
}

// Require reports an actionable error when name is not on PATH.
// Prefer calling this before openMigrated / Start so a missing tool never
// opens or mutates the archive.
func Require(name, installHint string) error {
	if _, err := exec.LookPath(name); err != nil {
		return Missing(name, installHint)
	}
	return nil
}

// WrapExecErr upgrades exec.ErrNotFound (including *exec.Error wrappers from
// Start/Output) to the same user-facing diagnostic as Require.
func WrapExecErr(name, installHint string, err error) error {
	if err == nil {
		return nil
	}
	if errors.Is(err, exec.ErrNotFound) {
		return Missing(name, installHint)
	}
	return err
}
