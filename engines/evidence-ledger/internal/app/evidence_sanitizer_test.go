package app

import (
	"bytes"
	"encoding/json"
	"fmt"
	"go/ast"
	"go/parser"
	"go/token"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/escoffier-labs/miseledger/internal/provenance"
	"github.com/escoffier-labs/miseledger/internal/textnorm"
)

const (
	sanitizerSearchToken   = "EVSANITIZER"
	needleWindow           = 32
	vacuityLeafFloor       = 700
	cachedAttackerResultID = "EVSAN-attacker-cached-result-id-leak-40b"
)

type sanitizerNeedles struct {
	all []string

	query, project, from, to, source     string
	filePath, qualifiedName              string
	cacheID, cacheURI, cacheQuery        string
	externalID, snippet, text, summary   string
	collectionName, collectionKind       string
	actorName, actorType                 string
	artPath, artURL, artMIME, artText    string
	artHash, rawHash, rawPath            string
	relType, relTarget, relKind, relTime string
	timestamp, createdAt                 string
	metaFree, provenanceString           string
	controlSnippet                       string
}

func TestEvidenceExitsNoUnderboundNeedleAnyPath(t *testing.T) {
	withTempHome(t)
	runOK(t, "init")
	needles := newSanitizerNeedles()
	eligibleID, ineligibleIDs := seedSanitizerArchive(t, needles)
	writeTamperedEvidenceCache(t, needles, append([]string{eligibleID}, ineligibleIDs...))
	writeListingPaddingCaches(t, 180)

	codeRef, err := json.Marshal(map[string]any{
		"schema":         "brigade.code-reference.v1",
		"repository":     "escoffier-labs/brigade",
		"revision":       map[string]any{"commit": strings.Repeat("a", 40)},
		"file_path":      needles.filePath,
		"qualified_name": needles.qualifiedName,
		"symbol_kind":    "function",
		"source_span":    map[string]any{"start_line": 1, "line_count": 1},
		"change_kind":    "changed",
	})
	if err != nil {
		t.Fatal(err)
	}

	db := openTestDB(t)
	e1, err := materializeEvidenceBundle(db, SearchOpts{
		Query:               needles.query,
		Source:              needles.source,
		Project:             needles.project,
		From:                needles.from,
		To:                  needles.to,
		IncludeRelated:      true,
		IncludeArtifactText: true,
	}, append([]string{eligibleID}, ineligibleIDs...))
	db.Close()
	if err != nil {
		t.Fatal(err)
	}

	cli := runOK(t, "evidence", sanitizerSearchToken, "--json", "--include-related", "--include-artifact-text",
		"--project", needles.project, "--from", needles.from, "--to", needles.to,
		"--code-reference", string(codeRef))

	mcp, err := mcpEvidence(map[string]any{
		"query":                 sanitizerSearchToken,
		"project":               needles.project,
		"from":                  needles.from,
		"to":                    needles.to,
		"include_related":       true,
		"include_artifact_text": true,
		"code_reference": map[string]any{
			"schema":         "brigade.code-reference.v1",
			"repository":     "escoffier-labs/brigade",
			"revision":       map[string]any{"commit": strings.Repeat("a", 40)},
			"file_path":      needles.filePath,
			"qualified_name": needles.qualifiedName,
			"symbol_kind":    "function",
			"source_span":    map[string]any{"start_line": 1, "line_count": 1},
			"change_kind":    "changed",
		},
	})
	if err != nil {
		t.Fatal(err)
	}
	mcpBytes := []byte(mcp["content"].([]map[string]any)[0]["text"].(string))

	httpBody, _ := json.Marshal(map[string]any{
		"query":                 sanitizerSearchToken,
		"project":               needles.project,
		"from":                  needles.from,
		"to":                    needles.to,
		"include_related":       true,
		"include_artifact_text": true,
		"code_reference": map[string]any{
			"schema":         "brigade.code-reference.v1",
			"repository":     "escoffier-labs/brigade",
			"revision":       map[string]any{"commit": strings.Repeat("a", 40)},
			"file_path":      needles.filePath,
			"qualified_name": needles.qualifiedName,
			"symbol_kind":    "function",
			"source_span":    map[string]any{"start_line": 1, "line_count": 1},
			"change_kind":    "changed",
		},
	})
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/evidence", bytes.NewReader(httpBody))
	req.Header.Set("Content-Type", "application/json")
	newHTTPHandler().ServeHTTP(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("HTTP /evidence status=%d body=%s", rec.Code, rec.Body.String())
	}

	listBytes := []byte(runOK(t, "evidence", "list", "--json"))
	showCLI := []byte(runOK(t, "evidence", "show", needles.cacheID, "--json"))
	mcpShow, err := mcpEvidenceShow(map[string]any{"id": needles.cacheID})
	if err != nil {
		t.Fatal(err)
	}
	showMCP := []byte(mcpShow["content"].([]map[string]any)[0]["text"].(string))

	exits := []struct {
		name          string
		raw           []byte
		expectControl bool
		listing       bool
		minLeaves     int
	}{
		{"E1 item_ids", e1, true, false, vacuityLeafFloor},
		{"E2 CLI", []byte(cli), true, false, vacuityLeafFloor},
		{"E2 MCP", mcpBytes, true, false, vacuityLeafFloor},
		{"E2 HTTP", rec.Body.Bytes(), true, false, vacuityLeafFloor},
		{"E3 list", listBytes, false, true, vacuityLeafFloor},
		{"E4 CLI show", showCLI, true, false, vacuityLeafFloor},
		{"E4 MCP show", showMCP, true, false, vacuityLeafFloor},
	}

	for _, exit := range exits {
		t.Run(exit.name, func(t *testing.T) {
			assertSanitizedEvidencePayload(t, exit.raw, needles, exit.expectControl, exit.listing, exit.minLeaves)
		})
	}
}

func TestEvidenceExitBytesEqualFinalize(t *testing.T) {
	withTempHome(t)
	runOK(t, "init")
	needles := newSanitizerNeedles()
	eligibleID, ineligibleIDs := seedSanitizerArchive(t, needles)
	writeTamperedEvidenceCache(t, needles, append([]string{eligibleID}, ineligibleIDs...))

	db := openTestDB(t)
	e1, err := materializeEvidenceBundle(db, SearchOpts{Query: sanitizerSearchToken, IncludeArtifactText: true, IncludeRelated: true}, []string{eligibleID})
	db.Close()
	if err != nil {
		t.Fatal(err)
	}
	cli := []byte(runOK(t, "evidence", sanitizerSearchToken, "--json", "--include-artifact-text"))
	listBytes := []byte(runOK(t, "evidence", "list", "--json"))
	show := []byte(runOK(t, "evidence", "show", needles.cacheID, "--json"))

	for _, raw := range [][]byte{e1, cli, listBytes, show} {
		again, err := finalizeEvidenceResponse(evidenceOutbound{
			Tree:                decodeAny(t, raw),
			IncludeArtifactText: true,
		})
		if err != nil {
			t.Fatal(err)
		}
		if !bytes.Equal(bytes.TrimSpace(raw), bytes.TrimSpace(again)) {
			t.Fatalf("exit bytes were not a finalizeEvidenceResponse fixed point\nexit=%s\nagain=%s", raw, again)
		}
	}
}

func TestEvidenceExitASTForbidsUnsanitizedWriters(t *testing.T) {
	fset := token.NewFileSet()
	pkgs, err := parser.ParseDir(fset, ".", func(info os.FileInfo) bool {
		return strings.HasSuffix(info.Name(), ".go") && !strings.HasSuffix(info.Name(), "_test.go")
	}, 0)
	if err != nil {
		t.Fatal(err)
	}
	for _, pkg := range pkgs {
		for filename, file := range pkg.Files {
			reportEvidenceASTViolations(t, filename, file)
		}
	}
}

func TestEvidenceExitASTRejectsWriterPassedToUnlistedHelper(t *testing.T) {
	src := `package app

import "io"

func cmdEvidence(args []string, out, errw io.Writer) int {
	emitReviewLeak(out)
	return 0
}

func emitReviewLeak(w io.Writer) {
	io.WriteString(w, "leak")
}
`
	fset := token.NewFileSet()
	file, err := parser.ParseFile(fset, "review_leak.go", src, 0)
	if err != nil {
		t.Fatal(err)
	}
	findings := collectEvidenceASTViolations("review_leak.go", file)
	if len(findings) == 0 {
		t.Fatal("guard is still name-blind: passing out to an unlisted helper that writes must fail")
	}
	joined := strings.Join(findings, "\n")
	if !strings.Contains(joined, "emitReviewLeak") {
		t.Fatalf("expected a writer-passing finding for emitReviewLeak, got:\n%s", joined)
	}
}

func reportEvidenceASTViolations(t *testing.T, filename string, file *ast.File) {
	t.Helper()
	for _, finding := range collectEvidenceASTViolations(filename, file) {
		t.Error(finding)
	}
}

func collectEvidenceASTViolations(filename string, file *ast.File) []string {
	var findings []string
	for _, decl := range file.Decls {
		fn, ok := decl.(*ast.FuncDecl)
		if !ok || fn.Body == nil {
			continue
		}
		name := fn.Name.Name
		switch {
		case evidenceExitFuncs[name]:
			findings = append(findings, exitASTViolations(filename, fn)...)
		case sanctionedEvidenceSinks[name]:
			findings = append(findings, sinkASTViolations(filename, fn)...)
		}
	}
	return findings
}

var evidenceExitFuncs = map[string]bool{
	"cmdEvidence":               true,
	"cmdEvidenceShow":           true,
	"cmdEvidenceList":           true,
	"materializeEvidenceBundle": true,
	"listEvidenceBundles":       true,
	"regenerateEvidenceBundle":  true,
	"mcpEvidence":               true,
	"mcpEvidenceShow":           true,
	"handleEvidence":            true,
}

var sanctionedEvidenceSinks = map[string]bool{
	"writeEvidenceJSON":      true,
	"renderEvidenceMarkdown": true,
	"renderEvidenceListText": true,
	"mcpTextResultBytes":     true,
	"writeEvidenceHTTP":      true,
}

var exitAllowedWriteLike = map[string]bool{
	"writeEvidenceJSON":      true,
	"renderEvidenceMarkdown": true,
	"renderEvidenceListText": true,
	"mcpTextResultBytes":     true,
	"writeEvidenceHTTP":      true,
	"fatalf":                 true,
	"httpError":              true,
	"writeEvidenceUsage":     true,
}

var exitWriterRecipients = map[string]bool{
	"writeEvidenceJSON":      true,
	"renderEvidenceMarkdown": true,
	"renderEvidenceListText": true,
	"mcpTextResultBytes":     true,
	"writeEvidenceHTTP":      true,
	"writeEvidenceUsage":     true,
	"httpError":              true,
	"cmdEvidenceShow":        true,
	"cmdEvidenceList":        true,
	"MaxBytesReader":         true,
}

var rendererWriterRecipients = map[string]bool{
	"Fprint":      true,
	"Fprintf":     true,
	"Fprintln":    true,
	"Write":       true,
	"WriteString": true,
}

func exitASTViolations(filename string, fn *ast.FuncDecl) []string {
	var findings []string
	writers := outputWriterParams(fn)
	ast.Inspect(fn.Body, func(n ast.Node) bool {
		call, ok := n.(*ast.CallExpr)
		if !ok {
			return true
		}
		callee := astCalleeName(call)
		base := calleeBase(callee)
		if name, writeLike := writeLikeCallee(call); writeLike && !exitAllowedWriteLike[base] {
			findings = append(findings, fmt.Sprintf("%s:%s calls %s; evidence exits may write or construct a response only via sanctioned byte-sink helpers", filename, fn.Name.Name, name))
		}
		if callPassesWriter(call, writers) && !exitWriterRecipients[base] {
			findings = append(findings, fmt.Sprintf("%s:%s passes the output writer to %s; evidence exits may hand the writer only to sanctioned byte-sink helpers", filename, fn.Name.Name, callee))
		}
		return true
	})
	return findings
}

func sinkASTViolations(filename string, fn *ast.FuncDecl) []string {
	var findings []string
	banned := map[string]bool{
		"writeJSON":     true,
		"httpJSON":      true,
		"mcpTextResult": true,
	}
	writers := outputWriterParams(fn)
	ast.Inspect(fn.Body, func(n ast.Node) bool {
		call, ok := n.(*ast.CallExpr)
		if !ok {
			return true
		}
		callee := astCalleeName(call)
		base := calleeBase(callee)
		if banned[base] {
			findings = append(findings, fmt.Sprintf("%s:%s calls %s; sanctioned sinks may not construct unsanitized responses", filename, fn.Name.Name, callee))
		}
		if callPassesWriter(call, writers) && !rendererWriterRecipients[base] && !banned[base] {
			findings = append(findings, fmt.Sprintf("%s:%s passes the output writer to %s; Markdown/text renderers may not hand the writer to an unlisted helper", filename, fn.Name.Name, callee))
		}
		return true
	})
	return findings
}

func outputWriterParams(fn *ast.FuncDecl) map[string]bool {
	writers := map[string]bool{}
	if fn.Type.Params == nil {
		return writers
	}
	for _, field := range fn.Type.Params.List {
		if !isOutputWriterType(field.Type) {
			continue
		}
		for _, name := range field.Names {
			if name.Name == "errw" {
				continue
			}
			writers[name.Name] = true
		}
	}
	return writers
}

func isOutputWriterType(expr ast.Expr) bool {
	switch t := expr.(type) {
	case *ast.StarExpr:
		return isOutputWriterType(t.X)
	case *ast.SelectorExpr:
		pkg, ok := t.X.(*ast.Ident)
		if !ok {
			return false
		}
		switch {
		case pkg.Name == "io" && t.Sel.Name == "Writer":
			return true
		case pkg.Name == "http" && t.Sel.Name == "ResponseWriter":
			return true
		case pkg.Name == "os" && t.Sel.Name == "File":
			return true
		}
	}
	return false
}

func callPassesWriter(call *ast.CallExpr, writers map[string]bool) bool {
	if len(writers) == 0 {
		return false
	}
	for _, arg := range call.Args {
		ident, ok := arg.(*ast.Ident)
		if ok && writers[ident.Name] {
			return true
		}
	}
	return false
}

func writeLikeCallee(call *ast.CallExpr) (string, bool) {
	name := astCalleeName(call)
	if name == "" {
		return "", false
	}
	if isWriteLikeName(name) {
		return name, true
	}
	return "", false
}

func astCalleeName(call *ast.CallExpr) string {
	switch fun := call.Fun.(type) {
	case *ast.Ident:
		return fun.Name
	case *ast.SelectorExpr:
		if ident, ok := fun.X.(*ast.Ident); ok {
			return ident.Name + "." + fun.Sel.Name
		}
		return fun.Sel.Name
	default:
		return ""
	}
}

func calleeBase(name string) string {
	if i := strings.LastIndex(name, "."); i >= 0 {
		return name[i+1:]
	}
	return name
}

func isWriteLikeName(name string) bool {
	base := calleeBase(name)
	switch base {
	case "httpJSON", "mcpTextResult", "mcpTextResultBytes",
		"renderEvidenceMarkdown", "renderEvidenceListText",
		"fatalf", "httpError", "writeEvidenceUsage", "Encode",
		"Write", "WriteString", "WriteHeader",
		"Fprint", "Fprintf", "Fprintln", "Print", "Printf", "Println":
		return true
	}
	if strings.Contains(strings.ToLower(base), "write") {
		return true
	}
	if strings.HasPrefix(name, "io.Copy") {
		return true
	}
	return false
}

func TestIneligibleStubExactlyThreeKeys(t *testing.T) {
	withTempHome(t)
	runOK(t, "init")
	id := insertCleanIntegrityItem(t, "UNIQUE_STUB_KEYS quarantined body", "quarantined", "pending")
	needles := newSanitizerNeedles()
	plantIneligibleFields(t, id, 0, needles)
	bundle := runJSON(t, "evidence", "UNIQUE_STUB_KEYS", "--json")
	item := firstBundleResult(t, bundle)
	stubID, _ := item["id"].(string)
	want := evidenceItemStubID(id)
	if stubID != want {
		t.Fatalf("stub id = %#v, want minted 24-hex %s (server id was %s)", item["id"], want, id)
	}
	if !evidenceBundleIDPattern.MatchString(stubID) {
		t.Fatalf("stub id = %#v, want 24 lowercase hex (server id was %s)", item["id"], id)
	}
	if item["eligibility_status"] != eligibilityIneligible {
		t.Fatalf("eligibility_status = %#v", item["eligibility_status"])
	}
	if item["reason_code"] != reasonTrustQuarantined {
		t.Fatalf("reason_code = %#v", item["reason_code"])
	}
	if len(item) != 3 {
		t.Fatalf("stub decoded to %d keys: %#v", len(item), item)
	}
}

func TestE4SourceMissingStubDropsCachedAttackerID(t *testing.T) {
	if len(cachedAttackerResultID) != 40 {
		t.Fatalf("cachedAttackerResultID must be 40 bytes, got %d", len(cachedAttackerResultID))
	}
	withTempHome(t)
	runOK(t, "init")
	needles := newSanitizerNeedles()
	writeTamperedEvidenceCache(t, needles, nil)

	cli := runOK(t, "evidence", "show", needles.cacheID, "--json")
	if strings.Contains(cli, cachedAttackerResultID) {
		t.Fatalf("E4 CLI echoed cached attacker result id")
	}
	var tree map[string]any
	if err := json.Unmarshal([]byte(cli), &tree); err != nil {
		t.Fatal(err)
	}
	assertSourceMissingStubDroppedID(t, tree, "E4 CLI")

	mcpShow, err := mcpEvidenceShow(map[string]any{"id": needles.cacheID})
	if err != nil {
		t.Fatal(err)
	}
	showMCP := mcpShow["content"].([]map[string]any)[0]["text"].(string)
	if strings.Contains(showMCP, cachedAttackerResultID) {
		t.Fatalf("E4 MCP echoed cached attacker result id")
	}
	var mcpTree map[string]any
	if err := json.Unmarshal([]byte(showMCP), &mcpTree); err != nil {
		t.Fatal(err)
	}
	assertSourceMissingStubDroppedID(t, mcpTree, "E4 MCP")
}

func assertSingleIneligibleEvidenceStub(t *testing.T, bundle map[string]any, serverID, reason string) {
	t.Helper()
	results, _ := bundle["results"].([]any)
	if len(results) != 1 {
		t.Fatalf("evidence results = %#v, want one ineligible stub", bundle)
	}
	item, _ := results[0].(map[string]any)
	if item["eligibility_status"] != eligibilityIneligible || item["reason_code"] != reason {
		t.Fatalf("evidence stub = %#v, want ineligible %s", item, reason)
	}
	want := evidenceItemStubID(serverID)
	if item["id"] != want {
		t.Fatalf("evidence stub id = %#v, want minted %s", item["id"], want)
	}
	if len(item) != 3 {
		t.Fatalf("evidence stub keys = %#v, want exactly three", item)
	}
}

func assertSourceMissingStubDroppedID(t *testing.T, tree map[string]any, surface string) {
	t.Helper()
	found := false
	for _, raw := range bundleResultMaps(tree) {
		if raw["reason_code"] != reasonSourceMissing {
			continue
		}
		found = true
		if _, ok := raw["id"]; ok {
			t.Fatalf("%s source_missing stub kept a cache-sourced id: %#v", surface, raw)
		}
		if len(raw) != 2 {
			t.Fatalf("%s source_missing stub keys = %#v, want eligibility_status + reason_code", surface, raw)
		}
	}
	if !found {
		t.Fatalf("%s missing source_missing stub: %#v", surface, tree)
	}
}

func TestWalkJSONLeavesInspectsMapKeys(t *testing.T) {
	const hostileKey = "hostile-source-kind-as-map-key"
	seen := false
	walkJSONLeaves(map[string]any{hostileKey: 1}, func(s string) {
		if s == hostileKey {
			seen = true
		}
	})
	if !seen {
		t.Fatal("walkJSONLeaves is key-blind; map keys must be inspected")
	}
}

func TestBuildProvisionalItemHasNoID(t *testing.T) {
	item := buildProvisionalItem(provisionalItemInput{
		ExternalID: "ext-provisional",
		Snippet:    "snippet",
		Timestamp:  "2026-08-21T00:00:00Z",
		SourceKind: "synthetic",
		Kind:       "message",
	})
	if _, ok := item["id"]; ok {
		t.Fatalf("provisional item must not carry id before the eligibility gate: %#v", item)
	}
}

func TestURLHashDoesNotAuthorizeSwappedArtifactText(t *testing.T) {
	withTempHome(t)
	runOK(t, "init")
	body := "UNIQUE_URLHASH eligible body"
	id := insertCleanIntegrityItem(t, body, "untrusted", "clean")
	swapped := "UNIQUE_URLHASH swapped artifact text NEEDLE"
	url := "https://example.test/swapped"
	db := openTestDB(t)
	urlHash := provenance.SHA256Bytes([]byte(url))
	if _, err := db.Exec(`insert into artifacts(id, source_id, item_id, external_id, kind, path, url, mime_type, text, content_hash, metadata_json)
values(?,?,?,?,?,?,?,?,?,?,?)`,
		"art-url-"+id, "integrity-src", id, "art:"+id, "url", "", url, "text/plain", swapped, "sha256:"+urlHash, "{}"); err != nil {
		t.Fatal(err)
	}
	db.Close()

	bundle := runJSON(t, "evidence", "UNIQUE_URLHASH", "--include-artifact-text", "--json")
	item := firstBundleResult(t, bundle)
	if item["eligibility_status"] != eligibilityIneligible || item["reason_code"] != reasonIntegrityMismatch {
		t.Fatalf("URL-hash swap must be an integrity-mismatch stub: %#v", item)
	}
	raw, _ := json.Marshal(item)
	if strings.Contains(string(raw), swapped) {
		t.Fatalf("URL-hash swap leaked artifact text: %s", raw)
	}
}

func newSanitizerNeedles() sanitizerNeedles {
	n := sanitizerNeedles{
		query:            plantNeedle("QUERY"),
		project:          plantNeedle("PROJECT"),
		from:             strings.Repeat("0", 180) + "NEEDLEFROM0001",
		to:               strings.Repeat("z", 180) + "NEEDLETO0001",
		source:           plantNeedle("SOURCE"),
		filePath:         "pkg/" + plantNeedle("FILEPATH"),
		qualifiedName:    plantNeedle("QUALNAME"),
		cacheID:          strings.Repeat("b", 24),
		cacheURI:         plantNeedle("CACHEURI"),
		cacheQuery:       plantNeedle("CACHEQUERY"),
		externalID:       plantNeedle("EXTID"),
		snippet:          plantNeedle("SNIPPET"),
		text:             plantNeedle("TEXT"),
		summary:          plantNeedle("SUMMARY"),
		collectionName:   plantNeedle("COLNAME"),
		collectionKind:   plantNeedle("COLKIND"),
		actorName:        plantNeedle("ACTORNAME"),
		actorType:        plantNeedle("ACTORTYPE"),
		artPath:          plantNeedle("ARTPATH"),
		artURL:           "https://example.test/" + plantNeedle("ARTURL"),
		artMIME:          plantNeedle("ARTMIME"),
		artText:          plantNeedle("ARTTEXT"),
		artHash:          plantNeedle("ARTHASH"),
		rawHash:          plantNeedle("RAWHASH"),
		rawPath:          plantNeedle("RAWPATH"),
		relType:          plantNeedle("RELTYPE"),
		relTarget:        plantNeedle("RELTARGET"),
		relKind:          plantNeedle("RELKIND"),
		relTime:          plantNeedle("RELTIME"),
		timestamp:        plantNeedle("TIMESTAMP"),
		createdAt:        plantNeedle("CREATEDAT"),
		metaFree:         plantNeedle("METAFREE"),
		provenanceString: plantNeedle("PROVSTR"),
		controlSnippet:   plantNeedle("CONTROL"),
	}
	n.all = []string{
		n.query, n.project, n.from, n.to, n.source, n.filePath, n.qualifiedName,
		n.cacheURI, n.cacheQuery, n.externalID, n.snippet, n.text, n.summary,
		n.collectionName, n.collectionKind, n.actorName, n.actorType,
		n.artPath, n.artURL, n.artMIME, n.artText, n.artHash, n.rawHash, n.rawPath,
		n.relType, n.relTarget, n.relKind, n.relTime, n.timestamp, n.createdAt,
		n.metaFree, n.provenanceString, cachedAttackerResultID,
	}
	return n
}

func plantNeedle(tag string) string {
	unit := tag + "-"
	filler := strings.Repeat(unit, (180/len(unit))+2)
	return filler[:180] + "NEEDLE" + tag
}

func seedSanitizerArchive(t *testing.T, needles sanitizerNeedles) (eligibleID string, ineligibleIDs []string) {
	t.Helper()
	eligibleID = insertCleanIntegrityItem(t, sanitizerSearchToken+" "+needles.controlSnippet, "untrusted", "clean")
	setItemProject(t, eligibleID, needles.project)
	seedEligibleProjection(t, eligibleID, needles.controlSnippet)

	type ineligibleCase struct {
		name  string
		setup func(text string) string
	}
	cases := []ineligibleCase{
		{"parse", func(text string) string {
			id := insertCleanIntegrityItem(t, text, "untrusted", "clean")
			patchItemProvenance(t, id, func(env map[string]any) { env["origin"] = "not-a-real-origin" })
			return id
		}},
		{"mismatch", func(text string) string {
			id := insertCleanIntegrityItem(t, text, "untrusted", "clean")
			forgeItemContentHash(t, id, forgedContentDigest)
			return id
		}},
		{"urlswap", func(text string) string {
			id := insertCleanIntegrityItem(t, text, "untrusted", "clean")
			plantURLSwapArtifact(t, id, needles)
			return id
		}},
		{"legacy", func(text string) string {
			return insertLegacyIntegrityItem(t, text)
		}},
		{"unknown", func(text string) string {
			return insertCleanIntegrityItem(t, text, "unknown", "clean")
		}},
		{"quarantine", func(text string) string {
			return insertCleanIntegrityItem(t, text, "quarantined", "pending")
		}},
		{"injection", func(text string) string {
			return insertCleanIntegrityItem(t, text, "untrusted", "pending")
		}},
	}
	for i, tc := range cases {
		text := fmt.Sprintf("%s %s %s", sanitizerSearchToken, tc.name, needles.text)
		id := tc.setup(text)
		plantIneligibleFields(t, id, i, needles)
		setItemProject(t, id, needles.project)
		ineligibleIDs = append(ineligibleIDs, id)
	}
	return eligibleID, ineligibleIDs
}

func seedEligibleProjection(t *testing.T, itemID, control string) {
	t.Helper()
	db := openTestDB(t)
	defer db.Close()
	for i := 0; i < 100; i++ {
		text := fmt.Sprintf("%s eligible-art-%03d", control, i)
		digest := provenance.SHA256Bytes([]byte(textnorm.Normalize(text)))
		if _, err := db.Exec(`insert into artifacts(id, source_id, item_id, external_id, kind, path, url, mime_type, text, content_hash, metadata_json)
values(?,?,?,?,?,?,?,?,?,?,?)`,
			fmt.Sprintf("art-elig-%03d", i), "integrity-src", itemID, fmt.Sprintf("art-elig-%03d", i),
			"note", fmt.Sprintf("eligible/art-%03d.md", i), fmt.Sprintf("https://example.test/elig/%03d", i),
			"text/plain", text, "sha256:"+digest, "{}"); err != nil {
			t.Fatal(err)
		}
	}
	for i := 0; i < 20; i++ {
		if _, err := db.Exec(`insert into relations(id, source_item_id, target_item_id, target_external_id, relation_type, confidence)
values(?,?,?,?,?,?)`,
			fmt.Sprintf("rel-elig-%03d", i), itemID, itemID, fmt.Sprintf("target-elig-%03d", i), "derived_from", 1.0); err != nil {
			t.Fatal(err)
		}
	}
}

func plantIneligibleFields(t *testing.T, itemID string, idx int, needles sanitizerNeedles) {
	t.Helper()
	db := openTestDB(t)
	defer db.Close()
	colID := fmt.Sprintf("needle-col-%d", idx)
	actorID := fmt.Sprintf("needle-actor-%d", idx)
	at := "2026-08-17T00:00:00Z"
	if _, err := db.Exec(`insert or ignore into collections(id, source_id, external_id, kind, name, metadata_json, created_at, updated_at) values(?,?,?,?,?,?,?,?)`,
		colID, "integrity-src", "col:"+colID, needles.collectionKind, needles.collectionName, "{}", at, at); err != nil {
		t.Fatal(err)
	}
	srcID := fmt.Sprintf("needle-src-%d", idx)
	if _, err := db.Exec(`insert or ignore into sources(id, kind, name, version, created_at, updated_at) values(?,?,?,?,?,?)`,
		srcID, needles.source, "NeedleSource", "1", at, at); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec(`insert or ignore into actors(id, source_id, external_id, type, name) values(?,?,?,?,?)`,
		actorID, srcID, "actor:"+actorID, needles.actorType, needles.actorName); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec(`update items set source_id=?, external_id=?, raw_hash=?, raw_path=?, collection_id=?, actor_id=?, created_at=?, summary=? where id=?`,
		srcID, needles.externalID, needles.rawHash, needles.rawPath, colID, actorID, needles.createdAt, needles.summary, itemID); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec(`update item_fts set source_kind=? where item_id=?`, needles.source, itemID); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec(`insert into artifacts(id, source_id, item_id, external_id, kind, path, url, mime_type, text, content_hash, metadata_json)
values(?,?,?,?,?,?,?,?,?,?,?)`,
		"art-needle-"+itemID, "integrity-src", itemID, "art:"+itemID, "note",
		needles.artPath, needles.artURL, needles.artMIME, needles.artText, needles.artHash, `{"free":"`+needles.metaFree+`"}`); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec(`insert into relations(id, source_item_id, target_external_id, relation_type, confidence) values(?,?,?,?,?)`,
		"rel-needle-"+itemID, itemID, needles.relTarget, needles.relType, 1.0); err != nil {
		t.Fatal(err)
	}
}

func plantURLSwapArtifact(t *testing.T, itemID string, needles sanitizerNeedles) {
	t.Helper()
	db := openTestDB(t)
	defer db.Close()
	url := "https://example.test/url-swap"
	digest := provenance.SHA256Bytes([]byte(url))
	if _, err := db.Exec(`insert into artifacts(id, source_id, item_id, external_id, kind, path, url, mime_type, text, content_hash, metadata_json)
values(?,?,?,?,?,?,?,?,?,?,?)`,
		"art-urlswap-"+itemID, "integrity-src", itemID, "art-url:"+itemID, "url",
		needles.artPath, url, needles.artMIME, needles.artText, "sha256:"+digest, "{}"); err != nil {
		t.Fatal(err)
	}
}

func setItemProject(t *testing.T, itemID, project string) {
	t.Helper()
	db := openTestDB(t)
	defer db.Close()
	if _, err := db.Exec(`insert or replace into item_metadata(item_id, key, value) values(?,?,?)`, itemID, "project", project); err != nil {
		t.Fatal(err)
	}
}

func writeTamperedEvidenceCache(t *testing.T, needles sanitizerNeedles, itemIDs []string) {
	t.Helper()
	results := make([]map[string]any, 0, len(itemIDs)+1)
	for _, id := range itemIDs {
		results = append(results, map[string]any{
			"id":          id,
			"external_id": needles.externalID,
			"snippet":     needles.snippet,
			"query":       needles.cacheQuery,
		})
	}
	results = append(results, map[string]any{"id": "missing-source-item", "snippet": needles.snippet})
	results = append(results, map[string]any{"id": cachedAttackerResultID, "snippet": needles.snippet})
	bundle := map[string]any{
		"id":           needles.cacheURI,
		"resource_uri": "miseledger://evidence/" + needles.cacheURI,
		"query":        needles.cacheQuery,
		"filters": map[string]any{
			"project":               needles.project,
			"from":                  needles.from,
			"to":                    needles.to,
			"source":                needles.source,
			"include_related":       true,
			"include_artifact_text": true,
		},
		"generated_at": "2026-08-21T00:00:00Z",
		"results":      results,
	}
	raw, err := json.MarshalIndent(bundle, "", "  ")
	if err != nil {
		t.Fatal(err)
	}
	dir := evidenceCacheDir()
	if err := os.MkdirAll(dir, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, needles.cacheID+".json"), append(raw, '\n'), 0o600); err != nil {
		t.Fatal(err)
	}
}

func writeListingPaddingCaches(t *testing.T, n int) {
	t.Helper()
	dir := evidenceCacheDir()
	if err := os.MkdirAll(dir, 0o700); err != nil {
		t.Fatal(err)
	}
	for i := 0; i < n; i++ {
		id := fmt.Sprintf("%024x", i+1)
		body := []byte(`{"id":"` + id + `","generated_at":"2026-08-21T00:00:00Z","results":[{"id":"pad"}]}` + "\n")
		if err := os.WriteFile(filepath.Join(dir, id+".json"), body, 0o600); err != nil {
			t.Fatal(err)
		}
	}
}

func assertSanitizedEvidencePayload(t *testing.T, raw []byte, needles sanitizerNeedles, expectControl, listing bool, minLeaves int) {
	t.Helper()
	var tree any
	if err := json.Unmarshal(raw, &tree); err != nil {
		t.Fatalf("payload is not JSON: %v\n%s", err, raw)
	}
	leaves := 0
	var leaked []string
	walkJSONLeaves(tree, func(s string) {
		leaves++
		for _, needle := range needles.all {
			if strings.Contains(s, needle) || sharesWindow(s, needle, needleWindow) {
				leaked = append(leaked, fmt.Sprintf("leaf %q shares needle window", truncateForTest(s)))
			}
		}
	})
	if leaves < minLeaves {
		t.Fatalf("vacuity floor: visited %d leaves, want >= %d", leaves, minLeaves)
	}
	if len(leaked) > 0 {
		t.Fatalf("attacker needle leaked:\n%s\n%s", strings.Join(leaked, "\n"), truncateForTest(string(raw)))
	}
	root, _ := tree.(map[string]any)
	if listing {
		assertListingShape(t, root)
		return
	}
	if _, ok := root["query"]; ok {
		t.Fatalf("root query survived sanitizer: %#v", root["query"])
	}
	if _, ok := root["filters"]; ok {
		t.Fatalf("root filters survived sanitizer: %#v", root["filters"])
	}
	assertResultStubs(t, root)
	if expectControl {
		blob := string(raw)
		if !strings.Contains(blob, needles.controlSnippet) {
			t.Fatalf("eligible positive control needle missing from payload")
		}
	}
}

func assertListingShape(t *testing.T, root map[string]any) {
	t.Helper()
	if _, ok := root["query"]; ok {
		t.Fatal("listing wrapper leaked query")
	}
	bundles, _ := root["bundles"].([]any)
	if len(bundles) == 0 {
		t.Fatal("listing wrapper missing bundles")
	}
	for _, raw := range bundles {
		entry, ok := raw.(map[string]any)
		if !ok {
			t.Fatalf("listing entry type %T", raw)
		}
		if len(entry) != 4 {
			t.Fatalf("listing entry keys = %#v, want exactly four", entry)
		}
		for _, key := range []string{"id", "resource_uri", "generated_at", "result_count"} {
			if _, ok := entry[key]; !ok {
				t.Fatalf("listing entry missing %s: %#v", key, entry)
			}
		}
	}
}

func assertResultStubs(t *testing.T, root map[string]any) {
	t.Helper()
	for _, raw := range bundleResultMaps(root) {
		if raw["eligibility_status"] != eligibilityIneligible {
			continue
		}
		if id, ok := raw["id"].(string); ok {
			if !evidenceBundleIDPattern.MatchString(id) {
				t.Fatalf("ineligible stub id = %#v, want 24 lowercase hex", raw["id"])
			}
			if len(raw) != 3 {
				t.Fatalf("ineligible stub with id must have exactly three keys: %#v", raw)
			}
			continue
		}
		if len(raw) != 2 {
			t.Fatalf("ineligible stub without id must keep only eligibility_status + reason_code: %#v", raw)
		}
	}
}

func walkJSONLeaves(v any, fn func(string)) {
	switch n := v.(type) {
	case map[string]any:
		for key, val := range n {
			fn(key)
			walkJSONLeaves(val, fn)
		}
	case []any:
		for _, val := range n {
			walkJSONLeaves(val, fn)
		}
	case string:
		fn(n)
	case json.Number:
		fn(n.String())
	case float64:
		fn(fmt.Sprint(n))
	case bool:
		fn(fmt.Sprint(n))
	}
}

func sharesWindow(leaf, needle string, window int) bool {
	if len(needle) < window {
		return strings.Contains(leaf, needle)
	}
	for i := 0; i+window <= len(needle); i++ {
		if strings.Contains(leaf, needle[i:i+window]) {
			return true
		}
	}
	return false
}

func decodeAny(t *testing.T, raw []byte) any {
	t.Helper()
	var tree any
	if err := json.Unmarshal(raw, &tree); err != nil {
		t.Fatal(err)
	}
	return tree
}

func truncateForTest(s string) string {
	if len(s) <= 240 {
		return s
	}
	return s[:240] + "..."
}
