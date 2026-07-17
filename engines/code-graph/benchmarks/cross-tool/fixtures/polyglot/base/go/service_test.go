package service

import "testing"

func TestServe(t *testing.T) {
	if Serve("fixture") != "fixture" {
		t.Fatal("unexpected value")
	}
}
