package adapter

import (
	"fmt"
	"io"
)

// MaxInputBytes is the hard ceiling for hook and stdin payloads. Reading stops
// one byte past this limit so oversized input is detected without unbounded
// allocation.
const MaxInputBytes = 256 * 1024

// ErrInputTooLarge is returned when hook or stdin input exceeds MaxInputBytes.
var ErrInputTooLarge = fmt.Errorf("input exceeds %d-byte limit", MaxInputBytes)

// ReadBounded reads from r up to MaxInputBytes. If more data is available it
// returns ErrInputTooLarge without retaining the excess.
func ReadBounded(r io.Reader) ([]byte, error) {
	limited := io.LimitReader(r, int64(MaxInputBytes)+1)
	raw, err := io.ReadAll(limited)
	if err != nil {
		return nil, fmt.Errorf("read input: %w", err)
	}
	if len(raw) > MaxInputBytes {
		return nil, ErrInputTooLarge
	}
	return raw, nil
}

// CheckSize refuses a pre-buffered payload (for example a Codex argv JSON
// blob) that exceeds MaxInputBytes.
func CheckSize(raw []byte) error {
	if len(raw) > MaxInputBytes {
		return ErrInputTooLarge
	}
	return nil
}
