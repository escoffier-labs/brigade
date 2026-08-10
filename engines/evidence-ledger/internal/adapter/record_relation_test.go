package adapter

import "testing"

func TestRelationQualifiedPrefersTargetObject(t *testing.T) {
	rel := Relation{
		TargetExternalID: "legacy",
		Target: &RelationTarget{
			Source:     "brigade",
			Collection: "brigade:receipts",
			ExternalID: "receipt:1",
		},
	}
	source, collection, externalID := rel.Qualified()
	if source != "brigade" || collection != "brigade:receipts" || externalID != "receipt:1" {
		t.Fatalf("got %q %q %q", source, collection, externalID)
	}
}

func TestRelationQualifiedFallsBackToLegacyExternalID(t *testing.T) {
	rel := Relation{TargetExternalID: "legacy-id"}
	source, collection, externalID := rel.Qualified()
	if source != "" || collection != "" || externalID != "legacy-id" {
		t.Fatalf("got %q %q %q", source, collection, externalID)
	}
}
