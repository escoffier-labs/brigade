package adapter

import (
	"encoding/json"
	"errors"
	"fmt"
	"strings"
)

const SchemaV1 = "miseledger.adapter.v1"

type Record struct {
	Schema     string          `json:"schema"`
	Source     Source          `json:"source"`
	Collection Collection      `json:"collection"`
	Item       Item            `json:"item"`
	Actor      *Actor          `json:"actor"`
	Artifacts  []Artifact      `json:"artifacts"`
	Links      []Link          `json:"links"`
	Relations  []Relation      `json:"relations"`
	Raw        RawRef          `json:"raw"`
	Unknown    json.RawMessage `json:"-"`
}

type Source struct {
	Kind    string `json:"kind"`
	Name    string `json:"name"`
	Version string `json:"version"`
}

type Collection struct {
	ExternalID string          `json:"external_id"`
	Kind       string          `json:"kind"`
	Name       string          `json:"name"`
	Metadata   json.RawMessage `json:"metadata"`
}

type Item struct {
	ExternalID string          `json:"external_id"`
	Kind       string          `json:"kind"`
	CreatedAt  string          `json:"created_at"`
	UpdatedAt  string          `json:"updated_at"`
	Text       string          `json:"text"`
	Summary    *string         `json:"summary"`
	Tags       []string        `json:"tags"`
	Metadata   json.RawMessage `json:"metadata"`
}

type Actor struct {
	ExternalID string          `json:"external_id"`
	Type       string          `json:"type"`
	Name       string          `json:"name"`
	Metadata   json.RawMessage `json:"metadata"`
}

type Artifact struct {
	ExternalID string          `json:"external_id"`
	Kind       string          `json:"kind"`
	Path       string          `json:"path"`
	URL        string          `json:"url"`
	MimeType   string          `json:"mime_type"`
	Text       string          `json:"text"`
	Hash       string          `json:"hash"`
	Metadata   json.RawMessage `json:"metadata"`
}

type Link struct {
	URL  string `json:"url"`
	Text string `json:"text"`
}

// RelationTarget is the versioned qualified cross-source target. When present it
// takes precedence over the legacy same-source TargetExternalID field.
type RelationTarget struct {
	Source     string `json:"source"`
	Collection string `json:"collection"`
	ExternalID string `json:"external_id"`
}

type Relation struct {
	// Target is the preferred qualified form (source + collection + external_id).
	// Older adapters that only set TargetExternalID remain valid and resolve
	// within the source item's own source_id.
	Target           *RelationTarget `json:"target"`
	TargetItemID     string          `json:"target_item_id"`
	TargetExternalID string          `json:"target_external_id"`
	Type             string          `json:"type"`
	Confidence       *float64        `json:"confidence"`
	Metadata         json.RawMessage `json:"metadata"`
}

// Qualified returns the effective (source, collection, external_id) resolution
// key. Empty source/collection means legacy same-source external-id lookup.
func (r Relation) Qualified() (source, collection, externalID string) {
	if r.Target != nil {
		source = strings.TrimSpace(r.Target.Source)
		collection = strings.TrimSpace(r.Target.Collection)
		externalID = strings.TrimSpace(r.Target.ExternalID)
	}
	if externalID == "" {
		externalID = strings.TrimSpace(r.TargetExternalID)
	}
	return source, collection, externalID
}

type RawRef struct {
	Format  string `json:"format"`
	Hash    string `json:"hash"`
	Path    string `json:"path"`
	Ordinal *int64 `json:"ordinal"`
}

func Parse(line []byte) (Record, error) {
	var rec Record
	if err := json.Unmarshal(line, &rec); err != nil {
		return rec, err
	}
	rec.Unknown = append([]byte(nil), line...)
	return rec, rec.Validate()
}

func (r Record) Validate() error {
	if r.Schema != SchemaV1 {
		return fmt.Errorf("unsupported schema %q", r.Schema)
	}
	if r.Source.Kind == "" {
		return errors.New("missing source.kind")
	}
	if r.Collection.ExternalID == "" {
		return errors.New("missing collection.external_id")
	}
	if r.Collection.Kind == "" {
		return errors.New("missing collection.kind")
	}
	if r.Item.ExternalID == "" {
		return errors.New("missing item.external_id")
	}
	if r.Item.Kind == "" {
		return errors.New("missing item.kind")
	}
	return nil
}
