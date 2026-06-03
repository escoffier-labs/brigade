package app

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"strconv"
	"strings"
)

func cmdServe(args []string, out, errw io.Writer) int {
	values, bools, rest, err := splitFlags(args, map[string]bool{"addr": true}, map[string]bool{"json": true})
	if err != nil {
		return fatalf(errw, "serve: %s", err)
	}
	if len(rest) != 0 {
		return fatalf(errw, "usage: spine serve [--addr 127.0.0.1:8765] [--json]")
	}
	addr := values["addr"]
	if addr == "" {
		addr = "127.0.0.1:8765"
	}
	if err := validateLocalAddr(addr); err != nil {
		return fatalf(errw, "serve: %s", err)
	}
	handler := newHTTPHandler()
	if bools["json"] {
		writeJSON(out, map[string]any{"ok": true, "addr": addr})
	} else {
		fmt.Fprintf(out, "listening on http://%s\n", addr)
	}
	if err := http.ListenAndServe(addr, handler); err != nil {
		return fatalf(errw, "serve: %s", err)
	}
	return 0
}

func validateLocalAddr(addr string) error {
	host, _, err := net.SplitHostPort(addr)
	if err != nil {
		return err
	}
	if host == "" || host == "localhost" {
		return nil
	}
	ip := net.ParseIP(strings.Trim(host, "[]"))
	if ip == nil || !ip.IsLoopback() {
		return errors.New("serve binds to loopback only")
	}
	return nil
}

func newHTTPHandler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/status", handleStatus)
	mux.HandleFunc("/sources", handleSources)
	mux.HandleFunc("/search", handleSearch)
	mux.HandleFunc("/items/", handleItem)
	mux.HandleFunc("/evidence", handleEvidence)
	return mux
}

func handleStatus(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		httpError(w, http.StatusMethodNotAllowed, "method not allowed")
		return
	}
	db, paths, err := openMigrated()
	if err != nil {
		httpError(w, http.StatusInternalServerError, err.Error())
		return
	}
	defer db.Close()
	status, err := collectStatus(db, paths)
	if err != nil {
		httpError(w, http.StatusInternalServerError, err.Error())
		return
	}
	httpJSON(w, status)
}

func handleSources(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		httpError(w, http.StatusMethodNotAllowed, "method not allowed")
		return
	}
	httpJSON(w, discoverSources())
}

func handleSearch(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		httpError(w, http.StatusMethodNotAllowed, "method not allowed")
		return
	}
	q := r.URL.Query()
	query := q.Get("q")
	if query == "" {
		httpError(w, http.StatusBadRequest, "missing q")
		return
	}
	db, _, err := openMigrated()
	if err != nil {
		httpError(w, http.StatusInternalServerError, err.Error())
		return
	}
	defer db.Close()
	results, err := search(db, searchOptsFromQuery(q, query))
	if err != nil {
		httpError(w, http.StatusInternalServerError, err.Error())
		return
	}
	httpJSON(w, map[string]any{"query": query, "results": results})
}

func handleItem(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		httpError(w, http.StatusMethodNotAllowed, "method not allowed")
		return
	}
	id := strings.TrimPrefix(r.URL.Path, "/items/")
	if id == "" || strings.Contains(id, "/") {
		httpError(w, http.StatusBadRequest, "missing item id")
		return
	}
	db, _, err := openMigrated()
	if err != nil {
		httpError(w, http.StatusInternalServerError, err.Error())
		return
	}
	defer db.Close()
	item, err := showItem(db, id)
	if err != nil {
		httpError(w, http.StatusNotFound, err.Error())
		return
	}
	httpJSON(w, item)
}

func handleEvidence(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		httpError(w, http.StatusMethodNotAllowed, "method not allowed")
		return
	}
	var req struct {
		Query   string `json:"query"`
		Source  string `json:"source"`
		Project string `json:"project"`
		From    string `json:"from"`
		To      string `json:"to"`
		Limit   int    `json:"limit"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		httpError(w, http.StatusBadRequest, err.Error())
		return
	}
	if req.Query == "" {
		httpError(w, http.StatusBadRequest, "missing query")
		return
	}
	db, _, err := openMigrated()
	if err != nil {
		httpError(w, http.StatusInternalServerError, err.Error())
		return
	}
	defer db.Close()
	bundle, err := evidenceBundle(db, SearchOpts{Query: req.Query, Source: req.Source, Project: req.Project, From: req.From, To: req.To, Limit: req.Limit})
	if err != nil {
		httpError(w, http.StatusInternalServerError, err.Error())
		return
	}
	httpJSON(w, bundle)
}

func searchOptsFromQuery(q map[string][]string, query string) SearchOpts {
	limit := 20
	if raw := firstQuery(q, "limit"); raw != "" {
		if parsed, err := strconv.Atoi(raw); err == nil {
			limit = parsed
		}
	}
	return SearchOpts{
		Query:      query,
		Source:     firstQuery(q, "source"),
		Collection: firstQuery(q, "collection"),
		Kind:       firstQuery(q, "kind"),
		ActorType:  firstQuery(q, "actor_type"),
		From:       firstQuery(q, "from"),
		To:         firstQuery(q, "to"),
		Project:    firstQuery(q, "project"),
		Tags:       firstQuery(q, "tags"),
		Limit:      limit,
	}
}

func firstQuery(q map[string][]string, key string) string {
	if vals := q[key]; len(vals) > 0 {
		return vals[0]
	}
	return ""
}

func httpJSON(w http.ResponseWriter, v any) {
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(v)
}

func httpError(w http.ResponseWriter, status int, msg string) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(map[string]any{"error": msg})
}
