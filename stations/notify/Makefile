# agent-notify build + test targets

BINARY  := agent-notify
PKG     := ./cmd/agent-notify
VERSION ?= $(shell git describe --tags --always --dirty 2>/dev/null || echo dev)
COMMIT  ?= $(shell git rev-parse --short HEAD 2>/dev/null || echo unknown)
BUILT   ?= $(shell date -u +%Y-%m-%dT%H:%M:%SZ)
LDFLAGS := -ldflags "-X main.version=$(VERSION) -X main.commit=$(COMMIT) -X main.buildDate=$(BUILT) -s -w"
PLATFORMS ?= linux/amd64 linux/arm64 darwin/amd64 darwin/arm64 windows/amd64 windows/arm64

.PHONY: all build test vet cross install package clean clean-dist

all: test build

build:
	go build $(LDFLAGS) -o $(BINARY) $(PKG)

test:
	go test -race ./...

vet:
	go vet ./...

# Cross-build for common targets. Output to dist/.
cross: clean
	mkdir -p dist
	GOOS=linux   GOARCH=amd64 go build $(LDFLAGS) -o dist/$(BINARY)-linux-amd64    $(PKG)
	GOOS=linux   GOARCH=arm64 go build $(LDFLAGS) -o dist/$(BINARY)-linux-arm64    $(PKG)
	GOOS=darwin  GOARCH=amd64 go build $(LDFLAGS) -o dist/$(BINARY)-darwin-amd64   $(PKG)
	GOOS=darwin  GOARCH=arm64 go build $(LDFLAGS) -o dist/$(BINARY)-darwin-arm64   $(PKG)

install: build
	$(if $(strip $(HOME)),,$(error HOME is not set; refusing to install into /bin))
	mkdir -p $(HOME)/bin
	install -m 0755 $(BINARY) $(HOME)/bin/$(BINARY)

clean-dist:
	rm -rf dist

# Build per-platform archives with checksums. Output to dist/.
package: clean-dist
	mkdir -p dist/tmp
	set -eu; \
	for platform in $(PLATFORMS); do \
		os=$${platform%/*}; \
		arch=$${platform#*/}; \
		ext=""; \
		if [ "$$os" = "windows" ]; then ext=".exe"; fi; \
		pkg="$(BINARY)_$(VERSION)_$${os}_$${arch}"; \
		out="dist/tmp/$$pkg"; \
		mkdir -p "$$out"; \
		GOOS=$$os GOARCH=$$arch go build -trimpath $(LDFLAGS) -o "$$out/$(BINARY)$$ext" $(PKG); \
		cp README.md LICENSE "$$out/"; \
		chmod 755 "$$out" "$$out/$(BINARY)$$ext"; \
		chmod 644 "$$out/README.md" "$$out/LICENSE"; \
		tar -C dist/tmp -czf "dist/$$pkg.tar.gz" "$$pkg"; \
	done
	cd dist && sha256sum *.tar.gz > checksums.txt
	chmod 644 dist/*.tar.gz dist/checksums.txt
	rm -rf dist/tmp

clean:
	rm -f $(BINARY)
	rm -rf dist/
