package textnorm

import "testing"

func TestNormalize(t *testing.T) {
	tests := []struct {
		name string
		in   string
		want string
	}{
		{"empty", "", ""},
		{"only spaces", "   ", ""},
		{"trim ends", "  hello  ", "hello"},
		{"collapse internal runs", "a   b", "a b"},
		{"tabs and newlines to single space", "a\t\nb", "a b"},
		{"mixed whitespace", "  foo\tbar \n baz  ", "foo bar baz"},
		{"already normal", "one two three", "one two three"},
		{"unicode preserved", "café\tdéjà", "café déjà"},
		{"no internal whitespace", "single", "single"},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := Normalize(tt.in); got != tt.want {
				t.Fatalf("Normalize(%q) = %q, want %q", tt.in, got, tt.want)
			}
		})
	}
}

func TestNormalizeIsIdempotent(t *testing.T) {
	in := "  lots   of\t\twhitespace\nhere  "
	once := Normalize(in)
	if twice := Normalize(once); twice != once {
		t.Fatalf("not idempotent: %q -> %q", once, twice)
	}
}
