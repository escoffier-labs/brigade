package ingest

import (
	"errors"
	"fmt"
	"testing"
)

func BenchmarkIsBusy(b *testing.B) {
	benchmarks := []struct {
		name string
		err  error
	}{
		{name: "code", err: resultCodeError{code: sqliteBusySnapshot, msg: "database is locked"}},
		{name: "wrapped_code", err: fmt.Errorf("write: %w", resultCodeError{code: sqliteBusySnapshot, msg: "database is locked"})},
		{name: "free_text", err: errors.New("database is locked (517) (SQLITE_BUSY)")},
	}
	for _, benchmark := range benchmarks {
		b.Run(benchmark.name, func(b *testing.B) {
			b.ReportAllocs()
			for i := 0; i < b.N; i++ {
				if IsBusy(benchmark.err) != (benchmark.name != "free_text") {
					b.Fatal("unexpected busy classification")
				}
			}
		})
	}
}
