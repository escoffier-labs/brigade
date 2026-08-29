package app

import (
	"database/sql"
	"path/filepath"
	"testing"

	"github.com/escoffier-labs/miseledger/internal/archive"
)

func benchmarkSearchArchive(b *testing.B) *sql.DB {
	b.Helper()
	db, err := archive.Open(filepath.Join(b.TempDir(), "search-benchmark.db"))
	if err != nil {
		b.Fatal(err)
	}
	b.Cleanup(func() { _ = db.Close() })
	if err := archive.Migrate(db); err != nil {
		b.Fatal(err)
	}
	insertSyntheticSearchArchive(b, db, 2000)
	return db
}

func BenchmarkSearch(b *testing.B) {
	db := benchmarkSearchArchive(b)
	opts := SearchOpts{Query: "needle", Limit: 20}
	sqlText, params := buildSearchQuery(opts)

	b.Run("plan", func(b *testing.B) {
		b.ReportAllocs()
		b.ResetTimer()
		for i := 0; i < b.N; i++ {
			rows, err := db.Query("explain query plan "+sqlText, params...)
			if err != nil {
				b.Fatal(err)
			}
			for rows.Next() {
				var id, parent, notUsed int
				var detail string
				if err := rows.Scan(&id, &parent, &notUsed, &detail); err != nil {
					_ = rows.Close()
					b.Fatal(err)
				}
			}
			if err := rows.Close(); err != nil {
				b.Fatal(err)
			}
		}
	})

	b.Run("execution", func(b *testing.B) {
		b.ReportAllocs()
		b.ResetTimer()
		for i := 0; i < b.N; i++ {
			if _, err := search(db, opts); err != nil {
				b.Fatal(err)
			}
		}
	})
}
