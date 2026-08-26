package app

import (
	"bytes"
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
	// maxScannerStderrBytes caps how much scanner stderr is retained for
	// diagnostics (#1205 round 2).
	maxScannerStderrBytes int64 = 1 << 20 // 1 MiB
)

// maxEvidenceBundleBytes caps a cached evidence bundle read back from disk.
var maxEvidenceBundleBytes int64 = 64 << 20 // 64 MiB

// cappedWriter collects scanner stderr but retains at most limit bytes of it
// (#1205 round 2). Writes are never rejected — the excess is dropped and the
// truncation is surfaced by String — so a flooding scanner cannot turn its
// own diagnostics into an engine-side allocation or a broken pipe.
type cappedWriter struct {
	buf       bytes.Buffer
	limit     int64
	truncated bool
}

func (w *cappedWriter) Write(p []byte) (int, error) {
	if w.truncated || int64(w.buf.Len()) >= w.limit {
		w.truncated = true
		return len(p), nil
	}
	space := int(w.limit) - w.buf.Len()
	if space < len(p) {
		w.buf.Write(p[:space])
		w.truncated = true
		return len(p), nil
	}
	w.buf.Write(p)
	return len(p), nil
}

func (w *cappedWriter) String() string {
	if !w.truncated {
		return w.buf.String()
	}
	return w.buf.String() + fmt.Sprintf("\n[scanner stderr exceeded the %d byte cap and was truncated]", w.limit)
}

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
