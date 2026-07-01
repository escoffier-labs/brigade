package app

import (
	"bufio"
	"bytes"
	"encoding/json"
	"strconv"
	"strings"
	"testing"
)

// TestMCPStdioFraming drives the real cmdMCP transport over stdin for both
// supported framings. The newline-delimited case is the ratified MCP stdio
// spec (Claude Desktop, MCP Inspector, Glama); before the dual-framing fix the
// server only accepted Content-Length and silently produced no output here.
func TestMCPStdioFraming(t *testing.T) {
	withTempHome(t)
	runOK(t, "init")

	initMsg := `{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}`
	listMsg := `{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}`

	cases := []struct {
		name  string
		input string
		parse func(t *testing.T, out []byte) []map[string]any
	}{
		{
			name:  "newline-delimited (MCP stdio spec)",
			input: initMsg + "\n" + listMsg + "\n",
			parse: parseNewlineResponses,
		},
		{
			name:  "content-length (LSP-style)",
			input: contentLengthFrame(initMsg) + contentLengthFrame(listMsg),
			parse: parseContentLengthResponses,
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			oldStdin := stdin
			stdin = strings.NewReader(tc.input)
			t.Cleanup(func() { stdin = oldStdin })

			var out, errw bytes.Buffer
			if code := cmdMCP(nil, &out, &errw); code != 0 {
				t.Fatalf("cmdMCP exit=%d errw=%s", code, errw.String())
			}
			resps := tc.parse(t, out.Bytes())
			if len(resps) != 2 {
				t.Fatalf("got %d responses, want 2; out=%q", len(resps), out.String())
			}
			res0, _ := resps[0]["result"].(map[string]any)
			server, _ := res0["serverInfo"].(map[string]any)
			if name, _ := server["name"].(string); name != "miseledger" {
				t.Fatalf("initialize serverInfo.name=%q, want miseledger; resp=%v", name, resps[0])
			}
			res1, _ := resps[1]["result"].(map[string]any)
			tools, _ := res1["tools"].([]any)
			if len(tools) == 0 {
				t.Fatalf("tools/list returned no tools; resp=%v", resps[1])
			}
		})
	}
}

// TestMCPNewlineToolsCall exercises a real tools/call (db-backed, larger
// response) over newline framing, since that is where the smoke test hangs.
func TestMCPNewlineToolsCall(t *testing.T) {
	withTempHome(t)
	runOK(t, "init")
	runOK(t, "import", "adapter", repoPath(t, "testdata/adapters/discrawl.fixture.jsonl"), "--source", "discrawl")

	input := strings.Join([]string{
		`{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}`,
		`{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}`,
		`{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"create_evidence_bundle","arguments":{"query":"adapter contract","source":"discrawl","limit":5}}}`,
	}, "\n") + "\n"

	oldStdin := stdin
	stdin = strings.NewReader(input)
	t.Cleanup(func() { stdin = oldStdin })

	var out, errw bytes.Buffer
	if code := cmdMCP(nil, &out, &errw); code != 0 {
		t.Fatalf("cmdMCP exit=%d errw=%s", code, errw.String())
	}
	resps := parseNewlineResponses(t, out.Bytes())
	if len(resps) != 3 {
		t.Fatalf("got %d responses, want 3; out=%q", len(resps), out.String())
	}
	res, _ := resps[2]["result"].(map[string]any)
	if content, _ := res["content"].([]any); len(content) == 0 {
		t.Fatalf("tools/call returned no content: %v", resps[2])
	}
}

func contentLengthFrame(s string) string {
	return "Content-Length: " + strconv.Itoa(len(s)) + "\r\n\r\n" + s
}

func parseNewlineResponses(t *testing.T, out []byte) []map[string]any {
	t.Helper()
	var resps []map[string]any
	for _, line := range bytes.Split(bytes.TrimSpace(out), []byte("\n")) {
		line = bytes.TrimSpace(line)
		if len(line) == 0 {
			continue
		}
		var m map[string]any
		if err := json.Unmarshal(line, &m); err != nil {
			t.Fatalf("bad newline response %q: %v", line, err)
		}
		resps = append(resps, m)
	}
	return resps
}

func parseContentLengthResponses(t *testing.T, out []byte) []map[string]any {
	t.Helper()
	r := bufio.NewReader(bytes.NewReader(out))
	var resps []map[string]any
	for {
		frame, err := readContentLengthFrame(r)
		if err != nil {
			break
		}
		var m map[string]any
		if err := json.Unmarshal(frame, &m); err != nil {
			t.Fatalf("bad content-length response %q: %v", frame, err)
		}
		resps = append(resps, m)
	}
	return resps
}
