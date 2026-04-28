# agent-notify build + test targets

BINARY  := agent-notify
PKG     := ./cmd/agent-notify
VERSION ?= $(shell git describe --tags --always --dirty 2>/dev/null || echo dev)
LDFLAGS := -ldflags "-X main.version=$(VERSION) -s -w"

.PHONY: all build test cross install clean

all: test build

build:
	go build $(LDFLAGS) -o $(BINARY) $(PKG)

test:
	go test -race ./...

# Cross-build for common targets. Output to dist/.
cross: clean
	mkdir -p dist
	GOOS=linux   GOARCH=amd64 go build $(LDFLAGS) -o dist/$(BINARY)-linux-amd64    $(PKG)
	GOOS=linux   GOARCH=arm64 go build $(LDFLAGS) -o dist/$(BINARY)-linux-arm64    $(PKG)
	GOOS=darwin  GOARCH=amd64 go build $(LDFLAGS) -o dist/$(BINARY)-darwin-amd64   $(PKG)
	GOOS=darwin  GOARCH=arm64 go build $(LDFLAGS) -o dist/$(BINARY)-darwin-arm64   $(PKG)

install: build
	install -m 0755 $(BINARY) $(HOME)/bin/$(BINARY)

clean:
	rm -f $(BINARY)
	rm -rf dist/
