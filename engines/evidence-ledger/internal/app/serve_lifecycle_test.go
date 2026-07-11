package app

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"net"
	"net/http"
	"strings"
	"testing"
	"time"
)

func TestServeBindFailureDoesNotReportStartup(t *testing.T) {
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
	if strings.Contains(out.String(), `"ok": true`) {
		t.Fatalf("serve reported startup json on bind failure: stdout=%s stderr=%s", out.String(), errw.String())
	}
	if !strings.Contains(errw.String(), "serve:") || !strings.Contains(errw.String(), "bind") {
		t.Fatalf("serve bind error missing from stderr: %q", errw.String())
	}
}

func TestServeReportsActualBoundAddrAndShutsDown(t *testing.T) {
	withTempHome(t)
	runOK(t, "init")

	started := make(chan *http.Server, 1)
	oldHook := serveServerStarted
	serveServerStarted = func(srv *http.Server) {
		started <- srv
	}
	t.Cleanup(func() { serveServerStarted = oldHook })

	pr, pw := io.Pipe()
	var errw bytes.Buffer
	done := make(chan int, 1)
	go func() {
		defer pw.Close()
		done <- Run([]string{"serve", "--addr", "127.0.0.1:0", "--json"}, pw, &errw)
	}()

	var startup map[string]any
	if err := json.NewDecoder(pr).Decode(&startup); err != nil {
		t.Fatalf("decode startup json: %v\nstderr=%s", err, errw.String())
	}
	if startup["ok"] != true {
		t.Fatalf("startup ok=%v, want true; startup=%v", startup["ok"], startup)
	}
	addr, _ := startup["addr"].(string)
	if addr == "" || strings.HasSuffix(addr, ":0") {
		t.Fatalf("startup addr=%q, want actual bound address", addr)
	}

	resp, err := http.Get("http://" + addr + "/status")
	if err != nil {
		t.Fatalf("GET /status at %s: %v", addr, err)
	}
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("GET /status status=%d, want %d", resp.StatusCode, http.StatusOK)
	}
	_ = resp.Body.Close()

	var srv *http.Server
	select {
	case srv = <-started:
	case <-time.After(2 * time.Second):
		t.Fatal("serve did not expose started server")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	if err := srv.Shutdown(ctx); err != nil {
		t.Fatalf("shutdown server: %v", err)
	}
	select {
	case code := <-done:
		if code != 0 {
			t.Fatalf("serve returned code=%d after shutdown; stderr=%s", code, errw.String())
		}
	case <-time.After(2 * time.Second):
		t.Fatal("serve did not return after shutdown")
	}
}
