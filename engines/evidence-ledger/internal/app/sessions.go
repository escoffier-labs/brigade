package app

import (
	"database/sql"
	"fmt"
	"io"
	"strings"
)

type SessionResult struct {
	SourceKind           string `json:"source_kind"`
	SourceName           string `json:"source_name,omitempty"`
	CollectionExternalID string `json:"collection_external_id"`
	CollectionName       string `json:"collection_name"`
	CollectionKind       string `json:"collection_kind"`
	ItemCount            int    `json:"item_count"`
	MatchCount           int    `json:"match_count,omitempty"`
	FirstSeen            string `json:"first_seen"`
	LastSeen             string `json:"last_seen"`
	SampleItemID         string `json:"sample_item_id,omitempty"`
	RawPath              string `json:"raw_path,omitempty"`
	RawOrdinal           int64  `json:"raw_ordinal,omitempty"`
	Snippet              string `json:"snippet,omitempty"`
	// Preview is a short excerpt of the session's opening human message (or
	// first item), so a row conveys what the session is about at a glance.
	Preview   string `json:"preview,omitempty"`
	Project   string `json:"project,omitempty"`
	Workspace string `json:"workspace,omitempty"`
	Model     string `json:"model,omitempty"`
	Harness   string `json:"harness,omitempty"`
}

type SessionFilters struct {
	Source  string
	Project string
	Model   string
}

func cmdSessions(args []string, out, errw io.Writer) int {
	if len(args) == 0 {
		return fatalf(errw, "usage: miseledger sessions list|search")
	}
	switch args[0] {
	case "list":
		return cmdSessionsList(args[1:], out, errw)
	case "search":
		return cmdSessionsSearch(args[1:], out, errw)
	default:
		return fatalf(errw, "usage: miseledger sessions list|search")
	}
}

func cmdSessionsList(args []string, out, errw io.Writer) int {
	values, bools, rest, err := splitFlags(args, map[string]bool{"source": true, "project": true, "model": true, "limit": true}, map[string]bool{"json": true})
	if err != nil {
		return fatalf(errw, "sessions list: %s", err)
	}
	if len(rest) != 0 {
		return fatalf(errw, "usage: miseledger sessions list [--source KIND] [--project NAME] [--model NAME] [--limit N] [--json]")
	}
	limit, err := parseLimit(values["limit"], 50)
	if err != nil {
		return fatalf(errw, "sessions list: %s", err)
	}
	db, _, err := openMigrated()
	if err != nil {
		return fatalf(errw, "sessions list: %s", err)
	}
	defer db.Close()
	filters := SessionFilters{
		Source:  strings.TrimSpace(values["source"]),
		Project: strings.TrimSpace(values["project"]),
		Model:   strings.TrimSpace(values["model"]),
	}
	rows, err := listSessions(db, filters, limit)
	if err != nil {
		return fatalf(errw, "sessions list: %s", err)
	}
	writeSessions(rows, bools["json"], out)
	return 0
}

func cmdSessionsSearch(args []string, out, errw io.Writer) int {
	values, bools, rest, err := splitFlags(args, map[string]bool{"source": true, "project": true, "model": true, "limit": true}, map[string]bool{"json": true})
	if err != nil {
		return fatalf(errw, "sessions search: %s", err)
	}
	if len(rest) == 0 {
		return fatalf(errw, "usage: miseledger sessions search <query> [--source KIND] [--project NAME] [--model NAME] [--limit N] [--json]")
	}
	limit, err := parseLimit(values["limit"], 20)
	if err != nil {
		return fatalf(errw, "sessions search: %s", err)
	}
	db, _, err := openMigrated()
	if err != nil {
		return fatalf(errw, "sessions search: %s", err)
	}
	defer db.Close()
	filters := SessionFilters{
		Source:  strings.TrimSpace(values["source"]),
		Project: strings.TrimSpace(values["project"]),
		Model:   strings.TrimSpace(values["model"]),
	}
	rows, err := searchSessions(db, strings.Join(rest, " "), filters, limit)
	if err != nil {
		return fatalf(errw, "sessions search: %s", err)
	}
	if bools["json"] {
		writeJSON(out, map[string]any{"query": strings.Join(rest, " "), "sessions": rows})
	} else {
		writeSessions(rows, false, out)
	}
	return 0
}

func writeSessions(rows []SessionResult, asJSON bool, out io.Writer) {
	if asJSON {
		writeJSON(out, map[string]any{"sessions": rows})
		return
	}
	for _, row := range rows {
		fmt.Fprintf(out, "%s %s items=%d matches=%d last=%s path=%s\n", row.SourceKind, row.CollectionExternalID, row.ItemCount, row.MatchCount, row.LastSeen, row.RawPath)
		if row.Snippet != "" {
			fmt.Fprintf(out, "  %s\n", row.Snippet)
		}
	}
}

func appendSessionFilters(where []string, params []any, filters SessionFilters) ([]string, []any) {
	if filters.Source != "" {
		where = append(where, "s.kind = ?")
		params = append(params, filters.Source)
	}
	if filters.Project != "" {
		where = append(where, `exists(
select 1 from items fi
join item_metadata fm on fm.item_id = fi.id
where fi.collection_id = c.id
  and fm.key in ('project','workspace','workspace_dir','cwd')
  and (fm.value = ? or fm.value like ?)
)`)
		params = append(params, filters.Project, "%"+filters.Project+"%")
	}
	if filters.Model != "" {
		where = append(where, `exists(
select 1 from items fi
join item_metadata fm on fm.item_id = fi.id
where fi.collection_id = c.id
  and fm.key = 'model'
  and (fm.value = ? or fm.value like ?)
)`)
		params = append(params, filters.Model, "%"+filters.Model+"%")
	}
	return where, params
}

func addSessionMetadata(db *sql.DB, result *SessionResult, collectionID string, filters SessionFilters) error {
	return db.QueryRow(`select
coalesce(
  min(case when im.key = 'project' and (?2 = '' or im.value = ?2 or im.value like ?3) then im.value end),
  ''
),
coalesce(
  min(case when im.key = 'workspace' and (?2 = '' or im.value = ?2 or im.value like ?3) then im.value end),
  min(case when im.key = 'workspace_dir' and (?2 = '' or im.value = ?2 or im.value like ?3) then im.value end),
  min(case when im.key = 'cwd' and (?2 = '' or im.value = ?2 or im.value like ?3) then im.value end),
  min(case when im.key = 'workspace' then im.value end),
  min(case when im.key = 'workspace_dir' then im.value end),
  min(case when im.key = 'cwd' then im.value end),
  ''
),
coalesce(
  min(case when im.key = 'model' and (?4 = '' or im.value = ?4 or im.value like ?5) then im.value end),
  min(case when im.key = 'model' then im.value end),
  ''
),
coalesce(min(case when im.key = 'harness' then im.value end),'')
from item_metadata im
join items i on i.id = im.item_id
where i.collection_id = ?1
  and im.key in ('project','workspace','workspace_dir','cwd','model','harness')
  and im.value != ''`, collectionID, filters.Project, "%"+filters.Project+"%", filters.Model, "%"+filters.Model+"%").Scan(
		&result.Project,
		&result.Workspace,
		&result.Model,
		&result.Harness,
	)
}

func listSessions(db *sql.DB, filters SessionFilters, limit int) ([]SessionResult, error) {
	if limit <= 0 || limit > 200 {
		limit = 50
	}
	where := []string{"c.kind in ('agent_session','conversation')"}
	params := []any{}
	where, params = appendSessionFilters(where, params, filters)
	params = append(params, limit)
	sqlText := `select s.kind, coalesce(s.name,''), c.id, c.external_id, c.name, c.kind, count(i.id), coalesce(min(i.created_at),'') as first_seen, coalesce(max(i.created_at),'') as last_seen,
coalesce((select ii.id from items ii where ii.collection_id = c.id order by coalesce(ii.created_at,'') desc, ii.id desc limit 1),''),
coalesce((select ii.raw_path from items ii where ii.collection_id = c.id and coalesce(ii.raw_path,'') != '' order by coalesce(ii.created_at,'') desc, ii.id desc limit 1),''),
coalesce((select ii.raw_ordinal from items ii where ii.collection_id = c.id and ii.raw_ordinal is not null order by coalesce(ii.created_at,'') desc, ii.id desc limit 1),0)
from collections c
join sources s on s.id = c.source_id
join items i on i.collection_id = c.id
where ` + strings.Join(where, " and ") + `
group by c.id, s.kind, s.name, c.external_id, c.name, c.kind
order by last_seen desc, c.name
limit ?`
	rows, err := db.Query(sqlText, params...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []SessionResult
	for rows.Next() {
		var row SessionResult
		var collectionID string
		if err := rows.Scan(&row.SourceKind, &row.SourceName, &collectionID, &row.CollectionExternalID, &row.CollectionName, &row.CollectionKind, &row.ItemCount, &row.FirstSeen, &row.LastSeen, &row.SampleItemID, &row.RawPath, &row.RawOrdinal); err != nil {
			return nil, err
		}
		if err := addSessionMetadata(db, &row, collectionID, filters); err != nil {
			return nil, err
		}
		row.Preview = sessionPreview(db, collectionID)
		out = append(out, row)
	}
	return out, rows.Err()
}

// sessionPreview returns a short excerpt of a session's opening human message,
// falling back to its first item, so callers can show what a session contains
// without opening it. It returns "" on any error.
func sessionPreview(db *sql.DB, collectionID string) string {
	var text string
	err := db.QueryRow(`select i.text
from items i
left join actors a on a.id = i.actor_id
where i.collection_id = ?
order by (case when a.type = 'human' then 0 else 1 end), coalesce(i.created_at,''), i.id
limit 1`, collectionID).Scan(&text)
	if err != nil {
		return ""
	}
	text = strings.Join(strings.Fields(text), " ")
	if len(text) > 200 {
		text = text[:200] + "…"
	}
	return text
}

// sessionItems returns the ordered items of one session collection so the UI
// can show a transcript. It is a non-FTS browse path (no query required).
func sessionItems(db *sql.DB, externalID, sourceKind string, limit int) ([]map[string]any, error) {
	if limit <= 0 || limit > 500 {
		limit = 200
	}
	rows, err := db.Query(`select i.id, i.kind, coalesce(a.type,''), coalesce(a.name,''), coalesce(i.created_at,''), i.text
from items i
join collections c on c.id = i.collection_id
join sources s on s.id = i.source_id
left join actors a on a.id = i.actor_id
where c.external_id = ? and s.kind = ?
order by coalesce(i.created_at,''), i.id
limit ?`, externalID, sourceKind, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []map[string]any{}
	for rows.Next() {
		var id, kind, actorType, actorName, createdAt, text string
		if err := rows.Scan(&id, &kind, &actorType, &actorName, &createdAt, &text); err != nil {
			return nil, err
		}
		out = append(out, map[string]any{
			"id": id, "kind": kind, "actor_type": actorType,
			"actor_name": actorName, "created_at": createdAt, "text": text,
		})
	}
	return out, rows.Err()
}

func searchSessions(db *sql.DB, query string, filters SessionFilters, limit int) ([]SessionResult, error) {
	if limit <= 0 || limit > 100 {
		limit = 20
	}
	where := []string{"item_fts match ?", "c.kind in ('agent_session','conversation')"}
	params := []any{ftsQuery(query)}
	where, params = appendSessionFilters(where, params, filters)
	params = append(params, limit*10)
	sqlText := `select i.id, s.kind, coalesce(s.name,''), c.id, c.external_id, c.name, c.kind, coalesce(i.created_at,''), coalesce(i.raw_path,''), coalesce(i.raw_ordinal,0), snippet(item_fts, 5, '[', ']', '...', 20), bm25(item_fts)
from item_fts
join items i on i.id = item_fts.item_id
join sources s on s.id = i.source_id
join collections c on c.id = i.collection_id
where ` + strings.Join(where, " and ") + `
order by bm25(item_fts), i.created_at desc, i.id
limit ?`
	rows, err := db.Query(sqlText, params...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	type hit struct {
		itemID, sourceKind, sourceName, collectionID, externalID, name, kind, createdAt, rawPath, snippet string
		rawOrdinal                                                                                        int64
	}
	grouped := map[string]*SessionResult{}
	order := []string{}
	for rows.Next() {
		var h hit
		var score float64
		if err := rows.Scan(&h.itemID, &h.sourceKind, &h.sourceName, &h.collectionID, &h.externalID, &h.name, &h.kind, &h.createdAt, &h.rawPath, &h.rawOrdinal, &h.snippet, &score); err != nil {
			return nil, err
		}
		row := grouped[h.collectionID]
		if row == nil {
			if len(order) >= limit {
				continue
			}
			count, first, last, err := sessionStats(db, h.collectionID)
			if err != nil {
				return nil, err
			}
			row = &SessionResult{
				SourceKind:           h.sourceKind,
				SourceName:           h.sourceName,
				CollectionExternalID: h.externalID,
				CollectionName:       h.name,
				CollectionKind:       h.kind,
				ItemCount:            count,
				FirstSeen:            first,
				LastSeen:             last,
				SampleItemID:         h.itemID,
				RawPath:              h.rawPath,
				RawOrdinal:           h.rawOrdinal,
				Snippet:              h.snippet,
			}
			if err := addSessionMetadata(db, row, h.collectionID, filters); err != nil {
				return nil, err
			}
			row.Preview = sessionPreview(db, h.collectionID)
			grouped[h.collectionID] = row
			order = append(order, h.collectionID)
		}
		row.MatchCount++
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	out := make([]SessionResult, 0, minInt(limit, len(order)))
	for _, id := range order {
		out = append(out, *grouped[id])
		if len(out) >= limit {
			break
		}
	}
	return out, nil
}

func sessionStats(db *sql.DB, collectionID string) (count int, first, last string, err error) {
	err = db.QueryRow(`select count(id), coalesce(min(created_at),''), coalesce(max(created_at),'') from items where collection_id = ?`, collectionID).Scan(&count, &first, &last)
	return count, first, last, err
}

func minInt(a, b int) int {
	if a < b {
		return a
	}
	return b
}
