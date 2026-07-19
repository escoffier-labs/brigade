package app

import (
	_ "embed"
	"net/http"
)

//go:embed webui/index.html
var indexHTML []byte

// handleUI serves the single-page browser UI at the server root. Any other
// unmatched path under "/" returns 404 so the JSON API stays authoritative.
func handleUI(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path != "/" {
		httpError(w, http.StatusNotFound, "not found")
		return
	}
	if r.Method != http.MethodGet {
		httpError(w, http.StatusMethodNotAllowed, "method not allowed")
		return
	}
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	w.Header().Set("Cache-Control", "no-store")
	_, _ = w.Write(indexHTML)
}
