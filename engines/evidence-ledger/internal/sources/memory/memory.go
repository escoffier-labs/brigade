package memory

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"unicode"

	"github.com/escoffier-labs/miseledger/internal/adapter"
	"github.com/escoffier-labs/miseledger/internal/sources"
	"github.com/escoffier-labs/miseledger/internal/textnorm"
)

const (
	SourceKind    = "brigade-memory"
	SourceName    = "Brigade Memory Cards"
	SourceVersion = "1.0.0"
	// LegacyCollectionID is the pre-namespace collection. Namespaced crawls
	// never tombstone or rebuild these rows (scoped-rebuild). Empty-namespace
	// status/doctor health dual-reads live counts across all collections,
	// including this legacy id.
	LegacyCollectionID = "memory:cards"
	CollectionKind     = "memory_cards"
	ItemKind           = "memory_card"
	IdentityExplicit   = "explicit_id"
	IdentityPath       = "path"
	MaxCardBytes       = 2 * 1024 * 1024
	MaxTextBytes       = 256 * 1024
	MaxUnknownKeys     = 32
	NamespaceFileRel   = "memory/NAMESPACE"
)

// namespacePattern matches operator-declared opaque memory-<uuid4> identifiers
// with RFC 4122 version 4 and variant bits (8/9/a/b).
var namespacePattern = regexp.MustCompile(`^memory-[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-4[0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$`)

// CardRoots are the default relative directories scanned under a workspace.
var CardRoots = []string{"memory/cards"}

// RelationKinds are explicit frontmatter relation keys Slice 1a understands.
var RelationKinds = []string{
	"derived_from",
	"created_by",
	"reinforced_by",
	"supported_by",
	"supersedes",
	"contradicts",
}

// CardOutcome is one walk result used by crawl receipts.
type CardOutcome struct {
	ExternalID     string
	ContentHash    string
	Fingerprint    string
	RawPath        string
	IdentitySource string
	Outcome        string // created/updated/unchanged filled by crawl; skipped/failed here
	Warning        string
	Record         *adapter.Record
}

// ResolveNamespace reads the operator-declared opaque memory-<uuid4> from
// memory/NAMESPACE. The engine never derives this from a clone path.
func ResolveNamespace(workspace string) (string, error) {
	path := filepath.Join(workspace, filepath.FromSlash(NamespaceFileRel))
	body, err := os.ReadFile(path)
	if err != nil {
		if os.IsNotExist(err) {
			return "", fmt.Errorf("missing operator-declared memory namespace at %s (expected opaque memory-<uuid4>)", NamespaceFileRel)
		}
		return "", err
	}
	ns := strings.TrimSpace(string(body))
	if ns == "" {
		return "", fmt.Errorf("empty memory namespace in %s", NamespaceFileRel)
	}
	if !namespacePattern.MatchString(ns) {
		return "", fmt.Errorf("invalid memory namespace %q in %s (want memory-<uuid4> with version 4 and RFC variant 8/9/a/b)", ns, NamespaceFileRel)
	}
	return ns, nil
}

// CollectionExternalID returns the namespaced collection id for a memory root.
func CollectionExternalID(namespace string) string {
	return namespace
}

// Generate walks workspace memory cards and emits miseledger.adapter.v1 records.
func Generate(path string, opts sources.Options, w io.Writer) (sources.Result, error) {
	outcomes, result, err := Walk(path, opts)
	if err != nil {
		return result, err
	}
	for _, card := range outcomes {
		if card.Record == nil {
			continue
		}
		rec := *card.Record
		sources.ApplyRedaction(&rec, opts)
		if err := sources.WriteRecord(w, rec); err != nil {
			return result, err
		}
		result.Records++
		if opts.AfterFile != nil {
			scan, scanErr := sources.NewFileScan(filepath.Join(path, filepath.FromSlash(card.RawPath)))
			if scanErr == nil {
				scan.ContentHash = card.ContentHash
				scan.Records = 1
				if card.Warning != "" {
					scan.Warnings = 1
				}
				if err := opts.AfterFile(scan); err != nil {
					return result, err
				}
			}
		}
	}
	return result, nil
}

// Walk inspects cards under path without writing adapter output.
func Walk(root string, opts sources.Options) ([]CardOutcome, sources.Result, error) {
	abs, err := filepath.Abs(root)
	if err != nil {
		return nil, sources.Result{}, err
	}
	info, err := os.Stat(abs)
	if err != nil {
		return nil, sources.Result{}, err
	}
	if !info.IsDir() {
		return nil, sources.Result{}, fmt.Errorf("memory crawl path must be a workspace directory: %s", abs)
	}
	namespace, err := ResolveNamespace(abs)
	if err != nil {
		return nil, sources.Result{}, err
	}

	var outcomes []CardOutcome
	var result sources.Result
	files, err := listCardFiles(abs)
	if err != nil {
		return nil, result, err
	}
	result.Files = make([]sources.FileScan, 0, len(files))

	for _, full := range files {
		if opts.Limit > 0 && countEmitted(outcomes) >= opts.Limit {
			break
		}
		rel, err := filepath.Rel(abs, full)
		if err != nil {
			return nil, result, err
		}
		rel = normalizeRelPath(rel)
		scan, err := sources.NewFileScan(full)
		if err != nil {
			outcomes = append(outcomes, CardOutcome{RawPath: rel, Outcome: "failed", Warning: err.Error()})
			result.Warnings = append(result.Warnings, rel+": "+err.Error())
			continue
		}
		if scan.Size > MaxCardBytes {
			msg := fmt.Sprintf("card too large (%d bytes > %d limit), skipped", scan.Size, MaxCardBytes)
			outcomes = append(outcomes, CardOutcome{
				ExternalID:     "path:" + rel,
				RawPath:        rel,
				IdentitySource: IdentityPath,
				Outcome:        "skipped",
				Warning:        msg,
			})
			result.Warnings = append(result.Warnings, rel+": "+msg)
			scan.Warnings = 1
			result.Files = append(result.Files, scan)
			continue
		}
		skip, err := scan.Prepare(opts)
		if err != nil {
			outcomes = append(outcomes, CardOutcome{RawPath: rel, Outcome: "failed", Warning: err.Error()})
			result.Warnings = append(result.Warnings, rel+": "+err.Error())
			continue
		}
		if skip {
			// Incremental skip still needs identity for reconcile manifests.
			body, readErr := os.ReadFile(full)
			if readErr != nil {
				outcomes = append(outcomes, CardOutcome{RawPath: rel, Outcome: "failed", Warning: readErr.Error()})
				result.Warnings = append(result.Warnings, rel+": "+readErr.Error())
				continue
			}
			card, warn := buildCard(abs, namespace, rel, body, scan.MTime)
			if card.Outcome == "failed" || card.Outcome == "skipped" {
				outcomes = append(outcomes, card)
				if warn != "" {
					result.Warnings = append(result.Warnings, warn)
				}
				result.Files = append(result.Files, scan)
				continue
			}
			card.Outcome = "unchanged"
			outcomes = append(outcomes, card)
			result.Files = append(result.Files, scan)
			continue
		}
		body, err := os.ReadFile(full)
		if err != nil {
			outcomes = append(outcomes, CardOutcome{RawPath: rel, Outcome: "failed", Warning: err.Error()})
			result.Warnings = append(result.Warnings, rel+": "+err.Error())
			continue
		}
		hash := "sha256:" + hex.EncodeToString(hashBytes(body))
		scan.ContentHash = hash
		card, warn := buildCard(abs, namespace, rel, body, scan.MTime)
		card.ContentHash = contentHashForRecord(card.Record)
		if card.ContentHash == "" {
			card.ContentHash = hash
		}
		if card.Record != nil {
			scan.Records = 1
		}
		if warn != "" {
			result.Warnings = append(result.Warnings, warn)
			scan.Warnings = 1
		}
		outcomes = append(outcomes, card)
		result.Files = append(result.Files, scan)
	}

	if dupWarns := detectDuplicateExplicitIDs(outcomes); len(dupWarns) > 0 {
		result.Warnings = append(result.Warnings, dupWarns...)
	}
	if fpWarns := detectDuplicateFingerprints(outcomes); len(fpWarns) > 0 {
		result.Warnings = append(result.Warnings, fpWarns...)
	}
	return outcomes, result, nil
}

// DuplicateExplicitIDs returns external IDs that appear more than once among
// successfully parsed cards. Callers must fail the scan before reconciliation.
func DuplicateExplicitIDs(outcomes []CardOutcome) []string {
	counts := map[string]int{}
	for _, o := range outcomes {
		if o.Record == nil || o.ExternalID == "" {
			continue
		}
		if o.IdentitySource != IdentityExplicit {
			continue
		}
		counts[o.ExternalID]++
	}
	var dups []string
	for id, n := range counts {
		if n > 1 {
			dups = append(dups, id)
		}
	}
	return dups
}

func detectDuplicateExplicitIDs(outcomes []CardOutcome) []string {
	dups := DuplicateExplicitIDs(outcomes)
	var warns []string
	for _, id := range dups {
		warns = append(warns, "duplicate explicit id "+id+": scan must fail before reconciliation")
	}
	return warns
}

func detectDuplicateFingerprints(outcomes []CardOutcome) []string {
	byFP := map[string][]string{}
	for _, o := range outcomes {
		if o.Record == nil || o.Fingerprint == "" {
			continue
		}
		byFP[o.Fingerprint] = append(byFP[o.Fingerprint], o.ExternalID)
	}
	var warns []string
	for fp, ids := range byFP {
		if len(ids) < 2 {
			continue
		}
		warns = append(warns, fmt.Sprintf("duplicate content fingerprint %s across identities %s (fingerprint is not card identity)", fp, strings.Join(ids, ", ")))
	}
	return warns
}

func countEmitted(outcomes []CardOutcome) int {
	n := 0
	for _, o := range outcomes {
		if o.Record != nil {
			n++
		}
	}
	return n
}

func listCardFiles(root string) ([]string, error) {
	var files []string
	for _, relRoot := range CardRoots {
		dir := filepath.Join(root, filepath.FromSlash(relRoot))
		info, err := os.Stat(dir)
		if err != nil {
			if os.IsNotExist(err) {
				continue
			}
			return nil, err
		}
		if !info.IsDir() {
			continue
		}
		err = filepath.WalkDir(dir, func(path string, d os.DirEntry, err error) error {
			if err != nil {
				return err
			}
			if d.IsDir() {
				name := strings.ToLower(d.Name())
				if name == "decay" || name == "backup" || name == "backups" {
					return filepath.SkipDir
				}
				return nil
			}
			if d.Type()&os.ModeSymlink != 0 {
				return nil
			}
			if strings.EqualFold(filepath.Ext(d.Name()), ".md") {
				files = append(files, path)
			}
			return nil
		})
		if err != nil {
			return nil, err
		}
	}
	return files, nil
}

func buildCard(workspace, namespace, rel string, body []byte, mtime string) (CardOutcome, string) {
	text := string(body)
	meta, hasFrontmatter, malformed := parseFrontmatter(text)
	if malformed {
		msg := rel + ": malformed frontmatter, skipped"
		return CardOutcome{
			ExternalID:     "path:" + normalizeRelPath(rel),
			RawPath:        rel,
			IdentitySource: IdentityPath,
			Outcome:        "skipped",
			Warning:        msg,
		}, msg
	}

	externalID, identitySource := cardIdentity(meta, rel)
	fingerprint := textnorm.ContentFingerprint(text)
	markdownBody := bodyAfterFrontmatter(text, hasFrontmatter)
	summary := truncateText(firstNonEmpty(stringField(meta, "summary"), stringField(meta, "description"), stringField(meta, "title"), stringField(meta, "topic")), MaxTextBytes)
	tags := stringList(meta, "tags")
	if len(tags) == 0 {
		tags = []string{"memory-card"}
	} else {
		tags = appendUnique(tags, "memory-card")
	}

	itemText := strings.TrimSpace(markdownBody)
	truncated := false
	if len(itemText) > MaxTextBytes {
		itemText = truncateText(itemText, MaxTextBytes)
		truncated = true
	}

	unknown := unknownFrontmatter(meta)
	itemMeta := map[string]any{
		"identity_source":     identitySource,
		"relative_path":       rel,
		"workspace":           filepath.Base(workspace),
		"memory_namespace":    namespace,
		"has_frontmatter":     hasFrontmatter,
		"topic":               stringField(meta, "topic"),
		"title":               stringField(meta, "title"),
		"category":            stringField(meta, "category"),
		"card_id":             stringField(meta, "id", "card_id"),
		"truncated_text":      truncated,
		"content_fingerprint": fingerprint,
		"unknown_frontmatter": unknown,
	}
	for _, key := range []string{"confidence", "last_reviewed", "fresh_until", "status"} {
		if v := stringField(meta, key); v != "" {
			itemMeta[key] = v
		}
	}

	var summaryPtr *string
	if summary != "" {
		summaryPtr = &summary
	}

	rec := adapter.Record{
		Schema: adapter.SchemaV1,
		Source: adapter.Source{Kind: SourceKind, Name: SourceName, Version: SourceVersion},
		Collection: adapter.Collection{
			ExternalID: CollectionExternalID(namespace),
			Kind:       CollectionKind,
			Name:       "Memory cards",
			Metadata: sources.Metadata(map[string]any{
				"workspace":        filepath.Base(workspace),
				"memory_namespace": namespace,
			}),
		},
		Item: adapter.Item{
			ExternalID: externalID,
			Kind:       ItemKind,
			CreatedAt:  mtime,
			UpdatedAt:  mtime,
			Text:       itemText,
			Summary:    summaryPtr,
			Tags:       tags,
			Metadata:   sources.Metadata(itemMeta),
		},
		Actor: &adapter.Actor{
			ExternalID: SourceKind + ":system:memory",
			Type:       "system",
			Name:       "memory-projection",
		},
		Relations: explicitRelations(meta),
		Raw: adapter.RawRef{
			Format: "markdown",
			Hash:   "sha256:" + hex.EncodeToString(hashBytes(body)),
			Path:   rel,
		},
	}

	outcome := CardOutcome{
		ExternalID:     externalID,
		RawPath:        rel,
		IdentitySource: identitySource,
		Fingerprint:    fingerprint,
		Record:         &rec,
		ContentHash:    contentHashForRecord(&rec),
	}
	return outcome, ""
}

const truncationMarker = "\n[truncated]"

func truncateText(text string, limit int) string {
	if len(text) <= limit {
		return text
	}
	if limit <= len(truncationMarker) {
		return truncationMarker[:limit]
	}
	cut := limit - len(truncationMarker)
	for cut > 0 && (text[cut]&0xc0) == 0x80 {
		cut--
	}
	return text[:cut] + truncationMarker
}

func cardIdentity(meta map[string]any, rel string) (string, string) {
	if id := stringField(meta, "id", "card_id"); id != "" {
		return id, IdentityExplicit
	}
	return "path:" + normalizeRelPath(rel), IdentityPath
}

func explicitRelations(meta map[string]any) []adapter.Relation {
	var out []adapter.Relation
	for _, kind := range RelationKinds {
		targetExternal := stringField(meta, kind)
		if targetExternal == "" {
			continue
		}
		targetSource := stringField(meta, kind+"_target_source", kind+"_source")
		targetCollection := stringField(meta, kind+"_target_collection", kind+"_collection")
		rel := adapter.Relation{Type: kind}
		if targetSource != "" || targetCollection != "" {
			rel.Target = &adapter.RelationTarget{
				Source:     targetSource,
				Collection: targetCollection,
				ExternalID: targetExternal,
			}
		} else {
			rel.TargetExternalID = targetExternal
		}
		out = append(out, rel)
	}
	return out
}

func contentHashForRecord(rec *adapter.Record) string {
	if rec == nil {
		return ""
	}
	summary := ""
	if rec.Item.Summary != nil {
		summary = *rec.Item.Summary
	}
	body := textnorm.Normalize(strings.TrimSpace(rec.Item.Text + "\n" + summary))
	sum := sha256.Sum256([]byte(body))
	return "sha256:" + hex.EncodeToString(sum[:])
}

func normalizeRelPath(rel string) string {
	rel = filepath.ToSlash(rel)
	rel = strings.TrimPrefix(rel, "./")
	return rel
}

func hashBytes(b []byte) []byte {
	sum := sha256.Sum256(b)
	return sum[:]
}

// parseFrontmatter mirrors brigade.memory_cmd._parse_frontmatter: flat keys,
// inline [lists], booleans, and quote stripping. It does not execute YAML.
func parseFrontmatter(text string) (meta map[string]any, has bool, malformed bool) {
	if !strings.HasPrefix(text, "---\n") && !strings.HasPrefix(text, "---\r\n") {
		return map[string]any{}, false, false
	}
	lines := splitLines(text)
	if len(lines) == 0 || strings.TrimSpace(lines[0]) != "---" {
		return map[string]any{}, false, false
	}
	end := -1
	for i := 1; i < len(lines); i++ {
		if strings.TrimSpace(lines[i]) == "---" {
			end = i
			break
		}
	}
	if end < 0 {
		return map[string]any{}, false, true
	}
	data := map[string]any{}
	for _, raw := range lines[1:end] {
		if !strings.Contains(raw, ":") {
			continue
		}
		key, value, ok := strings.Cut(raw, ":")
		if !ok {
			continue
		}
		key = strings.TrimSpace(key)
		value = strings.TrimSpace(value)
		if key == "" {
			continue
		}
		if strings.HasPrefix(value, "[") && strings.HasSuffix(value, "]") {
			data[key] = parseInlineList(value)
			continue
		}
		lower := strings.ToLower(value)
		if lower == "true" || lower == "false" {
			data[key] = lower == "true"
			continue
		}
		data[key] = trimQuotes(value)
	}
	return data, true, false
}

func bodyAfterFrontmatter(text string, has bool) string {
	if !has {
		return text
	}
	lines := splitLines(text)
	end := -1
	for i := 1; i < len(lines); i++ {
		if strings.TrimSpace(lines[i]) == "---" {
			end = i
			break
		}
	}
	if end < 0 || end+1 >= len(lines) {
		return ""
	}
	return strings.Join(lines[end+1:], "\n")
}

func splitLines(text string) []string {
	text = strings.ReplaceAll(text, "\r\n", "\n")
	return strings.Split(text, "\n")
}

func parseInlineList(value string) []string {
	inner := strings.TrimSpace(strings.TrimSuffix(strings.TrimPrefix(value, "["), "]"))
	if inner == "" {
		return []string{}
	}
	parts := strings.Split(inner, ",")
	out := make([]string, 0, len(parts))
	for _, part := range parts {
		part = trimQuotes(strings.TrimSpace(part))
		if part != "" {
			out = append(out, part)
		}
	}
	return out
}

func trimQuotes(s string) string {
	if len(s) >= 2 {
		if (s[0] == '\'' && s[len(s)-1] == '\'') || (s[0] == '"' && s[len(s)-1] == '"') {
			return s[1 : len(s)-1]
		}
	}
	return s
}

func stringField(meta map[string]any, keys ...string) string {
	for _, key := range keys {
		v, ok := meta[key]
		if !ok || v == nil {
			continue
		}
		switch t := v.(type) {
		case string:
			if strings.TrimSpace(t) != "" {
				return strings.TrimSpace(t)
			}
		case bool:
			return strconv.FormatBool(t)
		case []string:
			if len(t) > 0 {
				return strings.Join(t, ", ")
			}
		}
	}
	return ""
}

func stringList(meta map[string]any, key string) []string {
	v, ok := meta[key]
	if !ok || v == nil {
		return nil
	}
	switch t := v.(type) {
	case []string:
		return append([]string{}, t...)
	case string:
		if strings.TrimSpace(t) == "" {
			return nil
		}
		return []string{strings.TrimSpace(t)}
	default:
		return nil
	}
}

func unknownFrontmatter(meta map[string]any) map[string]any {
	known := map[string]bool{
		"id": true, "card_id": true, "topic": true, "title": true, "description": true,
		"summary": true, "category": true, "tags": true, "confidence": true,
		"last_reviewed": true, "fresh_until": true, "status": true,
		"evidence": true, "sources": true, "source": true, "refs": true, "links": true,
		"fingerprint": true, "reinforcements": true,
	}
	for _, kind := range RelationKinds {
		known[kind] = true
		known[kind+"_target_source"] = true
		known[kind+"_source"] = true
		known[kind+"_target_collection"] = true
		known[kind+"_collection"] = true
	}
	out := map[string]any{}
	for k, v := range meta {
		if known[k] {
			continue
		}
		if len(out) >= MaxUnknownKeys {
			break
		}
		switch t := v.(type) {
		case string:
			if len(t) > 256 {
				t = t[:256]
			}
			out[k] = t
		case bool, []string:
			out[k] = t
		default:
			out[k] = fmt.Sprint(t)
		}
	}
	return out
}

func appendUnique(list []string, value string) []string {
	for _, v := range list {
		if v == value {
			return list
		}
	}
	return append(list, value)
}

func firstNonEmpty(values ...string) string {
	for _, v := range values {
		if strings.TrimSpace(v) != "" {
			return strings.TrimSpace(v)
		}
	}
	return ""
}

// IsSafeRelPath rejects absolute and traversal paths for diagnostics.
func IsSafeRelPath(rel string) bool {
	if rel == "" || filepath.IsAbs(rel) {
		return false
	}
	clean := filepath.Clean(rel)
	if strings.HasPrefix(clean, "..") {
		return false
	}
	for _, r := range rel {
		if r == 0 || unicode.IsControl(r) {
			return false
		}
	}
	return true
}
