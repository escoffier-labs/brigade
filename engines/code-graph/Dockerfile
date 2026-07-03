# syntax=docker/dockerfile:1

# Stage 1: build the read-only MCP server binary.
# Edition 2024 requires rust >= 1.85; the `1-slim` tag tracks a current toolchain.
# rusqlite uses the `bundled` feature, so no system sqlite/openssl libs are needed.
FROM rust:1-slim AS builder
WORKDIR /app
COPY . .
RUN cargo build --release --bin graphtrail-mcp

# Stage 2: minimal runtime image. The MCP server speaks JSON-RPC over stdio,
# so there is no port to EXPOSE.
FROM debian:bookworm-slim
COPY --from=builder /app/target/release/graphtrail-mcp /usr/local/bin/graphtrail-mcp
ENTRYPOINT ["graphtrail-mcp"]
