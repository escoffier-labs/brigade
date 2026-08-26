package app

import (
	"fmt"
	"io"
	"os"
)

// Read bounds for engine-side inputs (#1205). Oversized input is rejected
// before it can force an unbounded allocation.
const (
	// maxScannerSubcommandOutput caps scanner subcommand stdout (for example
	// `stationtrail ... --dry-run --json`).
	maxScannerSubcommandOutput int64 = 8 << 20 // 8 MiB
	// maxScannerSummaryBytes caps the scanner-written summary JSON document.
	maxScannerSummaryBytes int64 = 1 << 20 // 1 MiB
)

// maxEvidenceBundleBytes caps a cached evidence bundle read back from disk.
var maxEvidenceBundleBytes int64 = 64 << 20 // 64 MiB

// readAllBounded reads r fully but refuses input larger than limit bytes.
func readAllBounded(r io.Reader, limit int64) ([]byte, error) {
	b, err := io.ReadAll(io.LimitReader(r, limit+1))
	if err != nil {
		return nil, err
	}
	if int64(len(b)) > limit {
		return nil, fmt.Errorf("input exceeds the %d byte read limit", limit)
	}
	return b, nil
}

// readFileBounded reads path after checking its size, so an oversized file is
// rejected before any allocation.
func readFileBounded(path string, limit int64) ([]byte, error) {
	info, err := os.Stat(path)
	if err != nil {
		return nil, err
	}
	if info.Size() > limit {
		return nil, fmt.Errorf("%s exceeds the %d byte read limit", path, limit)
	}
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	return readAllBounded(f, limit)
}
