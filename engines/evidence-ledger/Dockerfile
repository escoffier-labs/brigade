# Container image for the MiseLedger stdio MCP server (`miseledger mcp`).
# MiseLedger is pure Go (modernc.org/sqlite), so the build needs no CGO and the
# result is a static, dependency-free binary.

FROM golang:1.22-bookworm AS builder
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/miseledger ./cmd/miseledger

FROM gcr.io/distroless/static-debian12
COPY --from=builder /out/miseledger /usr/local/bin/miseledger
# The MCP server speaks JSON-RPC over stdio (no network port). Introspection
# needs no archive; for real use, mount a volume and set HOME to persist it.
ENTRYPOINT ["miseledger", "mcp"]
