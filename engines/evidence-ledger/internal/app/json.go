package app

import (
	"encoding/json"
	"fmt"
	"io"
)

func writeJSON(w io.Writer, v any) {
	enc := json.NewEncoder(w)
	enc.SetIndent("", "  ")
	_ = enc.Encode(v)
}

func fatalf(errw io.Writer, format string, args ...any) int {
	fmt.Fprintf(errw, format+"\n", args...)
	return 1
}
