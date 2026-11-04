# Container image for the MiseLedger stdio MCP server (`brigade-evidence mcp`).
# MiseLedger is pure Go (modernc.org/sqlite), so the build needs no CGO and the
# result is a static, dependency-free binary.

FROM golang:1.25-bookworm AS builder
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/brigade-evidence ./cmd/brigade-evidence

FROM gcr.io/distroless/static-debian12
COPY --from=builder /out/brigade-evidence /usr/local/bin/brigade-evidence
# The MCP server speaks JSON-RPC over stdio (no network port). Introspection
# needs no archive; for real use, mount a volume and set HOME to persist it.
ENTRYPOINT ["brigade-evidence", "mcp"]
