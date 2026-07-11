package app

import (
	"bytes"
	"encoding/json"
	"net"
	"strings"
	"testing"
)

func TestServeDispatchUsesRequestedEphemeralAddrForBindFailure(t *testing.T) {
	withTempHome(t)
	runOK(t, "init")

	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen on ephemeral loopback port: %v", err)
	}
	defer ln.Close()

	addr := ln.Addr().String()
	var out, errw bytes.Buffer
	code := Run([]string{"serve", "--addr", addr, "--json"}, &out, &errw)
	if code == 0 {
		t.Fatalf("serve succeeded despite occupied addr %s", addr)
	}

	var started map[string]any
	if err := json.Unmarshal(out.Bytes(), &started); err != nil {
		t.Fatalf("serve did not write startup json: %v\nstdout=%s stderr=%s", err, out.String(), errw.String())
	}
	if started["ok"] != true || started["addr"] != addr {
		t.Fatalf("serve startup json = %v, want ok=true addr=%s", started, addr)
	}
	if !strings.Contains(errw.String(), "serve:") || !strings.Contains(errw.String(), "bind") {
		t.Fatalf("serve bind error missing from stderr: %q", errw.String())
	}
}
