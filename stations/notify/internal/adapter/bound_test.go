package adapter

import (
	"bytes"
	"errors"
	"io"
	"strings"
	"testing"
)

func TestReadBounded_BelowLimit(t *testing.T) {
	in := bytes.Repeat([]byte("a"), MaxInputBytes-1)
	got, err := ReadBounded(bytes.NewReader(in))
	if err != nil {
		t.Fatalf("ReadBounded: %v", err)
	}
	if len(got) != MaxInputBytes-1 {
		t.Fatalf("len = %d, want %d", len(got), MaxInputBytes-1)
	}
}

func TestReadBounded_AtLimit(t *testing.T) {
	in := bytes.Repeat([]byte("b"), MaxInputBytes)
	got, err := ReadBounded(bytes.NewReader(in))
	if err != nil {
		t.Fatalf("ReadBounded: %v", err)
	}
	if len(got) != MaxInputBytes {
		t.Fatalf("len = %d, want %d", len(got), MaxInputBytes)
	}
}

func TestReadBounded_AboveLimit(t *testing.T) {
	in := bytes.Repeat([]byte("c"), MaxInputBytes+1)
	got, err := ReadBounded(bytes.NewReader(in))
	if !errors.Is(err, ErrInputTooLarge) {
		t.Fatalf("err = %v, want ErrInputTooLarge", err)
	}
	if got != nil {
		t.Fatalf("got %d bytes, want nil on oversized input", len(got))
	}
}

func TestReadBounded_AboveLimitDoesNotDrainUnbounded(t *testing.T) {
	// A reader that can produce far more than the limit must still stop after
	// MaxInputBytes+1 so memory stays bounded.
	r := &countingReader{limit: MaxInputBytes * 4}
	_, err := ReadBounded(r)
	if !errors.Is(err, ErrInputTooLarge) {
		t.Fatalf("err = %v, want ErrInputTooLarge", err)
	}
	if r.read > MaxInputBytes+1 {
		t.Fatalf("read %d bytes, want at most %d", r.read, MaxInputBytes+1)
	}
}

func TestAutoDetect_AboveLimit(t *testing.T) {
	in := strings.Repeat("x", MaxInputBytes+1)
	_, err := AutoDetect(strings.NewReader(in))
	if !errors.Is(err, ErrInputTooLarge) {
		t.Fatalf("err = %v, want ErrInputTooLarge", err)
	}
}

func TestCheckSize_Boundaries(t *testing.T) {
	if err := CheckSize(bytes.Repeat([]byte("d"), MaxInputBytes)); err != nil {
		t.Fatalf("at limit: %v", err)
	}
	if err := CheckSize(bytes.Repeat([]byte("e"), MaxInputBytes+1)); !errors.Is(err, ErrInputTooLarge) {
		t.Fatalf("above limit: %v", err)
	}
}

type countingReader struct {
	limit int
	read  int
}

func (c *countingReader) Read(p []byte) (int, error) {
	if c.read >= c.limit {
		return 0, io.EOF
	}
	n := len(p)
	if c.read+n > c.limit {
		n = c.limit - c.read
	}
	for i := 0; i < n; i++ {
		p[i] = 'z'
	}
	c.read += n
	return n, nil
}
