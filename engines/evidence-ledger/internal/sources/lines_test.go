package sources

import (
	"strings"
	"testing"
)

type collectedLine struct {
	line    string
	tooLong bool
	size    int64
}

func collectLines(t *testing.T, input string, max int) []collectedLine {
	t.Helper()
	var out []collectedLine
	err := EachLine(strings.NewReader(input), max, func(line []byte, tooLong bool, size int64) error {
		out = append(out, collectedLine{line: string(line), tooLong: tooLong, size: size})
		return nil
	})
	if err != nil {
		t.Fatalf("EachLine: %v", err)
	}
	return out
}

func TestEachLineSkipsOversizedLineAndContinues(t *testing.T) {
	long := strings.Repeat("x", 100)
	got := collectLines(t, "before\n"+long+"\nafter\n", 10)
	if len(got) != 3 {
		t.Fatalf("lines = %d, want 3: %+v", len(got), got)
	}
	if got[0].line != "before" || got[0].tooLong {
		t.Fatalf("line 1 = %+v", got[0])
	}
	if !got[1].tooLong || got[1].line != "" || got[1].size != 100 {
		t.Fatalf("oversized line = %+v, want tooLong with size 100", got[1])
	}
	if got[2].line != "after" || got[2].tooLong {
		t.Fatalf("line 3 = %+v, oversized line must not poison later lines", got[2])
	}
}

// A line far larger than the read buffer must be drained chunk by chunk
// without buffering it whole.
func TestEachLineOversizedSpansManyReadChunks(t *testing.T) {
	long := strings.Repeat("y", 300*1024)
	got := collectLines(t, long+"\nok\n", 1024)
	if len(got) != 2 {
		t.Fatalf("lines = %d, want 2", len(got))
	}
	if !got[0].tooLong || got[0].size != int64(len(long)) {
		t.Fatalf("oversized line = %+v", got[0])
	}
	if got[1].line != "ok" {
		t.Fatalf("line after oversized = %+v", got[1])
	}
}

func TestEachLineFinalLineWithoutNewline(t *testing.T) {
	got := collectLines(t, "a\nb", 10)
	if len(got) != 2 || got[1].line != "b" || got[1].tooLong {
		t.Fatalf("lines = %+v", got)
	}
	// Oversized final line without a terminator is still reported.
	got = collectLines(t, "a\n"+strings.Repeat("z", 20), 10)
	if len(got) != 2 || !got[1].tooLong || got[1].size != 20 {
		t.Fatalf("lines = %+v", got)
	}
}

func TestEachLineStripsCarriageReturns(t *testing.T) {
	got := collectLines(t, "a\r\nb\r", 10)
	if len(got) != 2 || got[0].line != "a" || got[1].line != "b" {
		t.Fatalf("lines = %+v", got)
	}
}

func TestEachLineBlankAndEmptyInput(t *testing.T) {
	if got := collectLines(t, "", 10); len(got) != 0 {
		t.Fatalf("empty input produced %+v", got)
	}
	got := collectLines(t, "\n\n", 10)
	if len(got) != 2 || got[0].line != "" || got[1].line != "" {
		t.Fatalf("blank lines = %+v", got)
	}
}
