# Runs Brigade's read-only memory MCP server over stdio, for MCP directories
# (Glama and similar) that build a repo image and introspect its tools. Mount a
# repo or workspace at /workspace to serve its memory cards. Switch to the
# skills server with: docker run -i IMAGE skills serve-mcp --stdio --target /workspace
#
# Installs the latest brigade-cli release from PyPI, so the image matches what
# users install and default-branch builds never point at an unreleased version.
# Pin one with: docker build --build-arg BRIGADE_VERSION=X.Y.Z .
# The build context stays empty (.dockerignore excludes everything here).
FROM python:3.12.14-slim-bookworm@sha256:392307d22300de8b5986851a12d9176dfc0fc073e65bf6523ebd7dcbeb23564e

ARG BRIGADE_VERSION=
LABEL org.opencontainers.image.source="https://github.com/escoffier-labs/brigade" \
      org.opencontainers.image.description="Brigade read-only MCP servers for memory cards and skills"

RUN python -m pip install --no-cache-dir "brigade-cli${BRIGADE_VERSION:+==${BRIGADE_VERSION}}" \
    && groupadd --gid 10001 brigade \
    && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /workspace brigade \
    && install -d -o 10001 -g 10001 -m 0755 /workspace

USER 10001:10001
WORKDIR /workspace
ENTRYPOINT ["brigade"]
CMD ["memory", "serve-mcp", "--stdio", "--target", "/workspace"]
